from __future__ import annotations
import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from continuity_engine import (
    CompanionService, CompanionStatus, ContinuityEngine, LLMGateway,
    ReplayClock, ReplayActionExecutor, SQLiteRepository, ReplayLLMAdapter,
    EmotionContinuity, MemoryLifecycle,
)


def build_engine(config_manager, *, fixtures=None):
    adapter = ReplayLLMAdapter(fixtures or {
        "simple_response": {"response_text": "我在听。"},
        "complex_dialogue": {"response_text": "你之前提到这件事。"},
    })
    gateway = LLMGateway(adapter, timeout_s=1.0,
                         context_token_budgets=config_manager.get()["llm"]["context_token_budgets"])
    engine = ContinuityEngine(config_manager, clock=ReplayClock(mono=10.0, wall=1_800_000_000),
                              action_executor=ReplayActionExecutor(), llm_gateway=gateway, replay_mode=True)
    return engine, adapter


@pytest.mark.asyncio
async def test_companion_single_turn_and_second_turn_uses_context(config_manager):
    engine, adapter = build_engine(config_manager)
    service = CompanionService(engine)
    try:
        first = await service.chat("你好", interaction_id="session")
        assert first.status == CompanionStatus.COMPLETED
        assert first.response_text == "我在听。"
        second = await service.chat("你还记得吗？", interaction_id="session")
        assert second.status == CompanionStatus.COMPLETED
        assert second.response_text == "你之前提到这件事。"
        assert [x.purpose for x in adapter.calls] == ["simple_response", "complex_dialogue"]
        assert len(service.history("session")) == 4
    finally:
        engine.close()


@pytest.mark.asyncio
async def test_companion_control_is_deterministic_and_never_calls_llm(config_manager):
    engine, adapter = build_engine(config_manager)
    service = CompanionService(engine)
    try:
        result = await service.chat("静音", interaction_id="session")
        assert result.status == CompanionStatus.CONTROL_ACCEPTED
        assert result.metadata["control_kind"] == "USER_MUTE"
        assert adapter.calls == []
        # The control latch is effective even before the engine tick consumes the event.
        response = await service.chat("你好", interaction_id="session")
        assert response.status == CompanionStatus.SILENCED
        assert response.error_code == "OUTPUT_MUTED"
        assert adapter.calls == []
    finally:
        engine.close()


@pytest.mark.asyncio
async def test_explicit_memory_saved_once_and_raw_text_not_put_in_engine_snapshot(config_manager):
    engine, adapter = build_engine(config_manager)
    repository = SQLiteRepository(":memory:")
    engine.attach_memory_repository(repository)
    service = CompanionService(engine)
    try:
        result = await service.chat("记住我喜欢天文学", interaction_id="session")
        assert result.status == CompanionStatus.MEMORY_SAVED
        assert result.memory_id
        rows = repository.list_memories()
        assert len(rows) == 1
        assert "天文学" in rows[0]["payload"]["summary"]
        engine.step(dt=0.0)
        assert engine.state.last_user_message is None
        assert len(repository.list_memories()) == 1
        assert adapter.calls == []
    finally:
        engine.close(); repository.close()


@pytest.mark.asyncio
async def test_memory_heuristics_require_approval(config_manager):
    engine, adapter = build_engine(config_manager)
    repository = SQLiteRepository(":memory:")
    engine.attach_memory_repository(repository)
    service = CompanionService(engine)
    try:
        result = await service.chat("我喜欢猫，也常常看天文书", interaction_id="session")
        assert result.memory_candidate_id
        assert repository.list_memories() == []
        memory_id = service.approve_memory_candidate(result.memory_candidate_id)
        assert repository.list_memories()[0]["memory_id"] == memory_id
        assert service.recall_memories("猫")
    finally:
        engine.close(); repository.close()


