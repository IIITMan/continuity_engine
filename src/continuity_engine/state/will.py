"""Bounded deterministic will arbiter. Produces candidates; never activates or executes actions."""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
import hashlib
import math
from typing import Any, Mapping
from ..core.models import Intent, IntentCandidate, IntentStatus
from .drive_field import FieldSnapshot

class WillState(str, Enum):
    IDLE = "IDLE"
    ATTEMPTING = "ATTEMPTING"
    INTERRUPTED = "INTERRUPTED"
    RECONSIDERING = "RECONSIDERING"
    ABANDONED = "ABANDONED"
    CANCELLED = "CANCELLED"

@dataclass(frozen=True, slots=True)
class WillTransition:
    state: WillState
    intent_key: str | None
    reason: str
    at_mono: float

class WillArbiter:
    def __init__(self, *, tick_s: float = 5.0, low_tension_threshold: float = 0.45,
                 useless_probability: float = 0.08, attempt_max_retry: int = 3,
                 action_waves_excluded: tuple[str, ...] = ("safety",), enabled: bool = True):
        if not math.isfinite(tick_s) or tick_s <= 0: raise ValueError("tick_s must be > 0")
        if not math.isfinite(low_tension_threshold) or not 0 <= low_tension_threshold <= 1: raise ValueError("low_tension_threshold must be in [0,1]")
        if not math.isfinite(useless_probability) or not 0 <= useless_probability <= 1: raise ValueError("useless_probability must be in [0,1]")
        if isinstance(attempt_max_retry, bool) or not isinstance(attempt_max_retry, int) or not 0 <= attempt_max_retry <= 10: raise ValueError("attempt_max_retry must be in [0,10]")
        self.tick_s, self.low_tension_threshold = float(tick_s), float(low_tension_threshold)
        self.useless_probability, self.attempt_max_retry = float(useless_probability), attempt_max_retry
        self.action_waves_excluded, self.enabled = tuple(action_waves_excluded), bool(enabled)
        self.state = WillState.IDLE
        self.intent_key: str | None = None
        self.retry_count = 0
        self.accumulator_s = 0.0
        self._tick_index = 0
        self._interruption_cancelled = False
        self.history: list[WillTransition] = []

    def interrupt(self, reason: str = "interrupted", *, cancelled: bool = False, now_mono: float = 0.0) -> None:
        if cancelled:
            self.state = WillState.CANCELLED
            self._interruption_cancelled = True
        elif self.state not in {WillState.IDLE, WillState.ABANDONED, WillState.CANCELLED}:
            self.state = WillState.INTERRUPTED
            self._interruption_cancelled = False
        else:
            return
        self.history.append(WillTransition(self.state, self.intent_key, str(reason)[:128], float(now_mono)))

    def reset_cancelled(self, *, explicit_new_request: bool = False) -> None:
        if explicit_new_request and self.state == WillState.CANCELLED:
            self.state = WillState.IDLE; self.intent_key = None; self.retry_count = 0; self._interruption_cancelled = False

    def propose(self, field: FieldSnapshot, *, dt: float, now_mono: float, current_intent: Intent | None = None,
                quiet_scope: str | None = None, user_speaking: bool = False, safety_stopped: bool = False) -> list[IntentCandidate]:
        if not self.enabled or quiet_scope is not None or user_speaking or safety_stopped:
            return []
        if not math.isfinite(dt) or dt < 0 or not math.isfinite(now_mono): raise ValueError("invalid time")
        self.accumulator_s += dt
        ticks = int((self.accumulator_s + 1e-12) / self.tick_s)
        if ticks <= 0: return []
        self.accumulator_s -= ticks * self.tick_s
        self._tick_index += ticks
        if current_intent is not None and current_intent.status in {IntentStatus.RUNNING, IntentStatus.WAITING}:
            return []
        if self.state == WillState.CANCELLED:
            return []  # explicit cancel is terminal until a new explicit request.
        if self.state == WillState.INTERRUPTED:
            if self.retry_count >= self.attempt_max_retry:
                self.state = WillState.ABANDONED
                self.history.append(WillTransition(self.state, self.intent_key, "retry_limit", now_mono))
                return []
            self.retry_count += 1
            self.state = WillState.ATTEMPTING
            self.history.append(WillTransition(self.state, self.intent_key, "bounded_retry_candidate", now_mono))
        waves = [(name, value) for name, value in field.action_waves if name not in self.action_waves_excluded]
        waves.sort(key=lambda kv: (-kv[1], kv[0]))
        if waves and waves[0][1] >= self.low_tension_threshold:
            dominant = waves[0][0]
            if self.intent_key is not None and dominant != self.intent_key:
                self.state = WillState.RECONSIDERING
                self.history.append(WillTransition(self.state, dominant, "dominant_wave_changed", now_mono))
            self.intent_key = dominant
            self.state = WillState.ATTEMPTING
            # A Will candidate has no action: IntentManager still owns activation.
            return [IntentCandidate(intent_type="life_will", goal=f"will:{dominant}", score=min(.64, .40 + .2 * waves[0][1]),
                                    urgency=0.0, reason=f"will_candidate:{dominant}", action_type=None,
                                    action_args={"will_key": dominant}, interruption_policy="REPLAN")]
        if all(v < self.low_tension_threshold for _, v in waves):
            # A deterministic hash-based draw is replayable and does not use process-global RNG.
            bucket = hashlib.sha256(f"will:{self._tick_index}".encode()).digest()[0] / 255.0
            if self.useless_probability > 0 and bucket < self.useless_probability:
                options = ("stretch", "hum", "idle_observe")
                choice = hashlib.sha256(f"useless:{self._tick_index}".encode()).digest()[0] % len(options)
                key = options[choice]
                self.state = WillState.ATTEMPTING; self.intent_key = key
                return [IntentCandidate(intent_type="life_will_useless", goal=f"will:useless:{key}", score=.46,
                                        urgency=0.0, reason=f"will_useless_candidate:{key}", action_type=None,
                                        action_args={"internal_only": True, "will_key": key}, interruption_policy="REPLAN")]
            if self.state not in {WillState.INTERRUPTED, WillState.CANCELLED}:
                self.state = WillState.IDLE; self.intent_key = None
        return []

    def snapshot(self) -> dict[str, Any]:
        return {"state": self.state.value, "intent_key": self.intent_key, "retry_count": self.retry_count,
                "accumulator_s": self.accumulator_s, "tick_index": self._tick_index,
                "interruption_cancelled": self._interruption_cancelled,
                "history": [{"state": x.state.value, "intent_key": x.intent_key, "reason": x.reason, "at_mono": x.at_mono} for x in self.history[-100:]]}

    def restore(self, data: Mapping[str, Any]) -> None:
        try:
            state = WillState(str(data.get("state", "IDLE")))
            retry = int(data.get("retry_count", 0)); accumulator = float(data.get("accumulator_s", 0.0)); tick = int(data.get("tick_index", 0))
            if retry < 0 or retry > self.attempt_max_retry or not math.isfinite(accumulator) or not 0 <= accumulator < self.tick_s or tick < 0: raise ValueError
            self.state, self.intent_key, self.retry_count = state, data.get("intent_key"), retry
            self.accumulator_s, self._tick_index = accumulator, tick
            self._interruption_cancelled = bool(data.get("interruption_cancelled", False))
        except (ValueError, TypeError):
            self.state, self.intent_key, self.retry_count, self.accumulator_s, self._tick_index = WillState.IDLE, None, 0, 0.0, 0
