from __future__ import annotations

import asyncio
import pytest

from continuity_engine import (
    ActionStatus, ContinuityEngine, DriveConfig, DriveSystem, EventKind,
    ReplayActionExecutor, ReplayClock, WorldFact, WorldStatus,
)
from continuity_engine.state.autonomy import AutonomyConfig, AutonomousBehaviorGenerator


def test_social_target_rises_with_idle_time_and_is_satisfied_by_user_interaction():
    drives = DriveSystem({"social": DriveConfig(1200, initial_value=0.15, initial_target=0.15)})
    gen = AutonomousBehaviorGenerator(AutonomyConfig(social_target_ramp_s=1800, social_cooldown_s=300), now_mono=0)
    gen.update_targets(drives, 900)
    assert drives.states["social"].target == pytest.approx(0.45)
    gen.note_user_interaction(900, drives)
    assert drives.states["social"].target == pytest.approx(0.15)
    assert gen.last_user_interaction_mono == 900


def test_social_checkin_requires_recent_verified_presence_and_idle_period():
    drives = DriveSystem({"social": DriveConfig(1200, initial_value=0.7, initial_target=0.7)})
    gen = AutonomousBehaviorGenerator(AutonomyConfig(social_min_idle_s=100, social_target_ramp_s=100, social_drive_threshold=0.4), now_mono=0)
    gen.update_targets(drives, 200)
    no_presence = gen.generate(drives=drives, world={}, now_mono=200, quiet_scope=None,
                               user_speaking=False, safety_stopped=False, current_intent=None)
    assert not any(c.intent_type == "proactive_checkin" for c in no_presence)
    world = {"owner_present": WorldFact("owner_present", True, WorldStatus.CONFIRMED, 0.95,
                                          ("camera",), 199, 204, ("e1",))}
    candidates = gen.generate(drives=drives, world=world, now_mono=200, quiet_scope=None,
                              user_speaking=False, safety_stopped=False, current_intent=None)
    proactive = [c for c in candidates if c.intent_type == "proactive_checkin"]
    assert len(proactive) == 1
    assert proactive[0].ordinary_proactive is True
    assert proactive[0].action_type == "SPEAK"


def test_social_checkin_suppressed_by_quiet_speaking_or_safety():
    drives = DriveSystem({"social": DriveConfig(1200, initial_value=0.8, initial_target=0.8)})
    gen = AutonomousBehaviorGenerator(AutonomyConfig(social_min_idle_s=1, social_target_ramp_s=1), now_mono=0)
    gen.update_targets(drives, 10)
    world = {"owner_present": WorldFact("owner_present", True, WorldStatus.CONFIRMED, 0.99,
                                          ("camera",), 9, 20, ("e1",))}
    for kwargs in (
        {"quiet_scope": "SESSION", "user_speaking": False, "safety_stopped": False},
        {"quiet_scope": None, "user_speaking": True, "safety_stopped": False},
        {"quiet_scope": None, "user_speaking": False, "safety_stopped": True},
    ):
        candidates = gen.generate(drives=drives, world=world, now_mono=10, current_intent=None, **kwargs)
        assert not any(c.intent_type == "proactive_checkin" for c in candidates)


def test_exploration_generates_observation_for_allowlisted_uncertain_fact_only():
    drives = DriveSystem({"exploration": DriveConfig(60, initial_value=0.8, initial_target=0.8)})
    gen = AutonomousBehaviorGenerator(AutonomyConfig(exploration_threshold=0.55), now_mono=0)
    world = {
        "sound_event_summary": WorldFact("sound_event_summary", None, WorldStatus.CONFLICT, 0.8, ("mic", "model"), 1, 30, ("e1", "e2")),
        "private_note": WorldFact("private_note", "x", WorldStatus.CONFLICT, 0.9, ("user",), 1, 30, ("e3",)),
    }
    candidates = gen.generate(drives=drives, world=world, now_mono=2, quiet_scope=None,
                              user_speaking=False, safety_stopped=False, current_intent=None)
    observations = [c for c in candidates if c.intent_type == "observe_fact"]
    assert len(observations) == 1
    assert observations[0].action_args["key"] == "sound_event_summary"


def test_engine_can_generate_autonomous_checkin_after_idle_when_owner_present(config_manager):
    clock = ReplayClock(mono=0, wall=1_800_000_000)
    executor = ReplayActionExecutor()
    engine = ContinuityEngine(config_manager, clock=clock, action_executor=executor, replay_mode=True)
    try:
        # Simulate a long idle interval and a fresh owner-presence observation.
        clock.advance(1800)
        engine.inject(engine.new_event(EventKind.WORLD_OBSERVATION.value, {
            "key": "owner_present", "value": True, "confidence": 0.99,
            "observation_type": "presence", "ttl_s": 5,
        }, source="camera", confidence=0.99))
        step = engine.step(dt=1800)
        assert step.state.current_intent is not None
        assert step.state.current_intent.intent_type == "proactive_checkin"
        assert len(step.proposals) == 1
        proposal = step.proposals[0]
        decision, result = asyncio.run(engine.dispatch_action(proposal))
        assert decision.outcome == "ALLOW"
        assert result is not None and result.status == ActionStatus.SUCCEEDED
        # Apply the terminal receipt and verify a repeated check-in is suppressed by cooldown.
        clock.advance(1)
        engine.step(dt=1)
        next_step = engine.step(dt=1)
        assert not any(p.ordinary_proactive for p in next_step.proposals)
    finally:
        engine.close()


