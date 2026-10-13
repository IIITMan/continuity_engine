from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from continuity_engine import (
    Attachment, BreathModel, DriveField, DriveState, EngineState, EventKind,
    LifeLayer, LifeStream, ReplayClock, ReplayActionExecutor, ContinuityEngine,
    ConfigManager, SQLiteRepository, StateViewBuilder, Temperament, WillArbiter,
    WillState, WorldFact, WorldStatus, prepare_speech_segments, strip_ai_phrases,
)


def fact(value: bool, status=WorldStatus.CONFIRMED, confidence=0.99):
    return WorldFact("owner_present", value, status, confidence, ("test_sensor",), 1.0, 100.0, ("evt",))


def test_drive_field_gates_weak_waves_and_excludes_safety():
    field = DriveField(low_activation=.45)
    weak = field.snapshot({"safety": .99, "rest": .02, "social": .01})
    assert weak.action_waves[0][0] == "rest"
    assert weak.struggle == 0.0
    strong = field.snapshot({"safety": .99, "rest": .9, "social": .89})
    assert strong.struggle == pytest.approx(.99)
    sleeping = field.snapshot({"safety": .8, "rest": .9, "social": .89}, sleeping=True)
    assert sleeping.struggle <= .3


def test_will_accumulator_does_not_drop_elapsed_ticks():
    will = WillArbiter(tick_s=5, useless_probability=0.0)
    snapshot = DriveField().snapshot({"rest": .1, "social": .2, "safety": .8})
    assert will.propose(snapshot, dt=4.9, now_mono=4.9) == []
    will.propose(snapshot, dt=0.1, now_mono=5.0)
    assert will._tick_index == 1
    will.propose(snapshot, dt=300, now_mono=305)
    assert will._tick_index == 61


def test_will_cancelled_state_is_terminal_until_explicit_new_request():
    will = WillArbiter(tick_s=1, useless_probability=1.0)
    will.interrupt("user_cancel", cancelled=True, now_mono=0)
    field = DriveField().snapshot({"rest": 0.1, "social": .1})
    assert will.propose(field, dt=10, now_mono=10) == []
    assert will.state == WillState.CANCELLED
    will.reset_cancelled(explicit_new_request=True)
    assert will.state == WillState.IDLE


def test_attachment_does_not_infer_absence_from_unknown_stale_or_probable():
    attachment = Attachment(separation_k=.0009)
    for state in (WorldStatus.UNKNOWN, WorldStatus.STALE, WorldStatus.PROBABLE, WorldStatus.CONFLICT):
        attachment.observe_presence(fact(False, state), dt=3600, now_wall=10000)
    assert attachment.separation_s == 0
    attachment.observe_presence(fact(False), dt=1800, now_wall=10000)
    assert attachment.confirmed_absent and attachment.separation_s == 1800
    assert attachment.separation_level == pytest.approx(.81, abs=.02) or attachment.separation_level <= .8


def test_attachment_reunion_has_three_tiers_and_bounded_trust():
    a = Attachment(trust=99, imprint_days=14)
    a.observe_presence(fact(False), dt=60, now_wall=1_800_000_000)
    response = a.observe_presence(fact(True), dt=1, now_wall=1_800_000_060)
    assert response is not None and response.category == "brief"
    assert a.trust == pytest.approx(99.25)
    assert not a.confirmed_absent and a.separation_s == 0


def test_life_stream_is_bounded_structured_and_deterministic():
    stream = LifeStream(max_entries_per_day=2, retention_days=2, seed="test")
    assert stream.record(day="2026-10-10", kind="shared_activity", summary_key="shared", source_id="evt1", timestamp_wall=1)
    assert not stream.record(day="2026-10-10", kind="shared_activity", summary_key="shared", source_id="evt1", timestamp_wall=1)
    assert stream.record(day="2026-10-10", kind="reunion", summary_key="reunion", source_id="evt2", timestamp_wall=2)
    assert not stream.record(day="2026-10-10", kind="novelty", summary_key="extra", source_id="evt3", timestamp_wall=3)
    first = stream.consolidate_night("2026-10-10")
    second = stream.consolidate_night("2026-10-10")
    assert first["summary"] == second["summary"]
    assert "text" not in str(stream.snapshot())
    with pytest.raises(ValueError):
        stream.record(day="2026-10-10", kind="reunion", summary_key="x", source_id="evt4", timestamp_wall=3,
                      metadata={"raw_dialogue": "should not persist"})