@pytest.mark.asyncio
async def test_secret_like_explicit_memory_never_reaches_model_or_session_history(config_manager):
    engine, adapter = build_engine(config_manager)
    repository = SQLiteRepository(":memory:")
    engine.attach_memory_repository(repository)
    service = CompanionService(engine)
    secret_text = "记住我的密码是 hunter2"
    try:
        result = await service.chat(secret_text, interaction_id="session")
        assert result.status == CompanionStatus.COMPLETED
        assert "不会保存" in result.response_text
        assert "hunter2" not in str(service.history("session"))
        assert "hunter2" not in str([e.payload for e in engine.router._normal])
        assert repository.list_memories() == []
        assert adapter.calls == []
    finally:
        engine.close(); repository.close()


def test_emotion_continuity_is_bounded_and_does_not_infer_from_silence():
    clock_value = [100.0]
    emotion = EmotionContinuity(decay_tau_s=10.0, clock=lambda: clock_value[0])
    observed = emotion.observe("我最近压力大", now=100.0)
    assert observed.label == "negative"
    assert observed.confidence >= 0.6
    clock_value[0] = 1000.0
    decayed = emotion.current(now=1000.0)
    assert decayed.confidence < observed.confidence
    # Calling current without any new message only decays the estimate; it does not
    # create a new negative state from the absence of interaction.
    assert decayed.confidence < 0.1
    assert emotion.strategy() == "gentle_acknowledgment"


def test_emotion_strategy_respects_explicit_listening_preference():
    emotion = EmotionContinuity(clock=lambda: 1.0)
    emotion.observe("我只想说说，不需要建议", now=1.0)
    assert emotion.strategy() == "listen_first"
    assert "不要立刻列解决步骤" in emotion.guidance()


def test_emotion_assessment_rejects_low_confidence_or_unknown_labels():
    emotion = EmotionContinuity(clock=lambda: 5.0)
    before = emotion.current(now=5.0)
    result = emotion.observe("没有明显线索", model_assessment={"emotion": "sad", "confidence": 0.2}, now=5.0)
    assert result.label is None
    result = emotion.observe("没有明显线索", model_assessment={"emotion": "diagnostic_label", "confidence": 0.99}, now=5.0)
    assert result.label is None
    assert result.confidence == before.confidence

@pytest.mark.asyncio
async def test_same_call_emotion_assessment_is_accepted_and_used_for_next_turn(config_manager):
    engine, adapter = build_engine(config_manager, fixtures={
        "simple_response": {"response_text": "听起来今天有些不容易。", "emotion_assessment": {
            "emotion": "sadness", "confidence": 0.88, "evidence": ["用户直接表达了低落"]}},
    })
    service = CompanionService(engine)
    try:
        result = await service.chat("我不太想说话", interaction_id="session")
        assert result.status == CompanionStatus.COMPLETED
        assert result.metadata["emotion_estimate"]["label"] == "sadness"
        assert result.metadata["emotion_estimate"]["confidence"] == pytest.approx(.88)
    finally:
        engine.close()


@pytest.mark.asyncio
async def test_stop_invalidates_inflight_dialogue_even_after_control_latch_is_consumed(config_manager):
    adapter = ReplayLLMAdapter({"simple_response": {"response_text": "迟到的回答不应输出。"}}, delay_s=0.15)
    gateway = LLMGateway(adapter, timeout_s=1,
                         context_token_budgets=config_manager.get()["llm"]["context_token_budgets"])
    engine = ContinuityEngine(config_manager, clock=ReplayClock(mono=2, wall=1_800_000_000),
                              action_executor=ReplayActionExecutor(), llm_gateway=gateway, replay_mode=True)
    service = CompanionService(engine)
    try:
        pending = asyncio.create_task(service.chat("你好", interaction_id="session"))
        await asyncio.sleep(0.02)
        control = await service.chat("停止", interaction_id="session")
        assert control.status == CompanionStatus.CONTROL_ACCEPTED
        # Model work is invalidated at command ingress, not only while the latch exists.
        engine.step(dt=0.0)
        result = await pending
        assert result.status == CompanionStatus.STALE
        assert "迟到的回答" not in result.response_text
    finally:
        engine.close()
