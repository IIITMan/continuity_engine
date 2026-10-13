from __future__ import annotations

import json

import pytest

from continuity_engine import ConfigManager, ContinuityEngine, InteractionState, InteractionStateError
from continuity_engine.llm.openai_compatible import _interaction_strategy_from_context
from continuity_engine.core.models import EngineState
from continuity_engine.llm.state_views import StateViewBuilder


def test_signals_are_bounded_replaceable_and_expire_without_raw_dialogue():
    now = [100.0]
    state = InteractionState(clock=lambda: now[0])
    state.observe_signal("user.fatigue", "possible", confidence=0.62, source="host", ttl_s=30)
    state.observe_signal("topic.current", "school", confidence=0.9, ttl_s=60)
    state.observe_signal("user.fatigue", "likely", confidence=0.8, ttl_s=30)
    snapshot = state.snapshot()
    assert len(snapshot["signals"]) == 2
    fatigue = next(x for x in snapshot["signals"] if x["name"] == "user.fatigue")
    assert fatigue["value"] == "likely"
    assert fatigue["confidence"] == 0.8
    assert snapshot["raw_dialogue_stored"] is False
    assert "observed_at_mono" not in fatigue
    now[0] += 31
    after = state.snapshot()
    assert [x["name"] for x in after["signals"]] == ["topic.current"]


def test_signal_validation_and_capacity_are_bounded():
    state = InteractionState()
    with pytest.raises(InteractionStateError):
        state.observe_signal("User Fatigue", "likely")
    with pytest.raises(InteractionStateError):
        state.observe_signal("user.fatigue", "x" * 101)
    with pytest.raises(InteractionStateError):
        state.observe_signal("user.fatigue", "likely", confidence=1.2)
    for i in range(40):
        state.observe_signal(f"signal.{i}", i, ttl_s=300)
    assert len(state.snapshot()["signals"]) == 32


def test_signals_do_not_automatically_activate_a_strategy():
    state = InteractionState()
    state.observe_signal("user.fatigue", "likely", confidence=0.8)
    assert state.snapshot()["current_strategy"] is None


def test_strategy_can_be_set_cleared_and_expire():
    now = [50.0]
    state = InteractionState(clock=lambda: now[0])
    result = state.set_strategy(
        "倾听优先", "以倾听为主，减少追问，不急于给解决方案。", ttl_s=10,
        reason_code="user_continues_sharing",
    )
    assert result["name"] == "倾听优先"
    assert result["expires_in_s"] == 10
    now[0] += 11
    assert state.snapshot()["current_strategy"] is None
    state.set_strategy("简洁回答", "先回答问题，再提供必要解释。")
    assert state.clear_strategy(reason_code="context_changed") is True
    assert state.clear_strategy() is False


def test_strategy_input_is_bounded_and_source_is_validated():
    state = InteractionState()
    with pytest.raises(InteractionStateError):
        state.set_strategy("x", "y", source="llm_auto")
    with pytest.raises(InteractionStateError):
        state.set_strategy("x", "z" * 601)
    with pytest.raises(InteractionStateError):
        state.set_strategy("x", "y", ttl_s=90_000)


def test_state_view_projects_only_strategy_and_reviewed_signal_fields(config_manager):
    engine = ContinuityEngine(config_manager)
    try:
        engine.observe_interaction_signal("user.fatigue", "likely", confidence=0.7)
        engine.set_interaction_strategy("倾听优先", "以倾听为主，减少追问。")
        view = engine.state_view_builder.build(
            "simple_response", engine.state, user_input="我今天有点累", 
            interaction_state_snapshot=engine.interaction_state_snapshot(),
        ).to_dict()
        rules = view["interaction_rules"]
        assert rules["current_interaction_strategy"]["name"] == "倾听优先"
        assert rules["interaction_state_signals"][0]["name"] == "user.fatigue"
        assert "raw_dialogue_stored" not in rules
    finally:
        if hasattr(engine, "close"):
            engine.close()


def test_adapter_extracts_only_a_bounded_current_strategy():
    context = json.dumps({"interaction_rules": {"current_interaction_strategy": {
        "name": "倾听优先", "instruction": "以倾听为主，减少追问。", "source": "engine_decision"
    }}}, ensure_ascii=False)
    assert _interaction_strategy_from_context(context) == ("倾听优先", "以倾听为主，减少追问。")
    assert _interaction_strategy_from_context('{"interaction_rules":{"current_interaction_strategy":{"name":"x"}}}') is None