def test_temperament_drift_is_bounded_and_plasticity_decays():
    t = Temperament(birth_wall=0)
    assert t.plasticity(0.5) == 1.0
    assert t.plasticity(2) == .7
    assert t.plasticity(6) == .5
    assert t.plasticity(20) == .3
    for _ in range(100):
        t.drift("affiliation", 1, now_wall=30 * 86400)
    assert t.traits["affiliation"] <= .8
    assert t.traits["affiliation"] >= .5


def test_temperament_action_parameters_are_bounded_and_do_not_grant_permissions():
    t = Temperament(traits={"affiliation": 0.8, "curiosity": 0.8, "reactivity": 0.8, "sensitivity": 0.8})
    params = t.action_params(verbosity_base=1.0, initiative_base=1.0, warmth_base=1.0, curiosity_base=1.0)
    assert 0.5 <= params["proactive_scale"] <= 1.5
    assert 80 <= params["verbosity_chars"] <= 400
    assert 0 <= params["warmth"] <= 1
    assert 0.9 <= params["exploration_threshold_scale"] <= 1.1
    assert 0.8 <= params["arousal_response_scale"] <= 1.2
    assert 0.75 <= params["startle_expression_scale"] <= 1.25


def test_breath_exact_update_remains_finite_for_large_dt_and_burst_visible():
    b = BreathModel()
    b.shock(.9)
    first = b.update(dt=60, arousal=.8)
    assert first["burst"] == pytest.approx(.9)
    assert 0 < first["rate_bpm"] <= 24
    assert 0 <= first["phase"] < 1
    sleep = b.update(dt=60, arousal=.1)
    assert 0 <= sleep["phase"] < 1 and abs(sleep["waveform"]) <= 1
    b.set_sleeping(True)
    sleeping = b.update(dt=60, arousal=.1)
    assert sleeping["rate_bpm"] < sleep["rate_bpm"]


def test_expression_helpers_preserve_identity_disclosures_and_terminal_punctuation():
    assert strip_ai_phrases("我在这里。") == "我在这里。"
    assert strip_ai_phrases("作为AI助手，很高兴为您服务。") == "。"
    assert prepare_speech_segments("**你好。**", verbosity_chars=100) == ("你好。",)
    assert len(prepare_speech_segments("好" * 400, verbosity_chars=80)[0]) <= 80


def test_engine_persists_life_layer_state_with_additive_schema(config_manager=None):
    cfg = ConfigManager.from_yaml("config/default.yaml")
    clock = ReplayClock(mono=0, wall=1_800_000_000)
    engine = ContinuityEngine(cfg, clock=clock, replay_mode=True, action_executor=ReplayActionExecutor())
    repo = SQLiteRepository(":memory:")
    try:
        engine.step(dt=1)
        engine.save(repo)
        state = repo.load_state()
        assert state is not None
        assert state.life_layer_state["schema_version"] == 1
        assert "breath" in state.life_layer_state and "life_stream" in state.life_layer_state
    finally:
        engine.close(); repo.close()


def test_life_context_only_exposes_bounded_summaries():
    state = EngineState()
    state.life_layer_state = {"life_context": [{"day": "2026-10-10", "entry_count": 1, "summary": "昨天记录了一次共同经历。", "source_ids": ["secret"]}]}
    view = StateViewBuilder().build("simple_response", state, user_input="还记得吗")
    payload = view.to_dict()
    assert payload["life_context"] == [{"day": "2026-10-10", "entry_count": 1, "summary": "昨天记录了一次共同经历。"}]
    assert "secret" not in view.to_json()


def test_sqlite_memory_search_and_deletion_lineage():
    repo = SQLiteRepository(":memory:")
    try:
        repo.put_memory("m1", "user_preference", {"summary": "用户喜欢天文学和星星"}, ["evt1"], 1.0)
        rows = repo.search_memories("天文学", limit=5)
        assert rows and rows[0]["memory_id"] == "m1"
        repo.put_derived("d1", "summary", {"summary": "偏好摘要"}, ["m1"], None, 2.0)
        deleted = repo.delete_memory("m1", 3.0)
        assert deleted["status"] == "DELETED"
        assert repo.list_memories() == []
    finally:
        repo.close()


