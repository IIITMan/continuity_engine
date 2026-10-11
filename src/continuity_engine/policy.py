"""Intent management, switch hysteresis and bounded clarification."""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
import uuid
from typing import Callable, Iterable

from .models import Intent, IntentCandidate, IntentStatus, TERMINAL_INTENT_STATUSES, thaw


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    switch_margin: float = 0.15
    min_dwell_time_s: float = 10.0
    consecutive_evaluations_to_switch: int = 2
    ordinary_candidate_threshold: float = 0.65
    high_urgency_review_threshold: float = 0.80
    clarification_max_rounds: int = 2


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    intent: Intent | None
    action_required: bool
    changed: bool
    reason: str
    previous_intent_id: str | None = None


class IntentManager:
    def __init__(self, config: PolicyConfig | None = None, id_factory: Callable[[], str] | None = None):
        self.config = config or PolicyConfig()
        self.id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self.current: Intent | None = None
        self._pending_switch_key: tuple[str, str] | None = None
        self._pending_switch_count = 0

    def evaluate(
        self,
        candidates: Iterable[IntentCandidate],
        now_mono: float,
        *,
        emergency: bool = False,
        force_replan: bool = False,
    ) -> PolicyDecision:
        if now_mono < 0:
            raise ValueError("now_mono must be >= 0")
        candidate_list = list(candidates)
        eligible = [c for c in candidate_list if c.score >= self.config.ordinary_candidate_threshold or c.urgency >= self.config.high_urgency_review_threshold]
        eligible.sort(key=lambda c: (c.urgency >= self.config.high_urgency_review_threshold, c.score, c.urgency), reverse=True)
        best = eligible[0] if eligible else None

        if self.current is not None and self.current.status in TERMINAL_INTENT_STATUSES:
            self.current = None

        if self.current is None:
            if best is None:
                return PolicyDecision(None, False, False, "no_eligible_candidate")
            new_intent = self._start(best, now_mono)
            self.current = new_intent
            self._reset_switch()
            return PolicyDecision(new_intent, True, True, "initial_candidate_selected")

        current = self.current
        current_key = (current.intent_type, current.goal)
        if best is None:
            self._reset_switch()
            return PolicyDecision(current, False, False, "keep_current_no_candidate")
        best_key = (best.intent_type, best.goal)
        if best_key == current_key:
            # Refresh score/reason only; intent identity and dwell clock remain stable.
            current.score = best.score
            current.reason = best.reason
            self._reset_switch()
            return PolicyDecision(current, False, False, "keep_current_intent")

        dwell = max(0.0, now_mono - current.started_at_mono)
        clearly_better = best.score >= current.score + self.config.switch_margin
        high_urgency = best.urgency >= self.config.high_urgency_review_threshold
        switch_allowed = emergency or force_replan or high_urgency
        if not switch_allowed and current.status == IntentStatus.RUNNING and dwell < self.config.min_dwell_time_s:
            self._reset_switch()
            return PolicyDecision(current, False, False, "minimum_dwell_time")
        if not switch_allowed and not clearly_better:
            self._reset_switch()
            return PolicyDecision(current, False, False, "switch_margin_not_met")

        if not switch_allowed:
            if self._pending_switch_key == best_key:
                self._pending_switch_count += 1
            else:
                self._pending_switch_key = best_key
                self._pending_switch_count = 1
            if self._pending_switch_count < self.config.consecutive_evaluations_to_switch:
                return PolicyDecision(current, False, False, "awaiting_switch_confirmation")

        previous_id = current.intent_id
        current.status = IntentStatus.INTERRUPTED
        new_intent = self._start(best, now_mono)
        self.current = new_intent
        self._reset_switch()
        why = "emergency_override" if emergency else "forced_replan" if force_replan else "high_urgency_override" if high_urgency else "switch_confirmed"
        return PolicyDecision(new_intent, True, True, why, previous_id)

    def cancel_current(self, reason: str = "user_cancel") -> str | None:
        if self.current is None:
            return None
        self.current.status = IntentStatus.CANCELLED
        self.current.cancellation_requested = True
        self.current.generation += 1
        self.current.reason = reason
        old_id = self.current.intent_id
        self.current = None
        self._reset_switch()
        return old_id

    def finish_current(self, status: IntentStatus, reason: str = "action_terminal") -> None:
        if status not in TERMINAL_INTENT_STATUSES:
            raise ValueError("finish status must be terminal")
        if self.current is not None:
            self.current.status = status
            self.current.reason = reason
            self.current = None
        self._reset_switch()

    def interrupt_current(self, reason: str = "interrupted") -> Intent | None:
        if self.current is None:
            return None
        self.current.status = IntentStatus.INTERRUPTED
        self.current.reason = reason
        old = self.current
        self.current = None
        self._reset_switch()
        return old

    def _start(self, candidate: IntentCandidate, now_mono: float) -> Intent:
        return Intent(
            intent_id=self.id_factory(), intent_type=candidate.intent_type, goal=candidate.goal,
            status=IntentStatus.RUNNING if candidate.action_type is not None else IntentStatus.WAITING,
            score=candidate.score, created_at_mono=now_mono,
            started_at_mono=now_mono, reason=candidate.reason, action_type=candidate.action_type,
            action_args=thaw(candidate.action_args), deadline_mono=candidate.deadline_mono,
            interruption_policy=candidate.interruption_policy,
            ordinary_proactive=candidate.ordinary_proactive, is_safety_alert=candidate.is_safety_alert,
        )

    def _reset_switch(self) -> None:
        self._pending_switch_key = None
        self._pending_switch_count = 0


class ClarificationLimitReached(RuntimeError):
    pass


class ClarificationManager:
    """A persistent intent should store rounds/questions from this manager."""

    def __init__(self, max_rounds: int = 2):
        if max_rounds != 2:
            raise ValueError("v0.2 contract fixes max clarification rounds at 2")
        self.max_rounds = max_rounds

    @staticmethod
    def normalize(question: str) -> str:
        lowered = unicodedata.normalize("NFKC", question).casefold()
        return re.sub(r"[\W_]+", "", lowered, flags=re.UNICODE)

    def ask(self, intent: Intent, question: str) -> bool:
        if intent.clarification_rounds >= self.max_rounds:
            raise ClarificationLimitReached("maximum clarification rounds reached")
        key = self.normalize(question)
        if not key:
            raise ValueError("clarification question cannot be empty")
        if key in {self.normalize(q) for q in intent.clarification_questions}:
            return False
        intent.clarification_questions.append(question)
        intent.clarification_rounds += 1
        return True
