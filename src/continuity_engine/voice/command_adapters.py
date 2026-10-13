"""JSON-over-stdin subprocess adapters for user-supplied ASR/TTS bridge executables.

These are integration points, not bundled recognition/synthesis models. The configured
program must implement the documented JSON protocol. Subprocesses never use a shell.
"""
from __future__ import annotations

import asyncio
import base64
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping, Sequence


class CommandAdapterError(RuntimeError):
    pass

class _JSONCommand:
    def __init__(self, command: Sequence[str], *, timeout_s: float = 30.0, max_input_bytes: int = 16_000_000,
                 max_output_bytes: int = 2_000_000, cwd: str | None = None, env: Mapping[str, str] | None = None):
        if not command or any(not isinstance(x, str) or not x for x in command):
            raise ValueError("command must be a non-empty sequence of non-empty strings")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s must be positive and finite")
        self.command = tuple(command)
        self.timeout_s = timeout_s
        self.max_input_bytes = max_input_bytes
        self.max_output_bytes = max_output_bytes
        self.cwd = cwd
        self.env = dict(os.environ, **dict(env or {}))
        self._active_processes: set[asyncio.subprocess.Process] = set()

    async def _run(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > self.max_input_bytes:
            raise CommandAdapterError("COMMAND_INPUT_TOO_LARGE")
        try:
            proc = await asyncio.create_subprocess_exec(*self.command, cwd=self.cwd, env=self.env,
                                                        stdin=asyncio.subprocess.PIPE,
                                                        stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.PIPE)
        except (OSError, ValueError) as exc:
            raise CommandAdapterError("COMMAND_START_FAILED") from exc
        self._active_processes.add(proc)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(raw), timeout=self.timeout_s)
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.communicate()
            raise CommandAdapterError("COMMAND_TIMED_OUT") from exc
        except asyncio.CancelledError:
            if proc.returncode is None:
                proc.kill()
            await proc.communicate()
            raise
        finally:
            self._active_processes.discard(proc)
        if len(stdout) > self.max_output_bytes:
            raise CommandAdapterError("COMMAND_OUTPUT_TOO_LARGE")
        if proc.returncode != 0:
            # Do not echo stderr; it may contain private audio paths or credentials.
            raise CommandAdapterError(f"COMMAND_EXIT_{proc.returncode}")
        try:
            result = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CommandAdapterError("COMMAND_INVALID_JSON") from exc
        if not isinstance(result, dict):
            raise CommandAdapterError("COMMAND_RESULT_NOT_OBJECT")
        return result

class JsonCommandSpeechRecognizer(_JSONCommand):
    """Bridge protocol input: {"audio_base64": "...", "format": "pcm_s16le|wav|..."}; output object with text."""
    async def transcribe(self, audio: Any) -> Mapping[str, Any]:
        if isinstance(audio, (bytes, bytearray, memoryview)):
            raw = bytes(audio)
        elif isinstance(audio, str):
            # Treat strings as paths only after an explicit caller passes them; never
            # read arbitrary paths from ASR/model output. Audio buffers are recommended.
            path = Path(audio)
            raw = await asyncio.to_thread(path.read_bytes)
        else:
            raise TypeError("audio must be bytes-like or an explicit filesystem path")
        if len(raw) > self.max_input_bytes:
            raise CommandAdapterError("AUDIO_TOO_LARGE")
        return await self._run({"audio_base64": base64.b64encode(raw).decode("ascii"), "format": "unknown"})

class JsonCommandTextToSpeech(_JSONCommand):
    """Bridge protocol input: {"text": "...", "voice": null|string}; output status metadata."""
    def __init__(self, command: Sequence[str], **kwargs: Any):
        super().__init__(command, **kwargs)
        self._active: asyncio.subprocess.Process | None = None

    async def speak(self, text: str, *, voice: str | None = None) -> Mapping[str, Any]:
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError("text must contain 1..4000 characters")
        result = await self._run({"text": text, "voice": voice})
        if "status" not in result:
            result["status"] = "SUCCEEDED"
        return result

    async def stop(self) -> None:
        # Best-effort process cancellation for barge-in and user stop/mute controls.
        active = list(self._active_processes)
        for proc in active:
            if proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
        if active:
            try:
                await asyncio.wait_for(asyncio.gather(*(p.wait() for p in active), return_exceptions=True), timeout=0.5)
            except asyncio.TimeoutError:
                for proc in active:
                    if proc.returncode is None:
                        proc.kill()
                await asyncio.gather(*(p.wait() for p in active), return_exceptions=True)
