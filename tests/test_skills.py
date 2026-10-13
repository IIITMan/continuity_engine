from __future__ import annotations

import asyncio

from continuity_engine import SkillCall, SkillRegistry, SkillSpec, SkillStatus


class EchoSkill:
    async def execute(self, call):
        return {"echo": call.arguments.get("text", "")}


def test_skill_permission_registration_dispatch_and_feedback():
    async def run():
        registry = SkillRegistry()
        await registry.register(SkillSpec("voice.tts", "Text-to-speech", ("audio.output",)), EchoSkill())
        denied = await registry.dispatch(SkillCall("voice.tts", {"text": "hello"}))
        assert denied.status == SkillStatus.DENIED
        assert denied.error_code == "PERMISSION_DENIED"
        ok = await registry.dispatch(SkillCall("voice.tts", {"text": "hello"}, granted_permissions=frozenset({"audio.output"})))
        assert ok.status == SkillStatus.OK
        assert ok.data == {"echo": "hello"}
        assert len(registry.feedback_snapshot()) == 2
    asyncio.run(run())


def test_skill_hot_unload_is_blocked_during_execution():
    async def run():
        started = asyncio.Event()
        finish = asyncio.Event()
        class SlowSkill:
            async def execute(self, call):
                started.set()
                await finish.wait()
                return {"done": True}
        registry = SkillRegistry()
        await registry.register(SkillSpec("test.slow", "slow test"), SlowSkill())
        task = asyncio.create_task(registry.dispatch(SkillCall("test.slow")))
        await started.wait()
        try:
            await registry.unregister("test.slow")
        except RuntimeError as exc:
            assert "active" in str(exc)
        else:
            raise AssertionError("unload must not interrupt an active execution")
        finish.set()
        assert (await task).status == SkillStatus.OK
        assert await registry.unregister("test.slow") is True
    asyncio.run(run())


def test_asr_normalization_preserves_only_explicit_emotion_metadata():
    from continuity_engine import normalize_asr_result
    result = normalize_asr_result({"text": "你好", "emotion": "happy", "emotion_confidence": 0.7, "unknown_sensor_dump": {"x": 1}})
    assert result["text"] == "你好"
    assert result["emotion"] == "happy"
    assert result["emotion_is_model_output"] is True
    assert "unknown_sensor_dump" not in result


def test_reference_skill_validates_input_and_returns_structured_result():
    import importlib.util
    from pathlib import Path
    from continuity_engine import SkillCall, SkillRegistry, SkillSpec, SkillStatus
    path = Path(__file__).resolve().parents[1] / "examples" / "skills" / "echo_skill.py"
    spec = importlib.util.spec_from_file_location("reference_echo_skill", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    async def run():
        registry = SkillRegistry()
        await registry.register(SkillSpec("example.echo", "Reference echo skill"), module.EchoSkill())
        good = await registry.dispatch(SkillCall("example.echo", {"text": "hello"}))
        assert good.status == SkillStatus.OK
        assert good.data == {"echo": "hello"}
        bad = await registry.dispatch(SkillCall("example.echo", {"text": 123}))
        assert bad.status == SkillStatus.FAILED
        assert bad.error_code == "INVALID_ARGUMENT_TEXT"
    asyncio.run(run())
