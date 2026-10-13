import asyncio
import os
import sys
from pathlib import Path

import pytest

from continuity_engine import (
    ActionProposal, ActionStatus, CallbackObservationProvider, ConfigManager,
    ContinuityEngine, EventKind, RealActionExecutor, SysfsLedDriver,
    action_executor_from_config,
)
from continuity_engine.execution.real_actions import (
    ActionCancelled, ActionDriverError, DesktopNotificationDriver,
    PlatformSpeechDriver, _ManagedProcessRunner,
)


class FakeSpeechDriver:
    def __init__(self, delay=0.0):
        self.delay = delay
        self.calls = []
        self.started = asyncio.Event()

    async def speak(self, action_id, text, cancel_event):
        self.calls.append((action_id, text))
        self.started.set()
        if self.delay:
            try:
                await asyncio.wait_for(cancel_event.wait(), self.delay)
                raise ActionCancelled()
            except asyncio.TimeoutError:
                pass
        if cancel_event.is_set():
            raise ActionCancelled()
        return {"backend": "fake", "character_count": len(text)}

    async def health_check(self):
        return True


class FakeNotificationDriver:
    def __init__(self):
        self.calls = []

    async def notify(self, action_id, title, text, cancel_event):
        self.calls.append((action_id, title, text))
        if cancel_event.is_set():
            raise ActionCancelled()
        return {"backend": "fake-notification"}

    async def health_check(self):
        return True


class FakeLightDriver:
    async def set_pattern(self, action_id, pattern, duration_s, cancel_event):
        if cancel_event.is_set():
            raise ActionCancelled()
        return {"pattern": pattern, "driver": "fake"}

    async def health_check(self):
        return True


def proposal(action_id="a1", intent_id="i1", action_type="SPEAK", args=None):
    return ActionProposal(
        action_id=action_id,
        intent_id=intent_id,
        action_type=action_type,
        args=args if args is not None else {"text": "hello"},
        created_at_mono=1.0,
    )


@pytest.mark.asyncio
async def test_real_executor_executes_speech_driver_and_caches_idempotently():
    speech = FakeSpeechDriver()
    executor = RealActionExecutor(speech_driver=speech, notification_driver=None)
    first = await executor.execute(proposal())
    second = await executor.execute(proposal())
    assert first.status == ActionStatus.SUCCEEDED
    assert second == first
    assert speech.calls == [("a1", "hello")]
    await executor.aclose()


@pytest.mark.asyncio
async def test_concurrent_duplicate_action_id_executes_only_once():
    speech = FakeSpeechDriver(delay=0.05)
    executor = RealActionExecutor(speech_driver=speech, notification_driver=None)
    first, second = await asyncio.gather(executor.execute(proposal()), executor.execute(proposal()))
    assert first == second
    assert first.status == ActionStatus.SUCCEEDED
    assert len(speech.calls) == 1
    await executor.aclose()


@pytest.mark.asyncio
async def test_reusing_action_id_for_different_payload_is_rejected():
    executor = RealActionExecutor(speech_driver=FakeSpeechDriver(), notification_driver=None)
    ok = await executor.execute(proposal())
    collision = await executor.execute(proposal(args={"text": "different"}))
    assert ok.status == ActionStatus.SUCCEEDED
    assert collision.status == ActionStatus.FAILED
    assert collision.error_code == "ACTION_ID_COLLISION"
    await executor.aclose()


@pytest.mark.asyncio
async def test_wait_is_a_real_cancellable_delay():
    executor = RealActionExecutor(speech_driver=None, notification_driver=None)
    p = proposal(action_type="WAIT", args={"duration_s": 5.0})
    task = asyncio.create_task(executor.execute(p))
    await asyncio.sleep(0.02)
    assert await executor.cancel("a1") is True
    result = await task
    assert result.status == ActionStatus.CANCELLED
    await executor.aclose()