def test_engine_does_not_autonomously_speak_without_presence(config_manager):
    clock = ReplayClock(mono=0, wall=1_800_000_000)
    engine = ContinuityEngine(config_manager, clock=clock, replay_mode=True, action_executor=ReplayActionExecutor())
    try:
        clock.advance(3600)
        result = engine.step(dt=3600)
        assert not any(p.ordinary_proactive for p in result.proposals)
    finally:
        engine.close()


def test_quiet_rest_is_bounded_and_does_not_reenter_immediately(config_manager):
    from copy import deepcopy
    from continuity_engine import ConfigManager
    cfg = config_manager.get()
    cfg["autonomy"]["rest"]["max_duration_s"] = 10.0
    cfg["autonomy"]["rest"]["suppression_s"] = 300.0
    config = ConfigManager(cfg)
    clock = ReplayClock(mono=0, wall=1_800_000_000)
    engine = ContinuityEngine(config, clock=clock, replay_mode=True, action_executor=ReplayActionExecutor())
    try:
        engine.inject(engine.new_event(EventKind.DRIVE_IMPULSE.value,
                                      {"drive": "rest", "delta": 0.8, "urgency_delta": 0.1}, source="test"))
        started = engine.step(dt=0)
        assert started.state.current_intent is not None
        assert started.state.current_intent.intent_type == "quiet_rest"
        assert not started.proposals
        clock.advance(11)
        finished = engine.step(dt=11)
        assert finished.state.current_intent is None
        assert "autonomy_rest_completed" in finished.diagnostics
        clock.advance(5)
        after = engine.step(dt=5)
        assert after.state.current_intent is None
    finally:
        engine.close()


def test_user_message_interrupts_quiet_rest(config_manager):
    clock = ReplayClock(mono=0, wall=1_800_000_000)
    engine = ContinuityEngine(config_manager, clock=clock, replay_mode=True, action_executor=ReplayActionExecutor())
    try:
        engine.inject(engine.new_event(EventKind.DRIVE_IMPULSE.value,
                                      {"drive": "rest", "delta": 0.8}, source="test"))
        engine.step(dt=0)
        engine.inject(engine.new_event(EventKind.USER_MESSAGE.value, {"text": "你好"}, source="mic"))
        result = engine.step(dt=0)
        assert result.state.current_intent is not None
        assert result.state.current_intent.intent_type == "respond_to_user"
        assert result.proposals and result.proposals[0].action_type == "SPEAK"
    finally:
        engine.close()


def test_proactive_quota_denial_defers_candidate_instead_of_retrying_each_tick(config_manager):
    from continuity_engine import ActionProposal, ConfigManager
    cfg = config_manager.get()
    cfg["autonomy"]["social"]["min_idle_s"] = 1.0
    cfg["autonomy"]["social"]["target_ramp_s"] = 1.0
    cfg["autonomy"]["social"]["drive_threshold"] = 0.2
    cfg["interaction"]["ordinary_proactive_messages"]["max_count"] = 1
    cfg["interaction"]["ordinary_proactive_messages"]["rolling_window_s"] = 100.0
    cfg["interaction"]["ordinary_proactive_messages"]["cooldown_s"] = 1.0
    config = ConfigManager(cfg)
    clock = ReplayClock(mono=0, wall=1_800_000_000)
    engine = ContinuityEngine(config, clock=clock, replay_mode=True, action_executor=ReplayActionExecutor())
    try:
        engine.drives.impulse("social", 0.7, 0.2)
        engine.safety.record_started(ActionProposal(
            action_id="already-sent", intent_id="old-intent", action_type="SPEAK",
            args={"text": "previous"}, created_at_mono=0, ordinary_proactive=True,
            reason_code="prior_checkin",
        ), 0)
        clock.advance(5)
        engine.inject(engine.new_event(EventKind.WORLD_OBSERVATION.value, {
            "key": "owner_present", "value": True, "confidence": 0.99,
            "observation_type": "presence", "ttl_s": 5,
        }, source="camera", confidence=0.99))
        denied = engine.step(dt=5)
        assert not denied.proposals
        assert denied.state.current_intent is None
        assert any("autonomy_proactive_deferred" in d for d in denied.diagnostics)
        clock.advance(1)
        again = engine.step(dt=1)
        assert not again.proposals
        assert again.state.current_intent is None
    finally:
        engine.close()
