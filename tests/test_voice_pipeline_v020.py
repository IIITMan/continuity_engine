from __future__ import annotations
import asyncio
import json
import sys

import pytest

from continuity_engine import (
    CompanionService, ContinuityEngine, LLMGateway, ReplayClock, ReplayActionExecutor,
    ReplayLLMAdapter, VoiceConversationPipeline, PCM16EnergyVAD,
    JsonCommandSpeechRecognizer, JsonCommandTextToSpeech, CommandAdapterError,
)

class FakeASR:
    async def transcribe(self, audio):
        return {"text": "你好", "source": "fake_asr", "emotion_label": "happy", "emotion_confidence": 0.9}

class FakeTTS:
    def __init__(self): self.texts=[]
    async def speak(self, text, *, voice=None):
        self.texts.append((text, voice))
        return {"status": "SUCCEEDED", "chars": len(text)}
    async def stop(self): pass

@pytest.mark.asyncio
async def test_voice_pipeline_asr_dialogue_tts_full_loop(config_manager):
    adapter = ReplayLLMAdapter({"simple_response": {"response_text": "你好，我在听。"}})
    gateway = LLMGateway(adapter, timeout_s=1,
                         context_token_budgets=config_manager.get()["llm"]["context_token_budgets"])
    engine = ContinuityEngine(config_manager, clock=ReplayClock(mono=1, wall=1_800_000_000),
                              action_executor=ReplayActionExecutor(), llm_gateway=gateway, replay_mode=True)
    tts = FakeTTS()
    pipeline = VoiceConversationPipeline(CompanionService(engine), FakeASR(), tts)
    try:
        result = await pipeline.process_audio(b"fake-audio", interaction_id="voice", voice="zh")
        assert result.status == "COMPLETED"
        assert result.transcript == "你好"
        assert tts.texts and "你好" in "".join(x[0] for x in tts.texts)
        assert len(adapter.calls) == 1
    finally:
        engine.close()

@pytest.mark.asyncio
async def test_pcm16_energy_vad_detects_signal_and_rejects_short_frame():
    vad = PCM16EnergyVAD(rms_threshold=100.0, min_samples=4)
    quiet = b"\x00\x00" * 10
    loud = b"\xe8\x03" * 10  # 1000 little-endian samples
    assert not await vad.detect_speech(quiet)
    assert await vad.detect_speech(loud)
    assert not await vad.detect_speech(b"\x00\x00")

@pytest.mark.asyncio
async def test_json_command_asr_adapter_uses_stdin_json_without_shell():
    program = 'import sys,json; json.load(sys.stdin); print(json.dumps({"text":"测试语音"}, ensure_ascii=False))'
    adapter = JsonCommandSpeechRecognizer([sys.executable, "-c", program], timeout_s=2)
    result = await adapter.transcribe(b"fake pcm")
    assert result["text"] == "测试语音"

@pytest.mark.asyncio
async def test_json_command_tts_adapter_normalizes_status():
    program = 'import sys,json; x=json.load(sys.stdin); print(json.dumps({"accepted": bool(x.get("text"))}))'
    adapter = JsonCommandTextToSpeech([sys.executable, "-c", program], timeout_s=2)
    result = await adapter.speak("你好", voice="zh")
    assert result["status"] == "SUCCEEDED"
    assert result["accepted"] is True

@pytest.mark.asyncio
async def test_json_command_adapter_timeout_fails_explicitly():
    program = 'import time; time.sleep(2)'
    adapter = JsonCommandSpeechRecognizer([sys.executable, "-c", program], timeout_s=0.05)
    with pytest.raises(CommandAdapterError, match="TIMED_OUT"):
        await adapter.transcribe(b"fake pcm")

@pytest.mark.asyncio
async def test_json_command_tts_stop_terminates_active_subprocess():
    program = 'import sys,json,time; json.load(sys.stdin); time.sleep(5); print(json.dumps({"status":"SUCCEEDED"}))'
    adapter = JsonCommandTextToSpeech([sys.executable, "-c", program], timeout_s=10)
    task = asyncio.create_task(adapter.speak("一段较长语音"))
    await asyncio.sleep(0.05)
    await adapter.stop()
    with pytest.raises(CommandAdapterError):
        await task

@pytest.mark.asyncio
async def test_new_text_turn_interrupts_active_voice_output(config_manager):
    """Text barge-in must stop host-managed TTS without waiting for another model turn."""
    class BlockingTTS:
        def __init__(self):
            self.started = asyncio.Event()
            self.released = asyncio.Event()
            self.stop_calls = 0
        async def speak(self, text, *, voice=None):
            self.started.set()
            await self.released.wait()
            return {"status": "CANCELLED"}
        async def stop(self):
            self.stop_calls += 1
            self.released.set()

    adapter = ReplayLLMAdapter({
        "simple_response": {"response_text": "我会慢慢说完这一段话，然后等待你。"},
        "complex_dialogue": {"response_text": "好的，我们继续用文字聊。"},
    })
    gateway = LLMGateway(adapter, timeout_s=1,
                         context_token_budgets=config_manager.get()["llm"]["context_token_budgets"])
    engine = ContinuityEngine(config_manager, clock=ReplayClock(mono=1, wall=1_800_000_000),
                              action_executor=ReplayActionExecutor(), llm_gateway=gateway, replay_mode=True)
    tts = BlockingTTS()
    companion = CompanionService(engine)
    pipeline = VoiceConversationPipeline(companion, FakeASR(), tts)
    voice_task = asyncio.create_task(pipeline.process_audio(b"fake-audio", interaction_id="barge"))
    try:
        await asyncio.wait_for(tts.started.wait(), timeout=2)
        # This input enters the text lane but its output-interrupt callback fires synchronously.
        result = await companion.chat("继续用文字聊", interaction_id="barge", input_channel="text")
        assert result.response_text == "好的，我们继续用文字聊。"
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(voice_task, timeout=2)
        assert tts.stop_calls >= 1
    finally:
        await pipeline.aclose()
        engine.close()