@pytest.mark.asyncio
async def test_wait_completes_after_requested_duration():
    executor = RealActionExecutor(speech_driver=None, notification_driver=None)
    result = await executor.execute(proposal(action_type="WAIT", args={"duration_s": 0.01}))
    assert result.status == ActionStatus.SUCCEEDED
    assert result.details["waited_s"] == pytest.approx(0.01)
    await executor.aclose()


@pytest.mark.asyncio
async def test_unconfigured_observe_or_light_fails_closed():
    executor = RealActionExecutor(speech_driver=None, notification_driver=None)
    observe = await executor.execute(proposal(action_type="OBSERVE", args={"key": "room_activity"}))
    light = await executor.execute(proposal(action_id="a2", action_type="LIGHT", args={"pattern": "success"}))
    assert observe.status == ActionStatus.FAILED and observe.error_code == "DRIVER_NOT_CONFIGURED"
    assert light.status == ActionStatus.FAILED and light.error_code == "DRIVER_NOT_CONFIGURED"
    await executor.aclose()


@pytest.mark.asyncio
async def test_observation_provider_returns_validated_world_evidence():
    provider = CallbackObservationProvider(lambda key, reason: {"value": {"activity": "quiet"}, "confidence": 0.91, "ttl_s": 3.0})
    executor = RealActionExecutor(speech_driver=None, notification_driver=None, observation_provider=provider)
    result = await executor.execute(proposal(action_type="OBSERVE", args={"key": "room_activity", "reason": "stale"}))
    assert result.status == ActionStatus.SUCCEEDED
    assert result.details["observation"]["confidence"] == pytest.approx(0.91)
    await executor.aclose()


@pytest.mark.asyncio
async def test_invalid_observation_from_sensor_is_rejected():
    provider = CallbackObservationProvider(lambda key, reason: {"value": "maybe", "confidence": 4.0})
    executor = RealActionExecutor(speech_driver=None, notification_driver=None, observation_provider=provider)
    result = await executor.execute(proposal(action_type="OBSERVE", args={"key": "room_activity"}))
    assert result.status == ActionStatus.FAILED
    assert result.error_code == "OBSERVATION_INVALID"
    await executor.aclose()


@pytest.mark.asyncio
async def test_invalid_action_args_do_not_call_driver():
    speech = FakeSpeechDriver()
    executor = RealActionExecutor(speech_driver=speech, notification_driver=None)
    result = await executor.execute(proposal(args={"text": "hello", "shell": "rm -rf /"}))
    assert result.status == ActionStatus.FAILED
    assert result.error_code == "INVALID_ACTION_ARGS"
    assert speech.calls == []
    await executor.aclose()


@pytest.mark.asyncio
async def test_real_executor_wired_through_engine_safety_path(config_manager):
    speech = FakeSpeechDriver()
    executor = RealActionExecutor(speech_driver=speech, notification_driver=None)
    engine = ContinuityEngine(config_manager, action_executor=executor)
    try:
        engine.inject(engine.new_event(EventKind.USER_MESSAGE.value, {"text": "hello robot"}, source="test"))
        step = engine.step(dt=0.0)
        assert len(step.proposals) == 1
        safety, result = await engine.dispatch_action(step.proposals[0])
        assert safety.outcome == "ALLOW"
        assert result.status == ActionStatus.SUCCEEDED
        assert len(speech.calls) == 1
    finally:
        await executor.aclose()
        engine.close()


@pytest.mark.asyncio
async def test_engine_mute_guard_prevents_real_speech_driver(config_manager):
    speech = FakeSpeechDriver()
    executor = RealActionExecutor(speech_driver=speech, notification_driver=None)
    engine = ContinuityEngine(config_manager, action_executor=executor)
    try:
        engine.inject(engine.new_event(EventKind.USER_MESSAGE.value, {"text": "hello robot"}, source="test"))
        step = engine.step(dt=0.0)
        engine.inject(engine.new_event(EventKind.USER_MUTE.value, {}, source="button"))
        engine.step(dt=0.0)
        decision, result = await engine.dispatch_action(step.proposals[0])
        assert decision.outcome == "DENY_HARD"
        assert result is None
        assert speech.calls == []
    finally:
        await executor.aclose()
        engine.close()


