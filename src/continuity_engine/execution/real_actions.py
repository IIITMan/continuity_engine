"""Real host action adapters for Continuity Engine.

This module performs real local side effects for SPEAK, NOTIFY and WAIT, and
provides explicit interfaces for actual sensor and LED hardware. Unsupported
hardware actions fail closed; they are never reported as successful merely
because a driver is not configured.

No shell is used. User-provided text is passed as argv/stdin or base64 data to
fixed platform scripts. The caller must dispatch through ContinuityEngine so
SafetySupervisor checks run before the executor is reached.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import math
import os
import re
import shutil
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol

from ..core.models import ActionProposal, ActionResult, ActionStatus, thaw, validate_json_compatible


class ActionDriverError(RuntimeError):
    """A controlled driver error.

    `outcome_unknown=True` means the external side effect may have happened,
    so callers must not automatically retry it.
    """

    def __init__(self, code: str, message: str = "", *, outcome_unknown: bool = False):
        super().__init__(message or code)
        self.code = code
        self.outcome_unknown = outcome_unknown


class ActionCancelled(Exception):
    """Raised by a driver when a cancellation request was observed."""


class SpeechDriver(Protocol):
    async def speak(self, action_id: str, text: str, cancel_event: asyncio.Event) -> Mapping[str, Any]: ...
    async def health_check(self) -> bool: ...


class NotificationDriver(Protocol):
    async def notify(self, action_id: str, title: str, text: str, cancel_event: asyncio.Event) -> Mapping[str, Any]: ...
    async def health_check(self) -> bool: ...


class ObservationProvider(Protocol):
    async def observe(self, key: str, reason: str, cancel_event: asyncio.Event) -> Mapping[str, Any]: ...
    async def health_check(self) -> bool: ...


class LightDriver(Protocol):
    async def set_pattern(
        self, action_id: str, pattern: str, duration_s: float | None, cancel_event: asyncio.Event
    ) -> Mapping[str, Any]: ...
    async def health_check(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class _ProcessOutcome:
    returncode: int
    stderr: str
    cancelled: bool = False


class _ManagedProcessRunner:
    """Cancellable subprocess runner shared by platform speech/notification drivers."""

    def __init__(self, *, terminate_grace_s: float = 0.5):
        if not math.isfinite(terminate_grace_s) or terminate_grace_s <= 0:
            raise ValueError("terminate_grace_s must be finite and > 0")
        self.terminate_grace_s = terminate_grace_s
        self._processes: dict[str, asyncio.subprocess.Process] = {}

    async def run(
        self,
        action_id: str,
        argv: list[str],
        *,
        cancel_event: asyncio.Event,
        stdin_data: bytes | None = None,
    ) -> _ProcessOutcome:
        if cancel_event.is_set():
            raise ActionCancelled()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ActionDriverError("DRIVER_UNAVAILABLE", f"command not found: {argv[0]}") from exc
        except PermissionError as exc:
            raise ActionDriverError("DRIVER_PERMISSION_DENIED", f"permission denied: {argv[0]}") from exc
        except OSError as exc:
            raise ActionDriverError("PROCESS_START_FAILED", f"could not start {argv[0]}") from exc

        self._processes[action_id] = process
        communicate_task = asyncio.create_task(process.communicate(input=stdin_data))
        cancel_task = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {communicate_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancel_task in done and cancel_event.is_set():
                await self._terminate(process)
                try:
                    _, stderr = await asyncio.wait_for(
                        asyncio.shield(communicate_task), timeout=self.terminate_grace_s + 1.0
                    )
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    communicate_task.cancel()
                    stderr = b""
                raise ActionCancelled()

            _, stderr = await communicate_task
            if cancel_event.is_set():
                raise ActionCancelled()
            return _ProcessOutcome(
                returncode=int(process.returncode if process.returncode is not None else -1),
                stderr=(stderr or b"").decode("utf-8", errors="replace")[-1000:],
            )
        except asyncio.CancelledError:
            cancel_event.set()
            try:
                await asyncio.shield(self._terminate(process))
            except Exception:
                pass
            if not communicate_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(communicate_task), timeout=self.terminate_grace_s + 1.0)
                except BaseException:
                    communicate_task.cancel()
            raise
        finally:
            cancel_task.cancel()
            try:
                await cancel_task
            except BaseException:
                pass
            self._processes.pop(action_id, None)

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=self.terminate_grace_s)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                return
            await process.wait()

    async def cancel(self, action_id: str) -> bool:
        process = self._processes.get(action_id)
        if process is None or process.returncode is not None:
            return False
        await self._terminate(process)
        return True

    async def close(self) -> None:
        processes = list(self._processes.values())
        if processes:
            await asyncio.gather(*(self._terminate(p) for p in processes), return_exceptions=True)


class PlatformSpeechDriver:
    """Platform-native speech synthesis using installed OS tools, without a shell.

    Linux requires `espeak-ng` or `espeak`; macOS uses `/usr/bin/say`; Windows
    uses Windows PowerShell + System.Speech. For other targets, inject another
    `SpeechDriver` implementation.
    """

    def __init__(self, *, runner: _ManagedProcessRunner | None = None, rate_wpm: int = 180):
        if not isinstance(rate_wpm, int) or isinstance(rate_wpm, bool) or not 80 <= rate_wpm <= 350:
            raise ValueError("rate_wpm must be an integer in [80, 350]")
        self.runner = runner or _ManagedProcessRunner()
        self.rate_wpm = rate_wpm

    def command_for(self, text: str) -> tuple[list[str], bytes | None, str]:
        if sys.platform.startswith("linux"):
            executable = shutil.which("espeak-ng") or shutil.which("espeak")
            if not executable:
                raise ActionDriverError("DRIVER_UNAVAILABLE", "install espeak-ng/espeak or inject a SpeechDriver")
            return [executable, "--stdin", "-s", str(self.rate_wpm)], text.encode("utf-8"), Path(executable).name
        if sys.platform == "darwin":
            executable = "/usr/bin/say"
            if not Path(executable).exists():
                raise ActionDriverError("DRIVER_UNAVAILABLE", "macOS say command is unavailable")
            # Text is one argv item: it is not interpreted by a shell.
            return [executable, "-r", str(self.rate_wpm), text], None, "say"
        if os.name == "nt":
            executable = shutil.which("powershell.exe") or shutil.which("powershell")
            if not executable:
                raise ActionDriverError("DRIVER_UNAVAILABLE", "Windows PowerShell is unavailable")
            payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
            script = (
                "$text = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('"
                + payload
                + "')); Add-Type -AssemblyName System.Speech; "
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                f"$s.Rate = {max(-5, min(5, round((self.rate_wpm - 180) / 20)))}; "
                "$s.Speak($text); $s.Dispose();"
            )
            encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
            return [executable, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], None, "System.Speech"
        raise ActionDriverError("DRIVER_UNAVAILABLE", f"no native TTS adapter for platform {sys.platform}")

    async def speak(self, action_id: str, text: str, cancel_event: asyncio.Event) -> Mapping[str, Any]:
        command, stdin_data, backend = self.command_for(text)
        outcome = await self.runner.run(action_id, command, stdin_data=stdin_data, cancel_event=cancel_event)
        if outcome.cancelled:
            raise ActionCancelled()
        if outcome.returncode != 0:
            # Speech may have begun before an OS TTS process failed.
            raise ActionDriverError("TTS_PROCESS_FAILED", outcome.stderr or f"TTS exited {outcome.returncode}", outcome_unknown=True)
        return {"backend": backend, "character_count": len(text)}

    async def health_check(self) -> bool:
        if sys.platform.startswith("linux"):
            return bool(shutil.which("espeak-ng") or shutil.which("espeak"))
        if sys.platform == "darwin":
            return Path("/usr/bin/say").exists()
        if os.name == "nt":
            return bool(shutil.which("powershell.exe") or shutil.which("powershell"))
        return False

    async def close(self) -> None:
        await self.runner.close()


class DesktopNotificationDriver:
    """Native desktop notification driver for Linux, macOS and Windows."""

    def __init__(self, *, runner: _ManagedProcessRunner | None = None, display_seconds: int = 4):
        if not isinstance(display_seconds, int) or isinstance(display_seconds, bool) or not 1 <= display_seconds <= 10:
            raise ValueError("display_seconds must be an integer in [1, 10]")
        self.runner = runner or _ManagedProcessRunner()
        self.display_seconds = display_seconds

    @staticmethod
    def _apple_script_quote(value: str) -> str:
        # AppleScript string literal, not shell quoting.
        value = value.replace("\r", " ").replace("\n", " ")
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def command_for(self, title: str, text: str) -> tuple[list[str], bytes | None, str]:
        if sys.platform.startswith("linux"):
            executable = shutil.which("notify-send")
            if not executable:
                raise ActionDriverError("DRIVER_UNAVAILABLE", "install libnotify/notify-send or inject a NotificationDriver")
            return [executable, "--app-name=Continuity Engine", "--", title, text], None, "notify-send"
        if sys.platform == "darwin":
            executable = shutil.which("osascript") or "/usr/bin/osascript"
            if not Path(executable).exists():
                raise ActionDriverError("DRIVER_UNAVAILABLE", "osascript is unavailable")
            script = f"display notification {self._apple_script_quote(text)} with title {self._apple_script_quote(title)}"
            return [executable, "-e", script], None, "osascript"
        if os.name == "nt":
            executable = shutil.which("powershell.exe") or shutil.which("powershell")
            if not executable:
                raise ActionDriverError("DRIVER_UNAVAILABLE", "Windows PowerShell is unavailable")
            payload = base64.b64encode(json.dumps({"title": title, "text": text}, ensure_ascii=False).encode("utf-8")).decode("ascii")
            script = (
                "$p=[System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('"
                + payload
                + "')); $o=ConvertFrom-Json -InputObject $p; "
                "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
                "$n=New-Object System.Windows.Forms.NotifyIcon; $n.Icon=[System.Drawing.SystemIcons]::Information; "
                "$n.BalloonTipTitle=$o.title; $n.BalloonTipText=$o.text; $n.Visible=$true; "
                f"$n.ShowBalloonTip({self.display_seconds * 1000}); Start-Sleep -Seconds {self.display_seconds + 1}; $n.Dispose();"
            )
            encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
            return [executable, "-NoProfile", "-NonInteractive", "-STA", "-EncodedCommand", encoded], None, "Windows.Forms.NotifyIcon"
        raise ActionDriverError("DRIVER_UNAVAILABLE", f"no desktop notification adapter for platform {sys.platform}")

    async def notify(self, action_id: str, title: str, text: str, cancel_event: asyncio.Event) -> Mapping[str, Any]:
        command, stdin_data, backend = self.command_for(title, text)
        outcome = await self.runner.run(action_id, command, stdin_data=stdin_data, cancel_event=cancel_event)
        if outcome.cancelled:
            raise ActionCancelled()
        if outcome.returncode != 0:
            raise ActionDriverError("NOTIFICATION_PROCESS_FAILED", outcome.stderr or f"notification exited {outcome.returncode}", outcome_unknown=True)
        return {"backend": backend, "character_count": len(text)}

    async def health_check(self) -> bool:
        if sys.platform.startswith("linux"):
            return bool(shutil.which("notify-send"))
        if sys.platform == "darwin":
            return Path("/usr/bin/osascript").exists()
        if os.name == "nt":
            return bool(shutil.which("powershell.exe") or shutil.which("powershell"))
        return False

    async def close(self) -> None:
        await self.runner.close()


ObservationCallback = Callable[[str, str], Mapping[str, Any] | Awaitable[Mapping[str, Any]]]


class CallbackObservationProvider:
    """Adapts a host sensor service to the executor's observation contract.

    The callback must actively request/collect an observation and return exactly
    `{value, confidence, ttl_s?}`. The executor validates the payload again before
    returning it to the engine.
    """

    def __init__(self, callback: ObservationCallback):
        if not callable(callback):
            raise TypeError("observation callback must be callable")
        self.callback = callback

    async def observe(self, key: str, reason: str, cancel_event: asyncio.Event) -> Mapping[str, Any]:
        if cancel_event.is_set():
            raise ActionCancelled()

        async def call_provider() -> Any:
            if inspect.iscoroutinefunction(self.callback):
                return await self.callback(key, reason)  # type: ignore[misc]
            # Sensor SDK wrappers are often synchronous and may block. Run them off
            # the event loop; cancellation stops awaiting the result, though Python
            # cannot forcibly terminate an already-running worker thread.
            result = await asyncio.to_thread(self.callback, key, reason)
            if inspect.isawaitable(result):
                return await result
            return result

        task = asyncio.create_task(call_provider())
        cancel_task = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait({task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
            if cancel_task in done and cancel_event.is_set():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise ActionCancelled()
            result = await task
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        finally:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
        if not isinstance(result, Mapping):
            raise ActionDriverError("OBSERVATION_INVALID", "sensor callback must return a mapping")
        return dict(result)

    async def health_check(self) -> bool:
        return True


class SysfsLedDriver:
    """Real Linux LED-class adapter writing `/sys/class/leds/<name>/brightness`.

    The LED name is configuration-owned, never taken from action arguments. If
    sysfs is unavailable or permissions are insufficient, LIGHT fails explicitly.
    Patterns are constant brightness levels; duration temporarily restores the
    previous brightness when complete or cancelled.
    """

    PATTERN_LEVELS = {"idle": 0.10, "attention": 0.60, "success": 1.0, "warning": 0.80}
    _NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")

    def __init__(self, led_name: str, *, root: str | Path = "/sys/class/leds"):
        if not isinstance(led_name, str) or not self._NAME_RE.fullmatch(led_name) or led_name in {".", ".."} or ".." in led_name:
            raise ValueError("led_name must be a simple sysfs LED name, not a path")
        self.led_name = led_name
        self.root = Path(root)
        self.led_dir = self.root / led_name
        self.brightness_path = self.led_dir / "brightness"
        self.max_brightness_path = self.led_dir / "max_brightness"

    def _read(self, path: Path) -> int:
        try:
            return int(path.read_text(encoding="ascii").strip())
        except FileNotFoundError as exc:
            raise ActionDriverError("LIGHT_DRIVER_UNAVAILABLE", f"missing sysfs node: {path.name}") from exc
        except PermissionError as exc:
            raise ActionDriverError("LIGHT_PERMISSION_DENIED", "cannot read LED sysfs node") from exc
        except (OSError, ValueError) as exc:
            raise ActionDriverError("LIGHT_READ_FAILED", "cannot read LED sysfs node") from exc

    def _write(self, value: int) -> None:
        try:
            self.brightness_path.write_text(f"{value}\n", encoding="ascii")
        except PermissionError as exc:
            raise ActionDriverError("LIGHT_PERMISSION_DENIED", "cannot write LED brightness") from exc
        except OSError as exc:
            raise ActionDriverError("LIGHT_WRITE_FAILED", "cannot write LED brightness", outcome_unknown=True) from exc

    async def set_pattern(
        self, action_id: str, pattern: str, duration_s: float | None, cancel_event: asyncio.Event
    ) -> Mapping[str, Any]:
        if pattern not in self.PATTERN_LEVELS:
            raise ActionDriverError("INVALID_ACTION_ARGS", "light pattern is not allowlisted")
        max_brightness = await asyncio.to_thread(self._read, self.max_brightness_path)
        if max_brightness <= 0:
            raise ActionDriverError("LIGHT_INVALID_MAX_BRIGHTNESS", "max_brightness must be positive")
        old_brightness = await asyncio.to_thread(self._read, self.brightness_path)
        target = round(max_brightness * self.PATTERN_LEVELS[pattern])
        await asyncio.to_thread(self._write, target)
        if duration_s is not None and duration_s > 0:
            cancelled = False
            try:
                await asyncio.wait_for(cancel_event.wait(), timeout=duration_s)
                cancelled = True
            except asyncio.TimeoutError:
                pass
            finally:
                await asyncio.to_thread(self._write, old_brightness)
            if cancelled:
                raise ActionCancelled()
        elif cancel_event.is_set():
            # Restore if cancellation raced immediately after the write.
            await asyncio.to_thread(self._write, old_brightness)
            raise ActionCancelled()
        return {"pattern": pattern, "brightness": target, "max_brightness": max_brightness,
                "duration_s": duration_s, "driver": "linux_sysfs_led"}

    async def health_check(self) -> bool:
        return self.brightness_path.exists() and self.max_brightness_path.exists()


@dataclass(slots=True)
class _ActiveExecution:
    fingerprint: str
    cancel_event: asyncio.Event
    task: asyncio.Task[ActionResult]


_AUTO_DRIVER = object()


class RealActionExecutor:
    """Real local action executor with idempotency and cancellation.

    `SPEAK`, `NOTIFY` and `WAIT` are implemented locally. `OBSERVE` and `LIGHT`
    require explicit observation/light drivers. It is deliberately not the default
    executor: applications must opt in and route proposals through engine safety.
    """

    is_simulated = False
    is_replay_adapter = False

    def __init__(
        self,
        *,
        speech_driver: SpeechDriver | None | object = _AUTO_DRIVER,
        notification_driver: NotificationDriver | None | object = _AUTO_DRIVER,
        observation_provider: ObservationProvider | None = None,
        light_driver: LightDriver | None = None,
        max_text_chars: int = 2000,
        max_wait_s: float = 300.0,
        result_cache_size: int = 10000,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not isinstance(max_text_chars, int) or isinstance(max_text_chars, bool) or not 1 <= max_text_chars <= 10000:
            raise ValueError("max_text_chars must be an integer in [1,10000]")
        if not math.isfinite(max_wait_s) or not 0 <= max_wait_s <= 3600:
            raise ValueError("max_wait_s must be finite and in [0,3600]")
        if not isinstance(result_cache_size, int) or isinstance(result_cache_size, bool) or result_cache_size < 100:
            raise ValueError("result_cache_size must be an integer >= 100")
        self.speech_driver = PlatformSpeechDriver() if speech_driver is _AUTO_DRIVER else speech_driver
        self.notification_driver = DesktopNotificationDriver() if notification_driver is _AUTO_DRIVER else notification_driver
        self.observation_provider = observation_provider
        self.light_driver = light_driver
        self.max_text_chars = max_text_chars
        self.max_wait_s = float(max_wait_s)
        self.result_cache_size = result_cache_size
        self.clock = clock
        self._lock = asyncio.Lock()
        self._active: dict[str, _ActiveExecution] = {}
        self._results: OrderedDict[str, tuple[str, ActionResult]] = OrderedDict()
        self._closed = False
        self._aclose_done = False

    @property
    def available_action_types(self) -> frozenset[str]:
        available = {"WAIT"}
        if self.speech_driver is not None:
            available.add("SPEAK")
        if self.notification_driver is not None:
            available.add("NOTIFY")
        if self.observation_provider is not None:
            available.add("OBSERVE")
        if self.light_driver is not None:
            available.add("LIGHT")
        return frozenset(available)

    @staticmethod
    def _fingerprint(proposal: ActionProposal) -> str:
        canonical = json.dumps({
            "intent_id": proposal.intent_id,
            "action_type": proposal.action_type,
            "args": thaw(proposal.args),
            "ordinary_proactive": proposal.ordinary_proactive,
            "reason_code": proposal.reason_code,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def execute(self, proposal: ActionProposal) -> ActionResult:
        fingerprint = self._fingerprint(proposal)
        owner = False
        async with self._lock:
            if self._closed:
                return self._result(proposal, ActionStatus.FAILED, "EXECUTOR_CLOSED")
            cached = self._results.get(proposal.action_id)
            if cached is not None:
                self._results.move_to_end(proposal.action_id)
                if cached[0] != fingerprint:
                    return self._result(proposal, ActionStatus.FAILED, "ACTION_ID_COLLISION")
                return cached[1]
            active = self._active.get(proposal.action_id)
            if active is not None:
                if active.fingerprint != fingerprint:
                    return self._result(proposal, ActionStatus.FAILED, "ACTION_ID_COLLISION")
                task = active.task
            else:
                owner = True
                cancel_event = asyncio.Event()
                task = asyncio.create_task(self._perform_and_record(proposal, fingerprint, cancel_event))
                self._active[proposal.action_id] = _ActiveExecution(fingerprint, cancel_event, task)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if owner:
                await self.cancel(proposal.action_id)
                # Let adapters observe cancellation and stop child processes before the
                # caller sees cancellation. Do not block indefinitely on a faulty plugin.
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
                except BaseException:
                    pass
            raise

    async def _perform_and_record(
        self, proposal: ActionProposal, fingerprint: str, cancel_event: asyncio.Event
    ) -> ActionResult:
        try:
            result = await self._dispatch(proposal, cancel_event)
        except ActionCancelled:
            result = self._result(proposal, ActionStatus.CANCELLED, "CANCELLED")
        except ActionDriverError as exc:
            status = ActionStatus.UNKNOWN if exc.outcome_unknown else ActionStatus.FAILED
            result = self._result(proposal, status, exc.code)
        except asyncio.CancelledError:
            cancel_event.set()
            result = self._result(proposal, ActionStatus.UNKNOWN, "EXECUTION_TASK_CANCELLED")
        except Exception as exc:
            # An unexpected exception may occur after an external side effect began.
            result = self._result(proposal, ActionStatus.UNKNOWN, f"DRIVER_UNEXPECTED_{type(exc).__name__}")
        async with self._lock:
            self._active.pop(proposal.action_id, None)
            self._results[proposal.action_id] = (fingerprint, result)
            self._results.move_to_end(proposal.action_id)
            while len(self._results) > self.result_cache_size:
                self._results.popitem(last=False)
        return result

    def _result(
        self, proposal: ActionProposal, status: ActionStatus, error_code: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> ActionResult:
        return ActionResult(
            action_id=proposal.action_id, intent_id=proposal.intent_id, status=status,
            completed_at_mono=self.clock(), error_code=error_code, details=dict(details or {}),
        )

    @staticmethod
    def _validate_args(proposal: ActionProposal, max_text_chars: int, max_wait_s: float) -> dict[str, Any]:
        args = thaw(proposal.args)
        schemas: dict[str, tuple[set[str], set[str]]] = {
            "SPEAK": ({"text"}, {"text", "mode"}),
            "NOTIFY": ({"text"}, {"text", "title"}),
            "OBSERVE": ({"key"}, {"key", "reason"}),
            "WAIT": ({"duration_s"}, {"duration_s"}),
            "LIGHT": ({"pattern"}, {"pattern", "duration_s"}),
        }
        schema = schemas.get(proposal.action_type)
        if schema is None:
            raise ActionDriverError("ACTION_NOT_SUPPORTED", f"unsupported action type {proposal.action_type}")
        required, allowed = schema
        if not isinstance(args, dict) or not required <= set(args) or not set(args) <= allowed:
            raise ActionDriverError("INVALID_ACTION_ARGS", f"invalid args for {proposal.action_type}")
        if proposal.action_type in {"SPEAK", "NOTIFY"}:
            text = args.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > max_text_chars:
                raise ActionDriverError("INVALID_ACTION_ARGS", "text must be non-empty and within configured length")
            if proposal.action_type == "SPEAK" and "mode" in args and args["mode"] not in {"task_plan", "local_template", "local_fallback", "llm"}:
                raise ActionDriverError("INVALID_ACTION_ARGS", "unsupported SPEAK mode")
            if proposal.action_type == "NOTIFY":
                title = args.get("title", "Continuity Engine")
                if not isinstance(title, str) or not title.strip() or len(title) > 100:
                    raise ActionDriverError("INVALID_ACTION_ARGS", "notification title must contain 1..100 characters")
                args["title"] = title
        elif proposal.action_type == "OBSERVE":
            if not isinstance(args["key"], str) or not args["key"] or len(args["key"]) > 128:
                raise ActionDriverError("INVALID_ACTION_ARGS", "observation key is invalid")
            reason = args.get("reason", "")
            if not isinstance(reason, str) or len(reason) > 200:
                raise ActionDriverError("INVALID_ACTION_ARGS", "observation reason must be <= 200 characters")
        elif proposal.action_type == "WAIT":
            duration = args["duration_s"]
            if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(float(duration)) or not 0 <= float(duration) <= max_wait_s:
                raise ActionDriverError("INVALID_ACTION_ARGS", f"duration_s must be finite and in [0,{max_wait_s}]")
            args["duration_s"] = float(duration)
        elif proposal.action_type == "LIGHT":
            if args["pattern"] not in SysfsLedDriver.PATTERN_LEVELS:
                raise ActionDriverError("INVALID_ACTION_ARGS", "light pattern is not allowlisted")
            if "duration_s" in args:
                duration = args["duration_s"]
                if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(float(duration)) or not 0 <= float(duration) <= 60:
                    raise ActionDriverError("INVALID_ACTION_ARGS", "light duration_s must be finite and in [0,60]")
                args["duration_s"] = float(duration)
        validate_json_compatible(args, "action.args")
        return args

    async def _dispatch(self, proposal: ActionProposal, cancel_event: asyncio.Event) -> ActionResult:
        args = self._validate_args(proposal, self.max_text_chars, self.max_wait_s)
        if cancel_event.is_set():
            raise ActionCancelled()
        action_type = proposal.action_type
        if action_type == "SPEAK":
            if self.speech_driver is None:
                raise ActionDriverError("DRIVER_NOT_CONFIGURED")
            details = await self.speech_driver.speak(proposal.action_id, args["text"], cancel_event)
            return self._result(proposal, ActionStatus.SUCCEEDED, details=details)
        if action_type == "NOTIFY":
            if self.notification_driver is None:
                raise ActionDriverError("DRIVER_NOT_CONFIGURED")
            details = await self.notification_driver.notify(
                proposal.action_id, args["title"], args["text"], cancel_event
            )
            return self._result(proposal, ActionStatus.SUCCEEDED, details=details)
        if action_type == "OBSERVE":
            if self.observation_provider is None:
                raise ActionDriverError("DRIVER_NOT_CONFIGURED", "no sensor observation provider is configured")
            observation = await self.observation_provider.observe(args["key"], args.get("reason", ""), cancel_event)
            if cancel_event.is_set():
                raise ActionCancelled()
            if not isinstance(observation, Mapping) or set(observation) - {"value", "confidence", "ttl_s"} or "value" not in observation:
                raise ActionDriverError("OBSERVATION_INVALID", "observation must contain value and optional confidence/ttl_s")
            confidence = observation.get("confidence")
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(float(confidence)) or not 0 <= float(confidence) <= 1:
                raise ActionDriverError("OBSERVATION_INVALID", "confidence must be finite in [0,1]")
            ttl = observation.get("ttl_s")
            if ttl is not None and (isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not math.isfinite(float(ttl)) or not 0.1 <= float(ttl) <= 3600):
                raise ActionDriverError("OBSERVATION_INVALID", "ttl_s must be finite in [0.1,3600]")
            clean = {"value": thaw(observation["value"]), "confidence": float(confidence)}
            if ttl is not None:
                clean["ttl_s"] = float(ttl)
            validate_json_compatible(clean, "observation")
            return self._result(proposal, ActionStatus.SUCCEEDED, details={"observation": clean})
        if action_type == "WAIT":
            duration = args["duration_s"]
            try:
                await asyncio.wait_for(cancel_event.wait(), timeout=duration)
                raise ActionCancelled()
            except asyncio.TimeoutError:
                if cancel_event.is_set():
                    raise ActionCancelled()
                return self._result(proposal, ActionStatus.SUCCEEDED, details={"waited_s": duration})
        if action_type == "LIGHT":
            if self.light_driver is None:
                raise ActionDriverError("DRIVER_NOT_CONFIGURED", "no LED/light driver is configured")
            details = await self.light_driver.set_pattern(
                proposal.action_id, args["pattern"], args.get("duration_s"), cancel_event
            )
            return self._result(proposal, ActionStatus.SUCCEEDED, details=details)
        raise ActionDriverError("ACTION_NOT_SUPPORTED")

    async def cancel(self, action_id: str) -> bool:
        async with self._lock:
            active = self._active.get(action_id)
            if active is None or active.task.done():
                return False
            active.cancel_event.set()
            return True

    async def health_check(self) -> bool:
        checks: list[Awaitable[bool]] = []
        for driver in (self.speech_driver, self.notification_driver, self.observation_provider, self.light_driver):
            if driver is None:
                continue
            check = getattr(driver, "health_check", None)
            if callable(check):
                checks.append(check())
        if not checks:
            return True  # WAIT remains available without external drivers.
        results = await asyncio.gather(*checks, return_exceptions=True)
        return all(result is True for result in results)

    async def aclose(self) -> None:
        if self._aclose_done:
            return
        self._closed = True
        async with self._lock:
            active = list(self._active.values())
            for execution in active:
                execution.cancel_event.set()
            tasks = [execution.task for execution in active]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=2.0)
            if pending:
                # A broken third-party driver must not make orderly shutdown hang
                # forever. Cancellation still gets a chance to run its cleanup.
                for task in pending:
                    task.cancel()
                _, stubborn = await asyncio.wait(pending, timeout=1.0)
                for task in stubborn:
                    task.cancel()
        for driver in (self.speech_driver, self.notification_driver, self.observation_provider, self.light_driver):
            close = getattr(driver, "close", None) if driver is not None else None
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result
        self._aclose_done = True

    def close(self) -> None:
        """Best-effort synchronous shutdown; prefer `await aclose()` in async hosts."""
        if self._aclose_done:
            return
        self._closed = True
        for execution in list(self._active.values()):
            execution.cancel_event.set()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.aclose())
        else:
            loop.create_task(self.aclose())


def action_executor_from_config(
    config: Mapping[str, Any],
    *,
    observation_provider: ObservationProvider | None = None,
    light_driver: LightDriver | None = None,
) -> Any:
    """Build an executor from `action_execution` config.

    The checked-in baseline remains `mode: simulated`; switching to `real` is an
    explicit opt-in. On real mode, native speech/notification drivers are enabled
    only when configured. OBSERVE requires a provider injected by the sensor host;
    LIGHT can use an injected driver or an explicitly configured Linux sysfs LED.
    """
    from .actions import SimulatedActionExecutor

    section = config.get("action_execution", {})
    if not isinstance(section, Mapping):
        raise ValueError("action_execution config must be a mapping")
    mode = section.get("mode", "simulated")
    if mode == "simulated":
        return SimulatedActionExecutor()
    if mode != "real":
        raise ValueError("action_execution.mode must be 'simulated' or 'real'")
    speech_cfg = section.get("speech", {})
    notify_cfg = section.get("notification", {})
    light_cfg = section.get("sysfs_led", {})
    speech_driver: SpeechDriver | None = None
    notification_driver: NotificationDriver | None = None
    if speech_cfg.get("enabled", True):
        speech_driver = PlatformSpeechDriver(rate_wpm=int(speech_cfg.get("rate_wpm", 180)))
    if notify_cfg.get("enabled", True):
        notification_driver = DesktopNotificationDriver(display_seconds=int(notify_cfg.get("display_seconds", 4)))
    if light_driver is None and light_cfg.get("enabled", False):
        led_name = light_cfg.get("led_name")
        if not isinstance(led_name, str) or not led_name:
            raise ValueError("action_execution.sysfs_led.led_name is required when enabled")
        light_driver = SysfsLedDriver(led_name, root=light_cfg.get("root", "/sys/class/leds"))
    return RealActionExecutor(
        speech_driver=speech_driver,
        notification_driver=notification_driver,
        observation_provider=observation_provider,
        light_driver=light_driver,
        max_text_chars=int(section.get("max_text_chars", 2000)),
        max_wait_s=float(section.get("max_wait_s", 300.0)),
        result_cache_size=int(section.get("result_cache_size", 10000)),
    )