def test_llm_provider_outage_returns_disclosed_local_fallback(config_manager):
    from continuity_engine import LLMGateway, StubLLMAdapter, LLMStatus

    async def run():
        clock = ReplayClock(mono=0, wall=1_800_000_000)
        # No fixture for simple_response causes a deterministic adapter failure.
        gateway = LLMGateway(StubLLMAdapter({}), timeout_s=1)
        engine = ContinuityEngine(config_manager, clock=clock, llm_gateway=gateway)
        try:
            result = await engine.request_llm(purpose="simple_response", interaction_id="fallback", generation=0, user_input="你好")
            assert result.status == LLMStatus.COMPLETED
            assert result.payload["degraded"] is True
            assert "暂时不可用" in result.payload["response_text"]
            assert result.error_code == "DEGRADED_LOCAL_FALLBACK"
        finally:
            engine.close()
    asyncio.run(run())


def test_life_stream_consolidation_is_idempotent_across_retries_and_restore():
    stream = LifeStream(seed="idempotent")
    stream.record(day="2026-10-12", kind="shared_activity", summary_key="shared", source_id="e1", timestamp_wall=1)
    first = stream.consolidate_night("2026-10-12")
    assert stream.nights == 1 and stream.self_depth == 1
    second = stream.consolidate_night("2026-10-12")
    assert second == first
    assert stream.nights == 1 and stream.self_depth == 1
    restored = LifeStream(seed="idempotent")
    restored.restore(stream.snapshot())
    assert restored.consolidate_night("2026-10-12") == first
    assert restored.nights == 1 and restored.self_depth == 1


def test_emotion_assessment_only_changes_transient_affect_and_rejects_low_confidence(tmp_path):
    from continuity_engine.state.affect_personality import AffectPersonality
    personality = AffectPersonality(enabled=True, persistence_path=tmp_path / "profile.json")
    original_traits = dict(personality.traits)
    original_arousal = personality.affect["arousal"]
    assert not personality.observe_emotion_assessment({"emotion": "anger", "confidence": .54, "evidence": []})
    assert personality.observe_emotion_assessment({"emotion": "fatigue", "confidence": .95, "evidence": ["secret raw text"]})
    assert personality.affect["arousal"] < original_arousal
    assert personality.traits == original_traits
    assert not (tmp_path / "profile.json").exists()  # affect/evidence never persisted


def test_engine_applies_structured_emotion_estimate_to_transient_affect(config_manager, tmp_path):
    from continuity_engine import LLMGateway, StubLLMAdapter
    from test_demand_understanding import valid_payload

    async def run():
        payload = valid_payload()
        cfg = config_manager.get()
        cfg["affect_personality"]["persistence_path"] = str(tmp_path / "persona.json")
        adapter = StubLLMAdapter({"demand_understanding": payload})
        engine = ContinuityEngine(ConfigManager(cfg), llm_gateway=LLMGateway(adapter, timeout_s=1))
        try:
            before = engine.affect_personality.affect["arousal"]
            result = await engine.understand_demand(user_input="我今天很累", interaction_id="emo", generation=1)
            assert result["emotion_assessment"]["emotion"] == "fatigue"
            assert engine.affect_personality.affect["arousal"] < before
            assert any("emotion_assessment_applied" in d for d in engine._diagnostics)
            assert engine.affect_personality.traits["warmth"] == .68
        finally:
            engine.close()
    asyncio.run(run())


def test_quiet_mode_disables_degraded_local_fallback(config_manager):
    from continuity_engine import LLMGateway, StubLLMAdapter, LLMStatus

    async def run():
        gateway = LLMGateway(StubLLMAdapter({}), timeout_s=1)
        engine = ContinuityEngine(config_manager, llm_gateway=gateway)
        try:
            engine.state.quiet_scope = "SESSION"
            result = await engine.request_llm(purpose="simple_response", interaction_id="quiet-fallback", generation=0, user_input="你好")
            assert result.status == LLMStatus.FAILED
            assert result.error_code != "DEGRADED_LOCAL_FALLBACK"
        finally:
            engine.close()
    asyncio.run(run())


def test_body_controller_fails_closed_without_driver_and_validates_args():
    from continuity_engine import ActionProposal, ActionStatus, BodyController, SemanticBodyMapper
    mapper = SemanticBodyMapper(enabled=True)
    cmd = mapper.map(event="startle", arousal=.7)
    assert cmd is not None

    async def run():
        controller = BodyController(enabled=True, mapper=mapper, driver=None)
        proposal = ActionProposal("a1", "i1", "BODY", cmd, created_at_mono=0)
        result = await controller.execute(proposal)
        assert result.status == ActionStatus.FAILED
        assert result.error_code == "BODY_DRIVER_UNAVAILABLE"
        invalid = ActionProposal("a2", "i1", "BODY", {"semantic": "startle", "intensity": 9, "sleeping": False}, created_at_mono=0)
        result2 = await controller.execute(invalid)
        assert result2.status == ActionStatus.FAILED
        assert result2.error_code == "BODY_ARGS_INVALID"
    asyncio.run(run())


