"""Rule-based autonomous intent generation from drives and world state.

This module proposes candidates only. IntentManager decides which candidate (if any)
becomes active, and SafetySupervisor remains the final gate for external effects.
No network, hardware, or LLM calls are made here.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping

from .drives import DriveSystem
from ..core.models import DriveState, Intent, IntentCandidate, IntentStatus, WorldFact, WorldStatus


@dataclass(frozen=True, slots=True)
class AutonomyConfig:
    enabled: bool = True
    rest_threshold: float = 0.78
    rest_candidate_score: float = 0.67
    exploration_threshold: float = 0.55
    exploration_cooldown_s: float = 120.0
    exploration_world_keys: tuple[str, ...] = ("sound_event_summary", "known_object_summary", "room_activity")
    social_enabled: bool = True
    social_target_base: float = 0.15
    social_target_max: float = 0.75
    social_target_ramp_s: float = 1800.0
    social_min_idle_s: float = 1800.0
    social_drive_threshold: float = 0.42
    owner_presence_confidence_min: float = 0.85
    social_cooldown_s: float = 1200.0
    social_message: str = "需要我帮忙或聊聊时，随时叫我。"
    rest_suppression_s: float = 300.0
    rest_max_duration_s: float = 300.0
    rest_exit_threshold: float = 0.45

    def __post_init__(self) -> None:
        for name in (
            "rest_threshold", "rest_candidate_score", "exploration_threshold",
            "social_target_base", "social_target_max", "social_drive_threshold",
            "owner_presence_confidence_min", "rest_exit_threshold",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{name} must be finite and in [0,1]")
        for name in ("exploration_cooldown_s", "social_target_ramp_s", "social_min_idle_s", "social_cooldown_s", "rest_suppression_s", "rest_max_duration_s"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and >= 0")
        if self.social_target_max < self.social_target_base:
            raise ValueError("social_target_max cannot be below social_target_base")
        if not self.social_message.strip():
            raise ValueError("social_message must be non-empty")


class AutonomousBehaviorGenerator:
    """Generate conservative candidates from stable state, not raw sensor frames.

    V1 strategies:
    - rest: a quiet, non-speaking waiting intent when rest drive is high;
    - exploration: request fresh observation for an allowlisted stale/conflicting fact;
    - social: low-frequency check-in only when presence is sufficiently supported.

    More open-ended behavior generation is intentionally not done by this class.
    """

    def __init__(self, config: AutonomyConfig | None = None, *, now_mono: float = 0.0):
        self.config = config or AutonomyConfig()
        if not math.isfinite(now_mono):
            raise ValueError("now_mono must be finite")
        self.last_user_interaction_mono = now_mono
        self.last_social_action_mono: float | None = None
        self._cooldown_until: dict[tuple[str, str], float] = {}
        self._social_suppressed_until = now_mono

    @classmethod
    def from_mapping(cls, cfg: Mapping[str, Any], *, now_mono: float = 0.0) -> "AutonomousBehaviorGenerator":
        section = cfg.get("autonomy", {})
        exploration = section.get("exploration", {})
        rest = section.get("rest", {})
        social = section.get("social", {})
        return cls(AutonomyConfig(
            enabled=bool(section.get("enabled", True)),
            rest_threshold=float(rest.get("threshold", 0.78)),
            rest_candidate_score=float(rest.get("candidate_score", 0.67)),
            exploration_threshold=float(exploration.get("threshold", 0.55)),
            exploration_cooldown_s=float(exploration.get("cooldown_s", 120.0)),
            exploration_world_keys=tuple(str(x) for x in exploration.get("world_keys", ("sound_event_summary", "known_object_summary", "room_activity"))),
            social_enabled=(bool(social.get("enabled", True)) and int(
                cfg.get("interaction", {}).get("ordinary_proactive_messages", {}).get("max_count", 2)
            ) > 0),
            social_target_base=float(social.get("target_base", 0.15)),
            social_target_max=float(social.get("target_max", 0.75)),
            social_target_ramp_s=float(social.get("target_ramp_s", 1800.0)),
            social_min_idle_s=float(social.get("min_idle_s", 1800.0)),
            social_drive_threshold=float(social.get("drive_threshold", 0.42)),
            owner_presence_confidence_min=float(social.get("owner_presence_confidence_min", 0.85)),
            social_cooldown_s=float(social.get("cooldown_s", 1200.0)),
            social_message=str(social.get("message", "需要我帮忙或聊聊时，随时叫我。")),
            rest_suppression_s=float(rest.get("suppression_s", 300.0)),
            rest_max_duration_s=float(rest.get("max_duration_s", 300.0)),
            rest_exit_threshold=float(rest.get("exit_threshold", 0.45)),
        ), now_mono=now_mono)

    def note_user_interaction(self, now_mono: float, drives: DriveSystem | None = None) -> None:
        if not math.isfinite(now_mono):
            raise ValueError("now_mono must be finite")
        self.last_user_interaction_mono = now_mono
        self._social_suppressed_until = now_mono + self.config.social_cooldown_s
        if drives is not None and "social" in drives.states:
            state = drives.states["social"]
            # A real interaction satisfies some social demand, without pretending it is fully met.
            drives.impulse("social", -min(0.25, state.value * 0.35), -min(0.2, state.urgency))
            drives.set_target("social", self.config.social_target_base, urgency=0.0)

    def defer_social_until(self, allowed_at_mono: float) -> None:
        if not math.isfinite(allowed_at_mono):
            raise ValueError("allowed_at_mono must be finite")
        self._social_suppressed_until = max(self._social_suppressed_until, allowed_at_mono)

    def update_targets(self, drives: DriveSystem, now_mono: float) -> None:
        if not self.config.enabled or "social" not in drives.states:
            return
        idle = max(0.0, now_mono - self.last_user_interaction_mono)
        ramp = self.config.social_target_ramp_s
        fraction = 1.0 if ramp == 0 else min(1.0, idle / ramp)
        target = self.config.social_target_base + (self.config.social_target_max - self.config.social_target_base) * fraction
        # During the cooldown following a real user interaction, do not immediately rebuild demand.
        if now_mono < self._social_suppressed_until:
            target = self.config.social_target_base
        urgency = 0.0 if idle < self.config.social_min_idle_s else min(1.0, (idle - self.config.social_min_idle_s) / max(1.0, self.config.social_target_ramp_s))
        drives.set_target("social", target, urgency=urgency)

    def generate(
        self,
        *,
        drives: DriveSystem,
        world: Mapping[str, WorldFact],
        now_mono: float,
        quiet_scope: str | None,
        user_speaking: bool,
        safety_stopped: bool,
        current_intent: Intent | None,
    ) -> list[IntentCandidate]:
        if not self.config.enabled or not math.isfinite(now_mono):
            return []
        result: list[IntentCandidate] = []
        # While a real action is running, keep the core stable. User/control events can
        # still force re-planning through the normal event path.
        if current_intent is not None and current_intent.status == IntentStatus.RUNNING:
            return result

        rest = drives.states.get("rest")
        rest_cooldown = self._cooldown_until.get(("quiet_rest", "enter_quiet_rest"), 0.0)
        if rest is not None and rest.value >= self.config.rest_threshold and now_mono >= rest_cooldown:
            result.append(IntentCandidate(
                intent_type="quiet_rest", goal="enter_quiet_rest", score=self.config.rest_candidate_score,
                urgency=min(0.5, rest.urgency), reason="autonomy_rest_drive_high", action_type=None,
                action_args={}, interruption_policy="RESUME_IF_VALID",
            ))

        exploration = drives.states.get("exploration")
        if exploration is not None and exploration.value >= self.config.exploration_threshold:
            for key in self.config.exploration_world_keys:
                fact = world.get(key)
                if fact is None or fact.status not in {WorldStatus.STALE, WorldStatus.CONFLICT, WorldStatus.PROBABLE}:
                    continue
                goal = f"refresh_world_fact:{key}"
                cooldown_until = self._cooldown_until.get(("observe_fact", goal), 0.0)
                if now_mono < cooldown_until:
                    continue
                score = min(0.92, 0.65 + 0.25 * exploration.value)
                result.append(IntentCandidate(
                    intent_type="observe_fact", goal=goal, score=score,
                    urgency=max(exploration.urgency, 0.1), reason="autonomy_uncertain_world_fact",
                    action_type="OBSERVE", action_args={"key": key, "reason": fact.status.value.lower()},
                    interruption_policy="REPLAN",
                ))
                break  # one autonomous observation request at a time

        if self._should_check_in(drives, world, now_mono, quiet_scope, user_speaking, safety_stopped, current_intent):
            social = drives.states.get("social")
            value = social.value if social else 0.0
            result.append(IntentCandidate(
                intent_type="proactive_checkin", goal="low_frequency_social_checkin",
                score=min(0.85, 0.66 + 0.20 * value), urgency=social.urgency if social else 0.0,
                reason="autonomy_social_checkin_after_idle_period", action_type="SPEAK",
                action_args={"text": self.config.social_message, "mode": "autonomous_checkin"},
                interruption_policy="DROP_IF_STALE", ordinary_proactive=True,
            ))
        return result

    def _should_check_in(self, drives: DriveSystem, world: Mapping[str, WorldFact], now_mono: float,
                         quiet_scope: str | None, user_speaking: bool, safety_stopped: bool,
                         current_intent: Intent | None) -> bool:
        if not self.config.social_enabled or quiet_scope is not None or user_speaking or safety_stopped:
            return False
        if current_intent is not None:
            # Never turn an autonomous check-in into a repeated action proposal.
            if current_intent.intent_type == "proactive_checkin":
                return False
            if current_intent.status in {IntentStatus.RUNNING, IntentStatus.WAITING}:
                return False
        idle = now_mono - self.last_user_interaction_mono
        if idle < self.config.social_min_idle_s or now_mono < self._social_suppressed_until:
            return False
        if self.last_social_action_mono is not None and now_mono - self.last_social_action_mono < self.config.social_cooldown_s:
            return False
        social = drives.states.get("social")
        if social is None or social.value < self.config.social_drive_threshold:
            return False
        presence = world.get("owner_present")
        if presence is None or presence.status not in {WorldStatus.CONFIRMED, WorldStatus.PROBABLE}:
            return False
        if presence.value is not True or presence.confidence < self.config.owner_presence_confidence_min:
            return False
        return True

    def rest_should_end(self, intent: Intent, drives: DriveSystem, now_mono: float) -> bool:
        if intent.intent_type != "quiet_rest":
            return False
        rest = drives.states.get("rest")
        duration = max(0.0, now_mono - intent.started_at_mono)
        return duration >= self.config.rest_max_duration_s or (rest is not None and rest.value <= self.config.rest_exit_threshold)

    def end_rest(self, now_mono: float, drives: DriveSystem) -> None:
        self._cooldown_until[("quiet_rest", "enter_quiet_rest")] = now_mono + self.config.rest_suppression_s
        if "rest" in drives.states:
            rest = drives.states["rest"]
            drives.impulse("rest", -min(0.4, rest.value * 0.6), -min(0.2, rest.urgency))

    def on_action_terminal(self, intent: Intent | None, status: str, now_mono: float, drives: DriveSystem) -> None:
        if intent is None:
            return
        if intent.intent_type == "proactive_checkin":
            self.last_social_action_mono = now_mono
            self._social_suppressed_until = now_mono + self.config.social_cooldown_s
            if status == "SUCCEEDED" and "social" in drives.states:
                social = drives.states["social"]
                drives.impulse("social", -min(0.30, social.value * 0.5), -min(0.25, social.urgency))
        elif intent.intent_type == "observe_fact":
            goal = intent.goal
            self._cooldown_until[("observe_fact", goal)] = now_mono + self.config.exploration_cooldown_s
            if status == "SUCCEEDED" and "exploration" in drives.states:
                exploration = drives.states["exploration"]
                drives.impulse("exploration", -min(0.25, exploration.value * 0.35), -min(0.15, exploration.urgency))
        elif intent.intent_type == "quiet_rest":
            self._cooldown_until[("quiet_rest", intent.goal)] = now_mono + self.config.rest_suppression_s
            if status == "SUCCEEDED" and "rest" in drives.states:
                drives.impulse("rest", -min(0.4, drives.states["rest"].value * 0.6), -min(0.2, drives.states["rest"].urgency))
