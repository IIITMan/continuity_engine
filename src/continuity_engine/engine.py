"""Main Continuity Engine coordinator for the desktop companion MVP."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
import math
import time
import uuid
import threading
from typing import Any, Mapping

from .actions import ActionExecutor, ReplayActionExecutor, SimulatedActionExecutor
from .clock import Clock, SystemClock
from .config import ConfigManager
from .drives import DriveConfig, DriveSystem
from .events import ControlLaneFull, EventRouter
from .llm_gateway import LLMGateway, LLMResult, ReplayGuard, ReplayModeError
from .models import (
    ActionProposal, ActionResult, ActionStatus, DriveState, EngineState, Event, EventKind,
    IntentCandidate, IntentStatus, Priority, WorldStatus,
)
from .policy import ClarificationLimitReached, ClarificationManager, IntentManager, PolicyConfig
from .replay import ReplayNetworkGuard
from .safety import SafetyDecision, SafetySupervisor
from .state_views import StateViewBuilder
from .world_model import WorldModel, WorldModelConfig


@dataclass(frozen=True, slots=True)
class EngineStepResult:
    state: EngineState
    proposals: tuple[ActionProposal, ...]
    events_processed: tuple[str, ...]
    dropped_event_ids: tuple[str, ...]
    cancel_action_ids: tuple[str, ...]
    diagnostics: tuple[str, ...]


class ContinuityEngine:
    """A synchronous deterministic core; I/O is explicitly dispatched asynchronously.

    `step` updates core state and returns approved action proposals. The application
    decides when to dispatch them using `dispatch_action`; this keeps LLM/network/audio
    latency out of the state-update path.
    """

    def __init__(
        self,
        config: ConfigManager,
        *,
        clock: Clock | None = None,
        state: EngineState | None = None,
        action_executor: ActionExecutor | None = None,
        llm_gateway: LLMGateway | None = None,
        replay_mode: bool = False,
        world_model: WorldModel | None = None,
    ):
        self.config = config
        self.clock = clock or SystemClock()
        self.state = state or EngineState(config_version=config.version)
        self.action_executor: ActionExecutor = action_executor or SimulatedActionExecutor()
        self.llm_gateway = llm_gateway
        self.replay_mode = replay_mode
        self._replay_network_guard = None
        if replay_mode:
            ReplayGuard.validate(replay_mode=True,
                                 llm_adapter=None if llm_gateway is None else llm_gateway.adapter,
                                 action_executor=self.action_executor)
        self.router = EventRouter(
            max_queue_size=int(config.get()["runtime"]["max_queue_size"]),
            reserved_control_capacity=int(config.get()["runtime"]["queue_overload"]["reserved_control_capacity"]),
        )
        cfg = config.get()
        self.drives = DriveSystem(self._drive_configs(cfg), now_mono=self.clock.monotonic())
        if self.state.drives:
            self.drives.states = {k: DriveState(v.value, v.target, v.urgency, v.updated_at_mono) for k, v in self.state.drives.items()}
        else:
            self.state.drives = self.drives.snapshot()
        world_cfg = cfg["world_model"]
        self.world_model = world_model or WorldModel(WorldModelConfig(
            confirm_confidence=float(world_cfg.get("confirm_confidence", 0.90)),
            probable_confidence=float(world_cfg.get("probable_confidence", 0.55)),
            conflict_confidence=float(world_cfg.get("conflict_confidence", 0.60)),
        ))
        self.policy = IntentManager(self._policy_config(cfg))
        self.policy.current = self.state.current_intent
        self.clarifications = ClarificationManager(max_rounds=int(cfg["interaction"]["clarification_max_rounds_per_intent"]))
        self.safety = self._make_safety(cfg)
        self.state.config_version = config.version
        self.state_view_builder = StateViewBuilder(allowed_world_keys=set(cfg["llm"].get("world_fact_allowlist", [])))
        self._active_action_by_intent: dict[str, str] = {}
        self._action_to_intent: dict[str, str] = {}
        self._active_proposals: dict[str, ActionProposal] = {}
        self._inflight_action_ids: set[str] = set()
        self._pending_proposals: dict[str, ActionProposal] = {}
        self._active_map_lock = threading.RLock()
        self._async_loop: asyncio.AbstractEventLoop | None = None
        self._dropped_events_pending: list[str] = []
        self._generation_lock = threading.RLock()
        self._interaction_generation: dict[str, int] = {}
        self._event_generation_by_id: dict[str, int] = {}
        self.recovery_intent = None
        self._last_step_mono = self.clock.monotonic()
        self._diagnostics: list[str] = []
        self._lock = asyncio.Lock()
        # Enter the process-wide replay network guard only after construction succeeds;
        # a constructor exception must never leave sockets monkeypatched.
        if replay_mode:
            self._replay_network_guard = ReplayNetworkGuard()
            self._replay_network_guard.__enter__()

    @staticmethod
    def _drive_configs(cfg: Mapping[str, Any]) -> dict[str, DriveConfig]:
        result: dict[str, DriveConfig] = {}
        drive_cfg = cfg["drives"]["default"]
        initial_values = {"rest": 0.15, "safety": 0.10, "exploration": 0.10, "social": 0.15, "task": 0.0}
        for name, row in drive_cfg.items():
            tau = row.get("tau_s") or 3600.0
            result[name] = DriveConfig(float(tau), initial_values.get(name, 0.0), float(row.get("target") or 0.0), 0.1)
        return result

    @staticmethod
    def _policy_config(cfg: Mapping[str, Any]) -> PolicyConfig:
        p = cfg["policy"]
        return PolicyConfig(
            switch_margin=float(p["switch_margin"]), min_dwell_time_s=float(p["min_dwell_time_s"]),
            consecutive_evaluations_to_switch=int(p["consecutive_evaluations_to_switch"]),
            ordinary_candidate_threshold=float(p["ordinary_candidate_threshold"]),
            high_urgency_review_threshold=float(p["high_urgency_review_threshold"]),
            clarification_max_rounds=int(cfg["interaction"]["clarification_max_rounds_per_intent"]),
        )

    @staticmethod
    def _make_safety(cfg: Mapping[str, Any]) -> SafetySupervisor:
        p = cfg["interaction"]["ordinary_proactive_messages"]
        return SafetySupervisor(proactive_max_count=int(p["max_count"]),
                                proactive_window_s=float(p["rolling_window_s"]),
                                proactive_cooldown_s=float(p["cooldown_s"]))

    def inject(self, event: Event) -> None:
        receipt = self.router.enqueue(event, self.clock.monotonic())
        if receipt.dropped_event_ids:
            self._dropped_events_pending.extend(receipt.dropped_event_ids)
            with self._generation_lock:
                for dropped_event_id in receipt.dropped_event_ids:
                    self._event_generation_by_id.pop(dropped_event_id, None)
        if not receipt.accepted:
            self._diagnostics.append(f"event_rejected:{event.event_id}:{receipt.disposition}")
            return
        if event.kind == EventKind.USER_MESSAGE.value:
            interaction_id = event.payload.get("interaction_id")
            if isinstance(interaction_id, str) and interaction_id:
                # Invalidate old cognition work at ingress, before the next core tick.
                with self._generation_lock:
                    generation = self._interaction_generation.get(interaction_id, 0) + 1
                    self._interaction_generation[interaction_id] = generation
                    self._event_generation_by_id[event.event_id] = generation
                    if self.llm_gateway is not None:
                        self.llm_gateway.set_generation(interaction_id, generation)
        if event.kind in {EventKind.SAFETY_STOP.value, EventKind.USER_STOP.value, EventKind.USER_CANCEL.value,
                          EventKind.USER_MUTE.value, EventKind.USER_QUIET.value, EventKind.USER_SPEECH_STARTED.value}:
            self._schedule_fast_cancellation(event)

    def _schedule_fast_cancellation(self, event: Event) -> None:
        """Request cancellation promptly when a live asyncio loop is available.

        This is a high-level action cancellation request, not a physical emergency-stop
        guarantee. Physical safety paths must exist independently of this Python process.
        """
        with self._active_map_lock:
            active = list(self._active_proposals.values())
        target = event.payload.get("intent_id")
        ids: list[str] = []
        if event.kind in {EventKind.SAFETY_STOP.value, EventKind.USER_STOP.value}:
            ids = [p.action_id for p in active]
        elif event.kind == EventKind.USER_CANCEL.value:
            current_id = str(target) if target else (self.policy.current.intent_id if self.policy.current else None)
            ids = [p.action_id for p in active if current_id is not None and p.intent_id == current_id]
        elif event.kind in {EventKind.USER_MUTE.value, EventKind.USER_SPEECH_STARTED.value}:
            ids = [p.action_id for p in active if p.action_type == "SPEAK"]
        elif event.kind == EventKind.USER_QUIET.value and event.payload.get("enabled", True):
            ids = [p.action_id for p in active if p.ordinary_proactive and p.action_type == "SPEAK"]
        if not ids:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = self._async_loop
        if loop is None or not loop.is_running():
            # The latch still guarantees dispatch refusal; main-loop step returns cancellation IDs.
            return
        def schedule() -> None:
            for action_id in ids:
                loop.create_task(self.cancel_action(action_id))
        loop.call_soon_threadsafe(schedule)

    def new_event(self, kind: str, payload: Mapping[str, Any] | None = None, *, source: str = "internal",
                  priority: int = int(Priority.NORMAL), expires_in_s: float | None = None,
                  confidence: float | None = None, correlation_id: str | None = None) -> Event:
        now_mono = self.clock.monotonic()
        return Event(
            event_id=str(uuid.uuid4()), kind=kind, source=source, timestamp_mono=now_mono,
            timestamp_wall=self.clock.wall_time(), payload=payload or {}, confidence=confidence,
            priority=priority, expires_at_mono=None if expires_in_s is None else now_mono + expires_in_s,
            correlation_id=correlation_id,
        )

    def step(self, dt: float | None = None) -> EngineStepResult:
        """Process a bounded batch without awaiting any external service."""
        now = self.clock.monotonic()
        if dt is None:
            dt = now - self._last_step_mono
        if not math.isfinite(dt) or dt < 0:
            raise ValueError("dt must be finite and >= 0; state was not changed")
        self._diagnostics = []
        cfg_version_before = self.config.version
        self.config.apply_pending()  # atomic hot config swap at the decision boundary
        cfg = self.config.get()
        if self.config.version != cfg_version_before:
            self._apply_reloadable_config(cfg)
            self.state.config_version = self.config.version
            self._diagnostics.append(f"config_applied:{self.config.version}")

        latches = self.router.consume_latches()
        cancelled_action_ids: set[str] = set()
        if latches.degraded:
            self.state.safety_stopped = True
            cancelled_action_ids.update(self._cancel_current("control_lane_overflow_degraded"))
            self._diagnostics.append("control_lane_overflow:entered_safe_degraded_state")
        if latches.quiet_changed:
            self.state.quiet_scope = latches.quiet_scope
            self._diagnostics.append(f"control_latch:quiet_scope:{latches.quiet_scope}")
        self.state.user_speaking = latches.user_speaking
        if latches.safety_stop:
            self.state.safety_stopped = True
            cancelled_action_ids.update(self._cancel_current("safety_stop"))
            self._diagnostics.append("control_latch:safety_stop")
        if latches.user_stop:
            cancelled_action_ids.update(self._cancel_current("user_stop"))
            self._diagnostics.append("control_latch:user_stop")
        if latches.muted is True:
            self.state.output_muted = True
            if self.policy.current and self.policy.current.action_type == "SPEAK":
                cancelled_action_ids.update(self._cancel_current("output_muted"))
        elif latches.muted is False:
            self.state.output_muted = False
        for intent_id in latches.cancelled_intent_ids:
            if self.policy.current and self.policy.current.intent_id == intent_id:
                cancelled_action_ids.update(self._cancel_current("user_cancel"))
        if latches.cancel_all:
            cancelled_action_ids.update(self._cancel_current("user_cancel_all"))

        events, expired_at_dequeue = self.router.drain_with_drops(
            limit=int(cfg["runtime"]["event_batch_max"]), now_mono=now,
        )
        processed: list[str] = []
        dropped_ids: list[str] = list(expired_at_dequeue)
        if expired_at_dequeue:
            with self._generation_lock:
                for expired_event_id in expired_at_dequeue:
                    self._event_generation_by_id.pop(expired_event_id, None)
        candidates: list[IntentCandidate] = []
        emergency = latches.safety_stop or latches.user_stop
        force_replan = False
        for event in events:
            processed.append(event.event_id)
            try:
                new_candidates, event_emergency, event_force_replan, cancelled = self._process_event(event, now, cfg)
                candidates.extend(new_candidates)
                emergency = emergency or event_emergency
                force_replan = force_replan or event_force_replan
                cancelled_action_ids.update(cancelled)
            except (KeyError, TypeError, ValueError) as exc:
                self._diagnostics.append(f"event_invalid:{event.event_id}:{type(exc).__name__}")

        # Tick drives from a validated dt. Update is deterministic for fixed state/input/dt.
        try:
            self.drives.update(dt, now)
            self.state.drives = self.drives.snapshot()
        except (ValueError, OverflowError) as exc:
            self._diagnostics.append(f"drive_update_rejected:{type(exc).__name__}")
            # Preserve the last valid state; the core loop remains available.

        candidates = self._dedupe_candidates(candidates)
        policy_decision = self.policy.evaluate(candidates, now, emergency=emergency, force_replan=force_replan)
        if policy_decision.previous_intent_id:
            previous_id = policy_decision.previous_intent_id
            with self._active_map_lock:
                previous_action = self._active_action_by_intent.get(previous_id)
            if previous_action:
                cancelled_action_ids.add(previous_action)
            self._pending_proposals.pop(previous_id, None)
            if self.llm_gateway is not None:
                self.llm_gateway.invalidate_intent(previous_id)
        if self.policy.current is not None:
            self.state.current_intent = self.policy.current
        else:
            self.state.current_intent = None

        proposals: list[ActionProposal] = []
        # Pending proposals are an in-memory outbox: if the caller misses a step result,
        # the same idempotent proposal is offered again until dispatch or cancellation.
        maybe_intent: Any = None
        if policy_decision.action_required:
            maybe_intent = policy_decision.intent
        elif self.policy.current is not None and self.policy.current.status == IntentStatus.WAITING:
            maybe_intent = self.policy.current
        if maybe_intent is not None and maybe_intent.action_type:
            proposal = self._pending_proposals.get(maybe_intent.intent_id)
            if proposal is None:
                action_id = str(uuid.uuid4())
                proposal = ActionProposal(
                    action_id=action_id, intent_id=maybe_intent.intent_id,
                    action_type=maybe_intent.action_type, args=maybe_intent.action_args,
                    created_at_mono=now, deadline_mono=maybe_intent.deadline_mono,
                    ordinary_proactive=maybe_intent.ordinary_proactive,
                    is_safety_alert=maybe_intent.is_safety_alert,
                    reason_code=maybe_intent.reason or "intent_selected", idempotency_key=f"{maybe_intent.intent_id}:{maybe_intent.generation}:{maybe_intent.action_type}",
                )
            safety = self.safety.check(proposal, self.state, now)
            if safety.outcome == "ALLOW":
                maybe_intent.status = IntentStatus.RUNNING
                self._pending_proposals[maybe_intent.intent_id] = proposal
            elif safety.outcome == "DENY_TEMPORARY":
                maybe_intent.status = IntentStatus.WAITING
                maybe_intent.reason = f"waiting:{safety.code}"
                self._diagnostics.append(f"action_waiting:{maybe_intent.intent_id}:{safety.code}")
            else:
                self._pending_proposals.pop(maybe_intent.intent_id, None)
                maybe_intent.status = IntentStatus.CANCELLED
                maybe_intent.reason = f"safety_denied:{safety.code}"
                self._diagnostics.append(f"action_denied:{maybe_intent.intent_id}:{safety.code}")
                self.policy.finish_current(IntentStatus.CANCELLED, maybe_intent.reason)
                self.state.current_intent = None
        current = self.policy.current
        if current is not None and current.intent_id in self._pending_proposals and current.status == IntentStatus.RUNNING:
            proposals.append(self._pending_proposals[current.intent_id])

        self.state.world = self.world_model.snapshot(now)
        self.state.drives = self.drives.snapshot()
        self.state.current_intent = self.policy.current
        self.state.state_version += 1
        self.state.last_reason = policy_decision.reason
        self.state.validate()
        self._last_step_mono = now
        reported_drops = tuple(self._dropped_events_pending + dropped_ids)
        self._dropped_events_pending.clear()
        return EngineStepResult(
            state=deepcopy(self.state), proposals=tuple(proposals), events_processed=tuple(processed),
            dropped_event_ids=reported_drops, cancel_action_ids=tuple(sorted(cancelled_action_ids)),
            diagnostics=tuple(self._diagnostics),
        )

    def _process_event(self, event: Event, now: float, cfg: Mapping[str, Any]) -> tuple[list[IntentCandidate], bool, bool, set[str]]:
        candidates: list[IntentCandidate] = []
        emergency = False
        force_replan = False
        cancel_ids: set[str] = set()
        payload = dict(event.payload)
        kind = event.kind
        if kind == EventKind.SAFETY_RESET.value:
            # Reset only through an explicit trusted operator/safety source after health check.
            if event.source in {"operator_control", "safety_supervisor"} and payload.get("health_check_passed") is True:
                self.state.safety_stopped = False
                self.router.reset_degraded_after_health_check()
                self._diagnostics.append("safety_stop_reset_after_health_check")
            else:
                self._diagnostics.append("safety_reset_rejected")
        elif kind == EventKind.SAFETY_STOP.value:
            self.state.safety_stopped = True
            cancel_ids |= self._cancel_current("safety_stop")
            emergency = True
        elif kind == EventKind.USER_STOP.value:
            cancel_ids |= self._cancel_current("user_stop")
            emergency = True
        elif kind == EventKind.USER_CANCEL.value:
            target = payload.get("intent_id")
            if self.policy.current is not None and (target is None or target == self.policy.current.intent_id):
                cancel_ids |= self._cancel_current("user_cancel")
                if target and self.llm_gateway is not None:
                    self.llm_gateway.invalidate_intent(str(target))
            self._invalidate_correlated_generation(payload)
            force_replan = True
        elif kind == EventKind.USER_MUTE.value:
            self.state.output_muted = True
            if self.policy.current and self.policy.current.action_type == "SPEAK":
                cancel_ids |= self._cancel_current("output_muted")
        elif kind == EventKind.USER_UNMUTE.value:
            self.state.output_muted = False
        elif kind == EventKind.USER_QUIET.value:
            if not payload.get("enabled", True):
                self.state.quiet_scope = None
                self.state.quiet_until_wall = None
            else:
                scope = str(payload.get("scope", cfg["interaction"].get("default_quiet_scope", "SESSION"))).upper()
                if scope not in {"TURN", "SESSION", "PERSISTENT"}:
                    raise ValueError("invalid quiet scope")
                self.state.quiet_scope = scope
                self.state.quiet_until_wall = payload.get("until_wall")
        elif kind == EventKind.USER_SPEECH_STARTED.value:
            self.state.user_speaking = True
            if self.policy.current and self.policy.current.action_type == "SPEAK":
                cancel_ids |= self._cancel_current("user_barge_in")
        elif kind == EventKind.USER_SPEECH_ENDED.value:
            self.state.user_speaking = False
        elif kind == EventKind.USER_MESSAGE.value:
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("USER_MESSAGE needs non-empty payload.text")
            interaction_id = payload.get("interaction_id")
            generation = None
            if isinstance(interaction_id, str) and interaction_id:
                with self._generation_lock:
                    generation = self._event_generation_by_id.pop(event.event_id, None)
                    latest_generation = self._interaction_generation.get(interaction_id, generation or 0)
                if generation is not None and generation < latest_generation:
                    self._diagnostics.append(f"user_message_superseded:{event.event_id}")
                    return candidates, emergency, force_replan, cancel_ids
                if generation is not None:
                    self.state.generation_by_interaction[interaction_id] = generation
            self.state.last_user_message = text.strip()
            # A gateway-backed interaction waits for a validated response; the core loop
            # never blocks on inference. Without interaction_id/gateway, use local fallback.
            llm_backed = self.llm_gateway is not None and isinstance(interaction_id, str) and bool(interaction_id)
            if not self.state.user_speaking:
                candidates.append(IntentCandidate(
                    intent_type="respond_to_user",
                    goal=f"respond_to_interaction:{interaction_id}" if llm_backed else f"respond_to_message:{event.event_id}",
                    score=0.95, urgency=0.7,
                    reason="awaiting_llm" if llm_backed else "direct_user_message",
                    action_type=None if llm_backed else "SPEAK",
                    action_args={} if llm_backed else {"text": "我听到了。", "mode": "local_fallback"},
                    interruption_policy="REPLAN_IF_STILL_VALID",
                ))
                force_replan = True
        elif kind == EventKind.INTENT_ACTION_READY.value:
            intent_id = str(payload.get("intent_id", ""))
            interaction_id = str(payload.get("interaction_id", ""))
            generation = payload.get("generation")
            current = self.policy.current
            with self._generation_lock:
                latest_generation = self._interaction_generation.get(interaction_id)
            if (current is None or current.intent_id != intent_id or not interaction_id
                    or not isinstance(generation, int) or isinstance(generation, bool)
                    or generation != latest_generation
                    or self.state.generation_by_interaction.get(interaction_id) != generation):
                self._diagnostics.append(f"llm_action_ready_stale:{event.event_id}")
            else:
                action_type = payload.get("action_type")
                action_args = payload.get("action_args", {})
                if not isinstance(action_type, str) or not action_type or not isinstance(action_args, Mapping):
                    raise ValueError("INTENT_ACTION_READY needs action_type and mapping action_args")
                validated_candidate = IntentCandidate(
                    intent_type=current.intent_type, goal=current.goal, score=current.score,
                    reason="llm_response_ready", action_type=action_type, action_args=action_args,
                    urgency=0.0, interruption_policy=current.interruption_policy,
                )
                current.action_type = validated_candidate.action_type
                current.action_args = dict(validated_candidate.action_args)
                current.status = IntentStatus.WAITING
                current.reason = "llm_response_ready"
                self.state.current_intent = current
        elif kind == EventKind.WORLD_OBSERVATION.value:
            key = str(payload["key"])
            value = payload.get("value")
            confidence = event.confidence if event.confidence is not None else float(payload.get("confidence", 0.5))
            ttl = float(payload.get("ttl_s", self._ttl_for(payload.get("observation_type", key), cfg)))
            fact = self.world_model.observe(
                key=key, value=value, confidence=confidence, source=event.source,
                observed_at_mono=event.timestamp_mono, ttl_s=ttl, event_id=event.event_id,
            )
            self._diagnostics.append(f"world_update:{key}:{fact.status.value}")
        elif kind == EventKind.DRIVE_IMPULSE.value or kind == EventKind.NOVEL_EVENT.value:
            self.drives.apply_event(event)
            if kind == EventKind.NOVEL_EVENT.value and payload.get("worth_observing", False):
                exp = self.drives.states.get("exploration")
                score = min(0.95, 0.60 + (exp.value if exp else 0.1) * 0.35)
                candidates.append(IntentCandidate(
                    intent_type="observe_event", goal=str(payload.get("goal", "inspect_novel_event")),
                    score=score, urgency=float(payload.get("urgency", 0.2)),
                    reason="novel_event_worth_observing", action_type="OBSERVE",
                    action_args={"event_id": event.event_id, "summary": str(payload.get("summary", ""))[:500]},
                    interruption_policy="REPLAN",
                ))
        elif kind == "INTENT_CANDIDATE":
            # Candidate intent requests are treated as untrusted input and validated by DTO construction.
            candidates.append(IntentCandidate(
                intent_type=str(payload["intent_type"]), goal=str(payload["goal"]), score=float(payload["score"]),
                reason=str(payload.get("reason", "external_candidate")), action_type=payload.get("action_type"),
                action_args=payload.get("action_args", {}), urgency=float(payload.get("urgency", 0.0)),
                risk_level=str(payload.get("risk_level", "low")), deadline_mono=payload.get("deadline_mono"),
                interruption_policy=str(payload.get("interruption_policy", "REPLAN")),
                ordinary_proactive=bool(payload.get("ordinary_proactive", False)),
                is_safety_alert=bool(payload.get("is_safety_alert", False)),
            ))
        elif kind == EventKind.ACTION_TERMINAL.value:
            action_id = str(payload.get("action_id", ""))
            intent_id = str(payload.get("intent_id", ""))
            status = str(payload.get("status", "UNKNOWN"))
            mapped_intent = self._action_to_intent.get(action_id)
            with self._active_map_lock:
                self._inflight_action_ids.discard(action_id)
            if mapped_intent != intent_id:
                self._diagnostics.append(f"action_result_ignored_bad_correlation:{action_id}")
            elif self.policy.current is not None and self.policy.current.intent_id == intent_id:
                if status == ActionStatus.SUCCEEDED.value:
                    self.policy.finish_current(IntentStatus.SUCCEEDED, "action_succeeded")
                elif status == ActionStatus.CANCELLED.value:
                    self.policy.finish_current(IntentStatus.CANCELLED, "action_cancelled")
                elif status == ActionStatus.FAILED.value:
                    self.policy.finish_current(IntentStatus.FAILED, "action_failed")
                else:
                    # UNKNOWN/TIMED_OUT may have external side effects. Do not blindly retry.
                    self.policy.current.status = IntentStatus.WAITING
                    self.policy.current.action_type = None
                    self.policy.current.reason = f"action_result_{status.lower()}_requires_replan"
                    self._diagnostics.append(f"action_result_requires_replan:{action_id}:{status}")
                self.state.current_intent = self.policy.current
            with self._active_map_lock:
                self._action_to_intent.pop(action_id, None)
                self._active_action_by_intent.pop(intent_id, None)
        elif kind in {EventKind.TIMER.value, EventKind.CONFIG_RELOAD.value}:
            pass
        return candidates, emergency, force_replan, cancel_ids

    @staticmethod
    def _ttl_for(observation_type: Any, cfg: Mapping[str, Any]) -> float:
        world_cfg = cfg["world_model"]
        if observation_type in {"presence", "person_presence"}:
            return float(world_cfg["presence_observation_ttl_s"])
        if observation_type in {"identity", "person_identity"}:
            return float(world_cfg["identity_observation_ttl_s"])
        if observation_type in {"sound", "transient_sound"}:
            return float(world_cfg["transient_sound_ttl_s"])
        return float(world_cfg["presence_observation_ttl_s"])

    def _cancel_current(self, reason: str) -> set[str]:
        active: set[str] = set()
        current = self.policy.current
        if current is not None:
            with self._active_map_lock:
                action_id = self._active_action_by_intent.get(current.intent_id)
                if action_id:
                    active.add(action_id)
            self._pending_proposals.pop(current.intent_id, None)
            if self.llm_gateway is not None:
                self.llm_gateway.invalidate_intent(current.intent_id)
            self.policy.cancel_current(reason)
            self.state.current_intent = None
        return active

    def _invalidate_correlated_generation(self, payload: Mapping[str, Any]) -> None:
        interaction_id = payload.get("interaction_id")
        if interaction_id:
            iid = str(interaction_id)
            with self._generation_lock:
                current = self._interaction_generation.get(iid, self.state.generation_by_interaction.get(iid, 0))
                next_generation = current + 1
                self._interaction_generation[iid] = next_generation
                self.state.generation_by_interaction[iid] = next_generation
            if self.llm_gateway is not None:
                self.llm_gateway.set_generation(iid, next_generation)
        elif payload.get("intent_id") and self.llm_gateway is not None:
            self.llm_gateway.invalidate_intent(str(payload["intent_id"]))

    @staticmethod
    def _dedupe_candidates(candidates: list[IntentCandidate]) -> list[IntentCandidate]:
        unique: dict[tuple[str, str], IntentCandidate] = {}
        for candidate in candidates:
            key = (candidate.intent_type, candidate.goal)
            previous = unique.get(key)
            if previous is None or (candidate.score, candidate.urgency) > (previous.score, previous.urgency):
                unique[key] = candidate
        return list(unique.values())

    def _apply_reloadable_config(self, cfg: Mapping[str, Any]) -> None:
        self.policy.config = self._policy_config(cfg)
        p = cfg["interaction"]["ordinary_proactive_messages"]
        self.safety.reconfigure(proactive_max_count=int(p["max_count"]), proactive_window_s=float(p["rolling_window_s"]), proactive_cooldown_s=float(p["cooldown_s"]))
        drive_cfg = cfg["drives"]["default"]
        for name, row in drive_cfg.items():
            if name in self.drives.configs and row.get("tau_s") is not None:
                old = self.drives.configs[name]
                self.drives.configs[name] = DriveConfig(float(row["tau_s"]), old.initial_value, old.initial_target, old.event_gain)

    async def dispatch_action(self, proposal: ActionProposal) -> tuple[SafetyDecision, ActionResult | None]:
        """Re-check safety at dispatch time, execute adapter, enqueue a terminal receipt."""
        async with self._lock:
            self._async_loop = asyncio.get_running_loop()
            current = self.policy.current
            if current is None or current.intent_id != proposal.intent_id or current.status in {
                IntentStatus.CANCELLED, IntentStatus.FAILED, IntentStatus.EXPIRED, IntentStatus.SUCCEEDED
            }:
                return SafetyDecision("DENY_HARD", "STALE_INTENT", "intent is no longer active"), None
            if proposal.action_id in self._inflight_action_ids:
                return SafetyDecision("DENY_HARD", "DUPLICATE_ACTION", "action is already in flight"), None
            pending = self._pending_proposals.get(proposal.intent_id)
            if pending is not None and pending.action_id != proposal.action_id:
                return SafetyDecision("DENY_HARD", "STALE_PROPOSAL", "proposal is not the current pending action"), None
            fast_latches = self.router.peek_latches()
            fast_denial = self._fast_latch_denial(proposal, fast_latches)
            if fast_denial is not None:
                return fast_denial, None
            safety = self.safety.check(proposal, self.state, self.clock.monotonic())
            if safety.outcome != "ALLOW":
                if safety.outcome == "DENY_TEMPORARY":
                    current.status = IntentStatus.WAITING
                    current.reason = f"waiting:{safety.code}"
                else:
                    self._pending_proposals.pop(proposal.intent_id, None)
                    self.policy.finish_current(IntentStatus.CANCELLED, f"safety_denied:{safety.code}")
                    self.state.current_intent = None
                    self.state.state_version += 1
                return safety, None
            self._pending_proposals.pop(proposal.intent_id, None)
            with self._active_map_lock:
                self._inflight_action_ids.add(proposal.action_id)
                self._active_action_by_intent[proposal.intent_id] = proposal.action_id
                self._action_to_intent[proposal.action_id] = proposal.intent_id
                self._active_proposals[proposal.action_id] = proposal
        # Close the race where a stop/barge-in arrives after the initial check but before
        # the executor starts. Once registered as active, subsequent events can cancel it.
        late_denial = self._fast_latch_denial(proposal, self.router.peek_latches())
        if late_denial is not None:
            with self._active_map_lock:
                self._inflight_action_ids.discard(proposal.action_id)
                self._active_proposals.pop(proposal.action_id, None)
                self._action_to_intent.pop(proposal.action_id, None)
                if self._active_action_by_intent.get(proposal.intent_id) == proposal.action_id:
                    self._active_action_by_intent.pop(proposal.intent_id, None)
            if late_denial.outcome == "DENY_TEMPORARY" and self.policy.current is not None and self.policy.current.intent_id == proposal.intent_id:
                self.policy.current.status = IntentStatus.WAITING
                self.policy.current.reason = f"waiting:{late_denial.code}"
            return late_denial, None
        self.safety.record_started(proposal, self.clock.monotonic())
        try:
            result = await self.action_executor.execute(proposal)
            if not isinstance(result, ActionResult):
                raise TypeError("executor must return ActionResult")
            if result.action_id != proposal.action_id or result.intent_id != proposal.intent_id:
                result = ActionResult(proposal.action_id, proposal.intent_id, ActionStatus.UNKNOWN,
                                      completed_at_mono=self.clock.monotonic(),
                                      error_code="EXECUTOR_RESULT_CORRELATION_FAILURE")
            elif result.completed_at_mono is None:
                result = ActionResult(result.action_id, result.intent_id, result.status,
                                      completed_at_mono=self.clock.monotonic(), error_code=result.error_code,
                                      details=result.details)
        except Exception as exc:
            result = ActionResult(proposal.action_id, proposal.intent_id, ActionStatus.UNKNOWN,
                                  completed_at_mono=self.clock.monotonic(), error_code=f"EXECUTOR_{type(exc).__name__}")
        with self._active_map_lock:
            self._active_proposals.pop(proposal.action_id, None)
        event = self.new_event(EventKind.ACTION_TERMINAL.value, {
            "action_id": result.action_id, "intent_id": result.intent_id, "status": result.status.value,
            "error_code": result.error_code,
        }, source="action_executor", priority=int(Priority.CRITICAL), correlation_id=proposal.intent_id)
        self.inject(event)
        return safety, result

    @staticmethod
    def _fast_latch_denial(proposal: ActionProposal, latches: Any) -> SafetyDecision | None:
        if latches.safety_stop or latches.user_stop or latches.degraded:
            return SafetyDecision("DENY_HARD", "CONTROL_LATCHED", "a stop/degraded control is pending")
        if latches.cancel_all or proposal.intent_id in latches.cancelled_intent_ids:
            return SafetyDecision("DENY_HARD", "CANCEL_LATCHED", "intent cancellation is pending")
        if proposal.action_type == "SPEAK" and latches.muted is True:
            return SafetyDecision("DENY_HARD", "MUTE_LATCHED", "output mute is pending")
        if proposal.action_type == "SPEAK" and (latches.user_speaking or latches.speech_started):
            return SafetyDecision("DENY_TEMPORARY", "USER_SPEAKING", "user speech/barge-in is pending")
        if proposal.ordinary_proactive and latches.quiet_changed and latches.quiet_scope is not None:
            return SafetyDecision("DENY_HARD", "QUIET_LATCHED", "quiet mode is pending")
        return None

    async def cancel_action(self, action_id: str) -> bool:
        """Request cancellation from executor; true means request accepted, not physical stop confirmed."""
        try:
            return await self.action_executor.cancel(action_id)
        except Exception as exc:
            self._diagnostics.append(f"action_cancel_error:{action_id}:{type(exc).__name__}")
            return False

    def ask_clarification(self, intent_id: str, question: str, *, risk_level: str = "low") -> bool:
        """Record a clarification question under AD-007; return False when deduped/limited."""
        current = self.policy.current
        if current is None or current.intent_id != intent_id:
            raise ValueError("clarification must belong to the current active intent")
        try:
            return self.clarifications.ask(current, question)
        except ClarificationLimitReached:
            if risk_level.lower() in {"high", "critical", "irreversible", "privacy_sensitive"}:
                self.policy.finish_current(IntentStatus.CANCELLED, "clarification_limit_high_risk")
                self.state.current_intent = None
            else:
                current.status = IntentStatus.WAITING
                current.action_type = None
                current.reason = "clarification_limit_safe_default_or_explain"
                self.state.current_intent = current
            self.state.state_version += 1
            return False

    async def request_llm(
        self, *, purpose: str, interaction_id: str, generation: int, user_input: str | None = None,
        intent_id: str | None = None, **view_kwargs: Any,
    ) -> LLMResult:
        """Build a filtered state view, then use the gateway outside `step`.

        This method never commits a response or memory. For dialogue, pass a completed
        response through submit_llm_response(); for plans/memories, use their dedicated
        validated workflows instead of mutating EngineState directly.
        """
        if self.llm_gateway is None:
            from .llm_gateway import LLMStatus
            return LLMResult("", status=LLMStatus.FAILED, error_code="LLM_NOT_CONFIGURED")
        with self._generation_lock:
            current_generation = self._interaction_generation.get(interaction_id)
            if current_generation is None:
                self._interaction_generation[interaction_id] = generation
                current_generation = generation
            if generation != current_generation:
                from .llm_gateway import LLMStatus
                return LLMResult("", LLMStatus.STALE, error_code="ENGINE_GENERATION_STALE")
        if purpose == "multi_step_plan":
            # Caller asks for candidate tools, but may never expand the product allowlist.
            requested_tools = set(view_kwargs.get("allowed_tools", []))
            configured_tools = set(self.config.get()["llm"].get("allowed_tools", []))
            view_kwargs["allowed_tools"] = sorted(requested_tools & configured_tools)
        view = self.state_view_builder.build(purpose, self.state, user_input=user_input, **view_kwargs)
        self.llm_gateway.set_generation(interaction_id, generation)
        result = await self.llm_gateway.generate(interaction_id=interaction_id, intent_id=intent_id,
                                                 generation=generation, purpose=purpose, view=view)
        from .llm_gateway import LLMStatus
        if result.status == LLMStatus.COMPLETED:
            with self._generation_lock:
                newest_generation = self._interaction_generation.get(interaction_id)
            if newest_generation != generation:
                return LLMResult(result.request_id, LLMStatus.STALE, error_code="ENGINE_GENERATION_STALE", latency_s=result.latency_s)
            if intent_id is not None and (self.policy.current is None or self.policy.current.intent_id != intent_id):
                return LLMResult(result.request_id, LLMStatus.STALE, error_code="INTENT_NO_LONGER_ACTIVE", latency_s=result.latency_s)
        return result

    def current_generation(self, interaction_id: str) -> int:
        """Return the current ingress generation for an interaction (0 before first input)."""
        with self._generation_lock:
            return self._interaction_generation.get(interaction_id, 0)

    def submit_llm_response(
        self, *, intent_id: str, interaction_id: str, generation: int, response_text: str,
    ) -> bool:
        """Submit validated dialogue text as a versioned event; stale results return False."""
        if not isinstance(response_text, str) or not response_text.strip() or len(response_text) > 8000:
            raise ValueError("response_text must contain 1..8000 characters")
        with self._generation_lock:
            current_generation = self._interaction_generation.get(interaction_id)
            persisted_generation = self.state.generation_by_interaction.get(interaction_id)
        if current_generation != generation or persisted_generation != generation:
            return False
        current = self.policy.current
        if current is None or current.intent_id != intent_id or current.intent_type != "respond_to_user":
            return False
        event = self.new_event(
            EventKind.INTENT_ACTION_READY.value,
            {"intent_id": intent_id, "interaction_id": interaction_id, "generation": generation,
             "action_type": "SPEAK", "action_args": {"text": response_text.strip(), "mode": "llm"}},
            source="llm_gateway", priority=int(Priority.HIGH), expires_in_s=10.0, correlation_id=intent_id,
        )
        try:
            receipt = self.router.enqueue(event, self.clock.monotonic())
        except BufferError:
            self._diagnostics.append("llm_response_rejected:queue_backpressure")
            return False
        if receipt.dropped_event_ids:
            self._dropped_events_pending.extend(receipt.dropped_event_ids)
            with self._generation_lock:
                for dropped_event_id in receipt.dropped_event_ids:
                    self._event_generation_by_id.pop(dropped_event_id, None)
        if not receipt.accepted:
            self._diagnostics.append(f"llm_response_rejected:{receipt.disposition}")
            return False
        return True

    def snapshot(self) -> EngineState:
        return deepcopy(self.state)

    def save(self, repository: Any) -> None:
        repository.save_state(self.state, self.clock.wall_time())

    def close(self) -> None:
        """Release replay network guard and adapters owned by the application."""
        if self._replay_network_guard is not None:
            self._replay_network_guard.__exit__(None, None, None)
            self._replay_network_guard = None
        close = getattr(self.action_executor, "close", None)
        if callable(close):
            close()

    def restore(self, repository: Any) -> bool:
        restored = repository.load_state()
        if restored is None:
            return False
        # Restored observation facts must be aged against monotonic time only within the same boot.
        # Since monotonic epochs generally don't survive restart, world evidence is conservatively stale.
        now = self.clock.monotonic()
        for key, fact in list(restored.world.items()):
            restored.world[key] = type(fact)(key, None, WorldStatus.STALE, 0.0, fact.sources, now, now, fact.evidence_event_ids)
        restored.safety_stopped = bool(restored.safety_stopped)
        restored.current_intent = None if restored.current_intent and restored.current_intent.status in {
            IntentStatus.CANCELLED, IntentStatus.FAILED, IntentStatus.SUCCEEDED, IntentStatus.EXPIRED
        } else restored.current_intent
        self.state = restored
        # Generations are bumped on recovery so no persisted/late async result can
        # be accepted as the continuation of an interaction from before restart.
        restored_generations = dict(restored.generation_by_interaction)
        with self._generation_lock:
            self._interaction_generation = {
                interaction_id: generation + 1
                for interaction_id, generation in restored_generations.items()
            }
            self._event_generation_by_id.clear()
            for interaction_id, generation in self._interaction_generation.items():
                self.state.generation_by_interaction[interaction_id] = generation
                if self.llm_gateway is not None:
                    self.llm_gateway.set_generation(interaction_id, generation)
        self.world_model.restore_facts(restored.world)
        self.recovery_intent = None
        if restored.current_intent is not None and restored.current_intent.status not in {
            IntentStatus.CANCELLED, IntentStatus.FAILED, IntentStatus.SUCCEEDED, IntentStatus.EXPIRED
        }:
            # Never replay an old physical/speech action blindly after process restart.
            self.recovery_intent = deepcopy(restored.current_intent)
            self.recovery_intent.status = IntentStatus.INTERRUPTED
            self.recovery_intent.reason = "restart_requires_replan"
            self.recovery_intent.action_type = None
            restored.current_intent = None
        self.policy.current = restored.current_intent
        cfg_drive_defaults = self._drive_configs(self.config.get())
        default_drives = {k: DriveState(v.initial_value, v.initial_target, 0.0, now) for k, v in cfg_drive_defaults.items()}
        unknown_drives = set(restored.drives) - set(self.drives.configs)
        if unknown_drives:
            raise ValueError(f"persisted state has unknown drive fields: {sorted(unknown_drives)}")
        merged_drives = {k: DriveState(v.value, v.target, v.urgency, now) for k, v in default_drives.items()}
        merged_drives.update({k: DriveState(v.value, v.target, v.urgency, now) for k, v in restored.drives.items()})
        self.drives.states = merged_drives
        self.state.drives = self.drives.snapshot()
        self.state.last_reason = "restored_conservative_mode"
        self._last_step_mono = now
        return True
