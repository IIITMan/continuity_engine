import pytest

from continuity_engine import (
    ClarificationLimitReached, ClarificationManager, EngineState, IntentCandidate,
    IntentManager, PolicyConfig, StateView, StateViewBuilder, StateViewError,
)


def candidate(kind, score, goal=None, **kwargs):
    return IntentCandidate(intent_type=kind, goal=goal or kind, score=score, reason="test", **kwargs)


def test_policy_initial_select_and_stable_current():
    p = IntentManager(PolicyConfig(min_dwell_time_s=0, consecutive_evaluations_to_switch=2), id_factory=iter(["i1", "i2", "i3"]).__next__)
    d1 = p.evaluate([candidate("talk", 0.8)], 0)
    assert d1.action_required and d1.intent.intent_id == "i1"
    d2 = p.evaluate([candidate("talk", 0.79)], 1)
    assert not d2.changed
    assert p.current.intent_id == "i1"


def test_policy_requires_margin_and_two_confirmations():
    p = IntentManager(PolicyConfig(min_dwell_time_s=0, switch_margin=0.15, consecutive_evaluations_to_switch=2), id_factory=iter(["i1", "i2"]).__next__)
    p.evaluate([candidate("talk", 0.7)], 0)
    d1 = p.evaluate([candidate("explore", 0.95)], 11)
    assert d1.reason == "awaiting_switch_confirmation"
    assert p.current.intent_type == "talk"
    d2 = p.evaluate([candidate("explore", 0.95)], 12)
    assert d2.changed
    assert d2.intent.intent_type == "explore"
    assert d2.previous_intent_id == "i1"


def test_high_urgency_bypasses_dwell_and_switch_confirmation():
    p = IntentManager(PolicyConfig(min_dwell_time_s=100, consecutive_evaluations_to_switch=5), id_factory=iter(["i1", "i2"]).__next__)
    p.evaluate([candidate("talk", 0.8)], 0)
    d = p.evaluate([candidate("safety_review", 0.4, urgency=0.95)], 1)
    assert d.changed
    assert d.intent.intent_type == "safety_review"


def test_clarification_is_two_rounds_and_deduplicated():
    p = IntentManager(id_factory=lambda: "i1")
    intent = p.evaluate([candidate("clarify", 0.8)], 0).intent
    cm = ClarificationManager(2)
    assert cm.ask(intent, "几点出发？")
    assert not cm.ask(intent, "几点出发")
    assert cm.ask(intent, "去哪一个地点？")
    with pytest.raises(ClarificationLimitReached):
        cm.ask(intent, "再补充一下？")


def test_state_view_is_purpose_filtered_and_immutable():
    state = EngineState()
    state.last_user_message = "hello"
    state.output_muted = True
    state.drives["secret_internal_drive"] = __import__("continuity_engine").DriveState(0.9, 0.9, 0.9, 0)
    view = StateViewBuilder().build("simple_response", state, user_input="hello")
    rendered = view.to_json()
    assert "secret_internal_drive" not in rendered
    assert '"output_muted":true' in rendered
    with pytest.raises(TypeError):
        view._fields["interaction_rules"]["output_muted"] = False


def test_state_view_rejects_unknown_purpose_and_extra_fields():
    with pytest.raises(StateViewError):
        StateViewBuilder().build("raw_snapshot", EngineState())
    with pytest.raises(StateViewError):
        StateView("simple_response", "1.0", {"purpose": "simple_response", "raw_state": {}})


def test_llm_world_view_omits_unapproved_sensitive_fact_keys():
    from continuity_engine import WorldFact, WorldStatus
    state = EngineState()
    state.world["owner_present"] = WorldFact("owner_present", True, WorldStatus.CONFIRMED, 0.99, ("camera",), 1, 99, ("e1",))
    state.world["home_address"] = WorldFact("home_address", "private address", WorldStatus.CONFIRMED, 0.99, ("user",), 1, 99, ("e2",))
    view = StateViewBuilder().build("complex_dialogue", state, user_input="hello")
    rendered = view.to_json()
    assert "owner_present" in rendered
    assert "home_address" not in rendered
    assert "private address" not in rendered


def test_simple_response_includes_only_allowlisted_fresh_world_facts():
    from continuity_engine import WorldFact, WorldStatus

    state = EngineState()
    state.world["day_period"] = WorldFact(
        "day_period", "evening", WorldStatus.CONFIRMED, 0.99, ("clock_service",), 5.0, 50.0, ("evt-time",)
    )
    state.world["weather_summary"] = WorldFact(
        "weather_summary", "light rain", WorldStatus.CONFIRMED, 0.95, ("weather_service",), 5.0, 8.0, ("evt-weather",)
    )
    state.world["owner_present"] = WorldFact(
        "owner_present", True, WorldStatus.CONFIRMED, 0.99, ("camera",), 1.0, 4.0, ("evt-stale",)
    )
    builder = StateViewBuilder()
    view = builder.build("simple_response", state, user_input="现在是什么天气？", now_mono=10.0)
    assert view.to_dict()["world_summary"] == [
        {"key": "day_period", "value": "evening", "status": "CONFIRMED", "confidence": 0.99, "updated_at_mono": 5.0}
    ]


def test_plan_view_rejects_unapproved_task_progress_fields():
    with pytest.raises(StateViewError):
        StateViewBuilder().build("multi_step_plan", EngineState(), goal="task", task_progress={"raw_engine_state": {"secret": 1}})


def test_switch_confirmation_must_be_consecutive():
    p = IntentManager(PolicyConfig(min_dwell_time_s=0, switch_margin=0.1, consecutive_evaluations_to_switch=2, ordinary_candidate_threshold=0.5),
                      id_factory=iter(["i1", "i2"]).__next__)
    p.evaluate([candidate("talk", 0.6)], 0)
    first = p.evaluate([candidate("explore", 0.9)], 11)
    assert first.reason == "awaiting_switch_confirmation"
    # A decision tick without the competing candidate breaks the confirmation streak.
    p.evaluate([], 12)
    third = p.evaluate([candidate("explore", 0.9)], 13)
    assert third.reason == "awaiting_switch_confirmation"
    assert p.current.intent_type == "talk"