def test_sysfs_led_driver_writes_and_restores_brightness(tmp_path):
    async def run():
        led_dir = tmp_path / "led0"
        led_dir.mkdir()
        (led_dir / "max_brightness").write_text("100\n")
        (led_dir / "brightness").write_text("12\n")
        driver = SysfsLedDriver("led0", root=tmp_path)
        cancel_event = asyncio.Event()
        result = await driver.set_pattern("light1", "success", 0.02, cancel_event)
        assert result["brightness"] == 100
        assert (led_dir / "brightness").read_text().strip() == "12"
        assert await driver.health_check()
    asyncio.run(run())


def test_sysfs_led_rejects_paths_as_led_names(tmp_path):
    with pytest.raises(ValueError):
        SysfsLedDriver("../brightness", root=tmp_path)


def test_sysfs_led_reports_missing_hardware(tmp_path):
    async def run():
        driver = SysfsLedDriver("missing", root=tmp_path)
        result = await driver.set_pattern("light1", "attention", None, asyncio.Event())
        return result
    with pytest.raises(ActionDriverError) as exc:
        asyncio.run(run())
    assert exc.value.code == "LIGHT_DRIVER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_managed_subprocess_cancellation_terminates_child_process():
    runner = _ManagedProcessRunner(terminate_grace_s=0.05)
    cancel_event = asyncio.Event()
    async def run_child():
        return await runner.run(
            "process1", [sys.executable, "-c", "import time; time.sleep(30)"],
            cancel_event=cancel_event,
        )
    task = asyncio.create_task(run_child())
    await asyncio.sleep(0.05)
    cancel_event.set()
    with pytest.raises(ActionCancelled):
        await asyncio.wait_for(task, 2.0)
    assert "process1" not in runner._processes
    await runner.close()


def test_platform_speech_passes_text_as_argv_or_stdin_not_shell(monkeypatch):
    monkeypatch.setattr("continuity_engine.execution.real_actions.sys.platform", "linux")
    monkeypatch.setattr("continuity_engine.execution.real_actions.shutil.which", lambda name: "/usr/bin/espeak-ng" if name == "espeak-ng" else None)
    driver = PlatformSpeechDriver()
    text = "hello; $(touch SHOULD_NOT_EXIST)"
    command, stdin_data, backend = driver.command_for(text)
    assert command == ["/usr/bin/espeak-ng", "--stdin", "-s", "180"]
    assert stdin_data == text.encode("utf-8")
    assert backend == "espeak-ng"
    assert "shell" not in " ".join(command).lower()


def test_macos_notification_quotes_strings_for_applescript(monkeypatch):
    monkeypatch.setattr("continuity_engine.execution.real_actions.sys.platform", "darwin")
    monkeypatch.setattr("continuity_engine.execution.real_actions.Path.exists", lambda self: str(self) == "/usr/bin/osascript")
    driver = DesktopNotificationDriver()
    command, stdin_data, backend = driver.command_for('title " test', 'x"; do shell script "whoami"')
    assert command[0] == "/usr/bin/osascript"
    assert stdin_data is None
    assert backend == "osascript"
    assert '\\"' in command[-1]
    assert "do shell script" in command[-1]  # string stays data inside a quoted AppleScript literal


def test_config_factory_defaults_to_simulated_and_requires_explicit_real_mode(config_manager):
    from continuity_engine import SimulatedActionExecutor
    cfg = config_manager.get()
    default_executor = action_executor_from_config(cfg)
    assert isinstance(default_executor, SimulatedActionExecutor)
    cfg["action_execution"] = {
        "mode": "real",
        "speech": {"enabled": False},
        "notification": {"enabled": False},
        "sysfs_led": {"enabled": False},
    }
    real = action_executor_from_config(cfg)
    assert isinstance(real, RealActionExecutor)
    assert real.available_action_types == frozenset({"WAIT"})
    asyncio.run(real.aclose())


