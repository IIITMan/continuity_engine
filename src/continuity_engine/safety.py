"""Policy-independent action safety and user-control filtering."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Iterable

from .models import ActionProposal, EngineState


@dataclass(frozen=True, slots=True)
class SafetyDecision:
    outcome: str  # ALLOW, DENY_HARD, DENY_NEED_INFO, DENY_TEMPORARY
    code: str
    reason: str


class SafetySupervisor:
    def __init__(
        self,
        *,
        allowed_action_types: Iterable[str] = ("SPEAK", "NOTIFY", "OBSERVE", "WAIT", "LIGHT"),
        proactive_max_count: int = 2,
        proactive_window_s: float = 3600.0,
        proactive_cooldown_s: float = 1200.0,
    ):
        self.allowed_action_types = frozenset(allowed_action_types)
        self.proactive_max_count = proactive_max_count
        self.proactive_window_s = proactive_window_s
        self.proactive_cooldown_s = proactive_cooldown_s
        self._proactive_starts: deque[float] = deque()
        self._last_proactive_at: float | None = None

    def check(self, proposal: ActionProposal, state: EngineState, now_mono: float) -> SafetyDecision:
        if proposal.action_type not in self.allowed_action_types:
            return SafetyDecision("DENY_HARD", "ACTION_NOT_ALLOWLISTED", "action type is not allowlisted")
        if state.safety_stopped and not proposal.is_safety_alert:
            return SafetyDecision("DENY_HARD", "SAFETY_STOP_LATCHED", "safety stop is active")
        if proposal.action_type == "SPEAK" and getattr(state, "output_muted", False):
            return SafetyDecision("DENY_HARD", "OUTPUT_MUTED", "output is muted")
        if proposal.action_type == "SPEAK" and state.user_speaking:
            return SafetyDecision("DENY_TEMPORARY", "USER_SPEAKING", "do not talk over the user")
        if proposal.ordinary_proactive:
            if state.quiet_scope is not None:
                return SafetyDecision("DENY_HARD", "QUIET_MODE", "ordinary proactive interaction is disabled")
            self._prune(now_mono)
            if self.proactive_max_count <= 0 or len(self._proactive_starts) >= self.proactive_max_count:
                return SafetyDecision("DENY_TEMPORARY", "PROACTIVE_RATE_LIMIT", "proactive message quota reached")
            if self._last_proactive_at is not None and now_mono - self._last_proactive_at < self.proactive_cooldown_s:
                return SafetyDecision("DENY_TEMPORARY", "PROACTIVE_COOLDOWN", "proactive cooldown active")
            if not proposal.reason_code or proposal.reason_code == "intent_selected":
                return SafetyDecision("DENY_HARD", "MISSING_PROACTIVE_REASON", "proactive action requires an auditable reason")
        return SafetyDecision("ALLOW", "OK", "action passed local policy checks")


    def reconfigure(self, *, proactive_max_count: int, proactive_window_s: float, proactive_cooldown_s: float) -> None:
        """Update whitelisted rate-limit parameters without erasing history."""
        if proactive_max_count < 0 or proactive_window_s <= 0 or proactive_cooldown_s < 0:
            raise ValueError("invalid proactive rate-limit configuration")
        self.proactive_max_count = proactive_max_count
        self.proactive_window_s = proactive_window_s
        self.proactive_cooldown_s = proactive_cooldown_s
        self._prune(max(self._proactive_starts[-1] if self._proactive_starts else 0.0, self._last_proactive_at or 0.0))

    def record_started(self, proposal: ActionProposal, now_mono: float) -> None:
        if proposal.ordinary_proactive:
            self._prune(now_mono)
            self._proactive_starts.append(now_mono)
            self._last_proactive_at = now_mono

    def _prune(self, now_mono: float) -> None:
        while self._proactive_starts and now_mono - self._proactive_starts[0] >= self.proactive_window_s:
            self._proactive_starts.popleft()
