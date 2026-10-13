"""Policy-aware bridge from LIFE_LAYER observations to semantic body proposals.

This module never calls a hardware driver directly. It submits allowlisted proposals
through ContinuityEngine's existing intent policy and SafetySupervisor.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class BodyExpressionDecision:
    accepted: bool
    semantic: str
    reason_code: str
    cooldown_remaining_s: float = 0.0


class EmbodiedExpressionCoordinator:
    ALLOWED = frozenset({"startle", "alert_gaze", "slow_breath", "stretch", "idle_pose", "neutral"})

    def __init__(self, engine: Any, *, enabled: bool = False,
                 cooldown_by_event: Mapping[str, float] | None = None):
        self.engine = engine
        self.enabled = bool(enabled)
        self.cooldown_by_event = {key: float(value) for key, value in (cooldown_by_event or {
            "startle": 3.0, "alert_gaze": 5.0, "slow_breath": 30.0,
            "stretch": 180.0, "idle_pose": 30.0, "neutral": 1.0,
        }).items()}
        if set(self.cooldown_by_event) - self.ALLOWED or any(not math.isfinite(v) or v < 0 for v in self.cooldown_by_event.values()):
            raise ValueError("cooldown map has an unknown semantic or invalid duration")
        self._last_proposed: dict[str, float] = {}

    def propose(self, semantic: str, *, intensity: float = 0.5, sleeping: bool = False,
                reason_code: str) -> BodyExpressionDecision:
        if semantic not in self.ALLOWED:
            raise ValueError("semantic event is not allowlisted")
        if not reason_code or len(reason_code) > 100:
            raise ValueError("reason_code must contain 1..100 characters")
        if isinstance(intensity, bool) or not isinstance(intensity, (int, float)) or not math.isfinite(float(intensity)) or not 0 <= float(intensity) <= 1:
            raise ValueError("intensity must be finite and in [0,1]")
        now = float(self.engine.clock.monotonic())
        remaining = self.cooldown_by_event.get(semantic, 0.0) - (now - self._last_proposed.get(semantic, float("-inf")))
        if remaining > 0:
            return BodyExpressionDecision(False, semantic, "COOLDOWN", remaining)
        state = self.engine.state
        latches = self.engine.router.peek_latches()
        effective_quiet = latches.quiet_scope if latches.quiet_changed else state.quiet_scope
        effective_muted = latches.muted if latches.muted is not None else state.output_muted
        if not self.enabled or not self.engine.body_enabled or self.engine.body_controller is None:
            return BodyExpressionDecision(False, semantic, "BODY_DISABLED")
        if state.safety_stopped or latches.safety_stop or latches.user_stop:
            return BodyExpressionDecision(False, semantic, "SAFETY_OR_STOP_LATCHED")
        if effective_quiet is not None or effective_muted or latches.user_speaking:
            return BodyExpressionDecision(False, semantic, "INTERACTION_CONTROLLED")
        accepted = self.engine.propose_body_event(semantic, intensity=float(intensity), sleeping=bool(sleeping))
        if accepted:
            self._last_proposed[semantic] = now
        return BodyExpressionDecision(accepted, semantic, reason_code if accepted else "ENGINE_REJECTED")

    def propose_from_state(self, *, affect: Mapping[str, Any], sleeping: bool = False,
                           startle_burst: float = 0.0) -> BodyExpressionDecision | None:
        """Map only approved, explicit numeric state into low-risk semantic proposals.

        It deliberately does not infer user mood from silence or initiate a body action
        merely because a user has not interacted recently.
        """
        if startle_burst > 0.75:
            return self.propose("startle", intensity=min(1.0, float(startle_burst)), sleeping=sleeping,
                                reason_code="life_layer_startle_burst")
        if sleeping:
            return self.propose("slow_breath", intensity=.25, sleeping=True,
                                reason_code="life_layer_sleep_state")
        arousal = affect.get("arousal", 0.18)
        if isinstance(arousal, (int, float)) and not isinstance(arousal, bool) and arousal < .2:
            return self.propose("neutral", intensity=.1, sleeping=False, reason_code="low_arousal_neutral")
        return None