def test_windows_speech_command_encodes_text_not_shell_code(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr("continuity_engine.execution.real_actions.os", SimpleNamespace(name="nt"))
    monkeypatch.setattr("continuity_engine.execution.real_actions.sys.platform", "win32")
    monkeypatch.setattr(
        "continuity_engine.execution.real_actions.shutil.which",
        lambda name: r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" if name in {"powershell.exe", "powershell"} else None,
    )
    driver = PlatformSpeechDriver()
    dangerous_looking_text = 'say "hello"; Start-Process calc.exe'
    command, stdin_data, backend = driver.command_for(dangerous_looking_text)
    assert command[-2] == "-EncodedCommand"
    assert dangerous_looking_text not in " ".join(command)
    assert stdin_data is None
    assert backend == "System.Speech"


def test_windows_notification_command_encodes_user_content(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr("continuity_engine.execution.real_actions.os", SimpleNamespace(name="nt"))
    monkeypatch.setattr("continuity_engine.execution.real_actions.sys.platform", "win32")
    monkeypatch.setattr(
        "continuity_engine.execution.real_actions.shutil.which",
        lambda name: r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" if name in {"powershell.exe", "powershell"} else None,
    )
    command, stdin_data, backend = DesktopNotificationDriver().command_for("safe title", '"; Invoke-Expression "bad"')
    assert command[-2] == "-EncodedCommand"
    assert 'Invoke-Expression "bad"' not in " ".join(command)
    assert stdin_data is None
    assert backend == "Windows.Forms.NotifyIcon"


@pytest.mark.asyncio
async def test_cancelled_observation_cancels_awaited_sensor_callback():
    callback_started = asyncio.Event()
    callback_cancelled = asyncio.Event()

    async def sensor_callback(key, reason):
        callback_started.set()
        try:
            await asyncio.sleep(30)
            return {"value": "late", "confidence": 0.8}
        finally:
            callback_cancelled.set()

    provider = CallbackObservationProvider(sensor_callback)
    executor = RealActionExecutor(
        speech_driver=None, notification_driver=None, observation_provider=provider
    )
    task = asyncio.create_task(executor.execute(proposal(action_type="OBSERVE", args={"key": "room_activity"})))
    await asyncio.wait_for(callback_started.wait(), 1.0)
    assert await executor.cancel("a1") is True
    result = await asyncio.wait_for(task, 1.0)
    assert result.status == ActionStatus.CANCELLED
    assert callback_cancelled.is_set()
    await executor.aclose()


def test_config_validation_rejects_unknown_action_executor_mode(config_manager):
    from continuity_engine import ConfigError
    cfg = config_manager.get()
    cfg["action_execution"]["mode"] = "realish"
    with pytest.raises(ConfigError, match="action_execution.mode"):
        ConfigManager(cfg)


def test_config_rejects_unknown_native_backend(config_manager):
    from continuity_engine import ConfigError
    cfg = config_manager.get()
    cfg["action_execution"]["speech"]["backend"] = "shell-command"
    with pytest.raises(ConfigError, match="speech.backend"):
        ConfigManager(cfg)


@pytest.mark.asyncio
async def test_user_barge_in_cancels_running_real_speech(config_manager):
    speech = FakeSpeechDriver(delay=10.0)
    executor = RealActionExecutor(speech_driver=speech, notification_driver=None)
    engine = ContinuityEngine(config_manager, action_executor=executor)
    try:
        engine.inject(engine.new_event(EventKind.USER_MESSAGE.value, {"text": "hello robot"}, source="test"))
        step = engine.step(dt=0.0)
        dispatch = asyncio.create_task(engine.dispatch_action(step.proposals[0]))
        await asyncio.wait_for(speech.started.wait(), 1.0)
        engine.inject(engine.new_event(EventKind.USER_SPEECH_STARTED.value, {}, source="vad"))
        # Ingress schedules the executor cancellation immediately; the next core step
        # also consumes the protected control latch and makes the cancellation auditable.
        engine.step(dt=0.0)
        safety, result = await asyncio.wait_for(dispatch, 1.0)
        assert safety.outcome == "ALLOW"
        assert result.status == ActionStatus.CANCELLED
    finally:
        await engine.aclose()
