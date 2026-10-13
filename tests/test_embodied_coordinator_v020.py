from __future__ import annotations
from types import SimpleNamespace

import pytest

from continuity_engine import EmbodiedExpressionCoordinator

class FakeEngine:
    def __init__(self):
        self.now = 10.0
        self.clock = SimpleNamespace(monotonic=lambda: self.now)
        self.state = SimpleNamespace(safety_stopped=False, output_muted=False, quiet_scope=None, user_speaking=False)
        self.latches = SimpleNamespace(safety_stop=False, user_stop=False, quiet_scope=None, quiet_changed=False,
                                       muted=None, user_speaking=False)
        self.router = SimpleNamespace(peek_latches=lambda: self.latches)
        self.body_enabled = True
        self.body_controller = object()
        self.proposals=[]
    def propose_body_event(self, event, *, intensity=0.5, sleeping=False):
        self.proposals.append((event, intensity, sleeping))
        return True


def test_body_expression_goes_through_engine_proposal_and_has_reason_code():
    engine = FakeEngine()
    coordinator = EmbodiedExpressionCoordinator(engine, enabled=True)
    result = coordinator.propose("startle", intensity=.8, reason_code="verified_sound_event")
    assert result.accepted
    assert result.reason_code == "verified_sound_event"
    assert engine.proposals == [("startle", .8, False)]


def test_body_expression_rejects_when_quiet_or_stopped():
    engine = FakeEngine()
    coordinator = EmbodiedExpressionCoordinator(engine, enabled=True)
    engine.latches.quiet_changed = True
    engine.latches.quiet_scope = "SESSION"
    result = coordinator.propose("stretch", reason_code="rest_state")
    assert not result.accepted and result.reason_code == "INTERACTION_CONTROLLED"
    engine.latches.quiet_changed = False
    engine.latches.safety_stop = True
    result = coordinator.propose("neutral", reason_code="neutral_state")
    assert not result.accepted and result.reason_code == "SAFETY_OR_STOP_LATCHED"
    assert engine.proposals == []


def test_body_expression_cooldown_and_allowlist():
    engine = FakeEngine()
    coordinator = EmbodiedExpressionCoordinator(engine, enabled=True, cooldown_by_event={"stretch": 10})
    first = coordinator.propose("stretch", reason_code="rest")
    second = coordinator.propose("stretch", reason_code="rest")
    assert first.accepted
    assert not second.accepted and second.reason_code == "COOLDOWN"
    with pytest.raises(ValueError):
        coordinator.propose("dance", reason_code="not_allowlisted")