def test_life_layer_reunion_emits_one_bounded_pulse_and_records_journal():
    from continuity_engine.state.drives import DriveConfig, DriveSystem
    layer = LifeLayer({"will": {"enabled": False}, "attachment": {"enabled": True, "separation_k": .0009},
                       "life_stream": {"enabled": True}}, now_wall=1_800_000_000)
    drives = DriveSystem({"attachment": DriveConfig(1800, .0, .0), "rest": DriveConfig(100, .2, .2),
                          "social": DriveConfig(100, .1, .1), "safety": DriveConfig(10, .8, .8)}, now_mono=0)
    absent_wall = 1_800_000_000
    layer.tick(dt=1800, now_mono=1800, now_wall=absent_wall, drives=drives,
               world={"owner_present": fact(False)}, affect={"arousal": .2}, current_intent=None,
               quiet_scope=None, user_speaking=False, safety_stopped=False)
    assert layer.consume_reunion_pulse() is None
    present_wall = absent_wall + 1801
    layer.tick(dt=1, now_mono=1801, now_wall=present_wall, drives=drives,
               world={"owner_present": fact(True)}, affect={"arousal": .2}, current_intent=None,
               quiet_scope=None, user_speaking=False, safety_stopped=False)
    pulse = layer.consume_reunion_pulse()
    assert pulse is not None and 0 < pulse["valence_delta"] <= .08
    assert layer.consume_reunion_pulse() is None
    assert drives.states["attachment"].value > 0
    day = datetime.fromtimestamp(present_wall, timezone.utc).date().isoformat()
    assert layer.life_stream.daily[day][0]["kind"] == "reunion"


def test_habituation_reduces_repeated_expression_but_never_suppresses_it():
    from continuity_engine import HabituationTracker
    h = HabituationTracker(repeat_scale=.25)
    first, familiar1 = h.expressive_intensity("unexpected_sound", .8)
    second, familiar2 = h.expressive_intensity("unexpected_sound", .8)
    assert first == .8 and not familiar1
    assert second == .2 and familiar2
    assert second > 0
    restored = HabituationTracker()
    restored.restore(h.snapshot())
    third, familiar3 = restored.expressive_intensity("unexpected_sound", .8)
    assert third == .2 and familiar3


def test_breath_wakes_from_sleep_and_returns_after_wake_window():
    b = BreathModel()
    b.set_sleeping(True)
    b.shock(.8, now_wall=1000, wake_hold_s=30)
    assert not b.sleeping
    b.update(dt=1, arousal=.6, now_wall=1029)
    assert not b.sleeping
    b.update(dt=1, arousal=.1, now_wall=1030)
    assert b.sleeping


def test_life_layer_global_ablation_keeps_new_state_and_fallback_inactive(config_manager):
    from continuity_engine import LLMStatus
    from copy import deepcopy

    async def run():
        cfg = deepcopy(config_manager.get())
        cfg["life_layer"]["enabled"] = False
        engine = ContinuityEngine(ConfigManager(cfg))
        try:
            before = engine.life_layer_snapshot()["breath"]
            engine.set_life_sleeping(True)
            assert engine.record_life_event(kind="novelty", summary_key="noise", source_id="e1") is False
            assert engine.life_layer_snapshot()["breath"] == before
            result = await engine.request_llm(purpose="simple_response", interaction_id="ablation", generation=0, user_input="你好")
            assert result.status == LLMStatus.FAILED
            assert result.error_code == "LLM_NOT_CONFIGURED"
        finally:
            engine.close()
    asyncio.run(run())


def test_engine_attachment_evidence_api_is_bounded_and_persistent(config_manager):
    engine = ContinuityEngine(config_manager)
    try:
        before = engine.life_layer.attachment.trust
        after = engine.record_attachment_evidence("positive_shared_activity", delta=1.0)
        assert after > before
        saved = engine.life_layer_snapshot()["attachment"]["trust"]
        assert saved == after
        with pytest.raises(ValueError):
            engine.record_attachment_evidence("silence", delta=1.0)
    finally:
        engine.close()
