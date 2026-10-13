"""Main Continuity Engine coordinator for the desktop companion MVP."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field, replace
import math
import time
import uuid
import threading
from typing import Any, Mapping

from ..execution.actions import ActionExecutor, ReplayActionExecutor, SimulatedActionExecutor
from ..state.autonomy import AutonomousBehaviorGenerator
from ..state.affect_personality import AffectPersonality
from ..state.life_layer import LifeLayer
from ..execution.expression import prepare_speech_segments
from ..execution.body import BodyController
from ..state.interaction_state import InteractionState
from .clock import Clock, SystemClock
from .config import ConfigManager
from ..state.drives import DriveConfig, DriveSystem
from ..state.events import ControlLaneFull, EventRouter
from ..llm.llm_gateway import LLMGateway, LLMResult, ReplayGuard, ReplayModeError
from .models import (
    ActionProposal, ActionResult, ActionStatus, DriveState, EngineState, Event, EventKind,
    IntentCandidate, IntentStatus, Priority, WorldStatus, thaw,
)
from ..state.policy import ClarificationLimitReached, ClarificationManager, IntentManager, PolicyConfig
from ..state.replay import ReplayNetworkGuard
from ..state.safety import SafetyDecision, SafetySupervisor
from ..llm.state_views import StateViewBuilder
from ..tasks.task_manager import TaskManager, TaskPlanError, TaskStatus
from ..state.world_model import WorldModel, WorldModelConfig


@dataclass(frozen=True, slots=True)
class EngineStepResult:
    state: EngineState
    proposals: tuple[ActionProposal, ...]
    events_processed: tuple[str, ...]
    dropped_event_ids: tuple[str, ...]
    cancel_action_ids: tuple[str, ...]
    diagnostics: tuple[str, ...]
    # Stable, minimized request for the host planner after a definite task failure.
    # The core never invokes an LLM automatically from step().
    replan_request: Mapping[str, Any] | None = None
    degraded: bool = False


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
        body_controller: BodyController | None = None,
    ):
        self.config = config
        self.clock = clock or SystemClock()
        self.affect_personality = AffectPersonality.from_mapping(config.get())
        self.interaction_state = InteractionState()
        self.state = state or EngineState(config_version=config.version)
        self.action_executor: ActionExecutor = action_executor or SimulatedActionExecutor()
        self.body_controller = body_controller
        body_cfg = config.get().get("life_layer", {}).get("body", {})
        self.body_enabled = bool(config.get().get("life_layer", {}).get("enabled", True)) and bool(body_cfg.get("enabled", False)) and body_controller is not None and body_controller.enabled and body_controller.driver is not None
        self.llm_gateway = llm_gateway
        self.memory_repository: Any | None = None
        self._degraded_interactions: set[tuple[str, int]] = set()
        self._fallback_responses: set[tuple[str, int]] = set()
        self._degraded_pending = False
        self.replay_mode = replay_mode
        self._replay_network_guard = None
        if replay_mode:
            if self.body_enabled:
                raise ReplayModeError("replay mode forbids physical BodyDriver execution")
            ReplayGuard.validate(replay_mode=True,
                                 llm_adapter=None if llm_gateway is None else llm_gateway.adapter,
                                 action_executor=self.action_executor)
        self.router = EventRouter(
            max_queue_size=int(config.get()["runtime"]["max_queue_size"]),
            reserved_control_capacity=int(config.get()["runtime"]["queue_overload"]["reserved_control_capacity"]),
        )
        cfg = config.get()
        self.drives = DriveSystem(self._drive_configs(cfg), now_mono=self.clock.monotonic())
        self.autonomy = AutonomousBehaviorGenerator.from_mapping(cfg, now_mono=self.clock.monotonic())
        self.life_layer = LifeLayer(cfg.get("life_layer", {}), now_mono=self.clock.monotonic(), now_wall=self.clock.wall_time())
        if self.state.life_layer_state:
            self.life_layer.restore(self.state.life_layer_state)
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
        self.safety = self._make_safety(cfg, body_available=self.body_enabled)
        self._temperament_params: dict[str, float | int] = {"proactive_scale": 1.0, "verbosity_chars": 214, "warmth": .68, "exploration_threshold_scale": 1.0, "arousal_response_scale": 1.0, "startle_expression_scale": 1.0}
        self.state.config_version = config.version
        self.state_view_builder = StateViewBuilder(allowed_world_keys=set(cfg["llm"].get("world_fact_allowlist", [])))
        task_cfg = cfg.get("tasks", {})
        self.task_manager = TaskManager(
            allowed_action_types=tuple(self.safety.allowed_action_types),
            allowed_world_keys=tuple(task_cfg.get("observe_world_key_allowlist", [])),
            max_steps=int(task_cfg.get("max_steps", 8)),
            max_plan_json_bytes=int(task_cfg.get("max_plan_json_bytes", 16_384)),
            history_limit=int(task_cfg.get("history_limit", 20)),
            default_priority=float(task_cfg.get("default_priority", 0.88)),
            default_step_timeout_s=float(task_cfg.get("default_step_timeout_s", 30.0)),
            maximum_step_timeout_s=float(task_cfg.get("maximum_step_timeout_s", 600.0)),
            maximum_task_deadline_s=float(task_cfg.get("maximum_task_deadline_s", 86_400.0)),
            minimum_priority=float(cfg["policy"]["ordinary_candidate_threshold"]),
            snapshot=self.state.task_state or None,
        )
        self.state.task_state = self.task_manager.snapshot()
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
        self._output_interrupt_callbacks: set[Any] = set()
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
    def _make_safety(cfg: Mapping[str, Any], *, body_available: bool = False) -> SafetySupervisor:
        p = cfg["interaction"]["ordinary_proactive_messages"]
        allowed = {"SPEAK", "NOTIFY", "OBSERVE", "WAIT", "LIGHT"}
        if body_available:
            allowed.add("BODY")
        return SafetySupervisor(allowed_action_types=allowed, proactive_max_count=int(p["max_count"]),
                                proactive_window_s=float(p["rolling_window_s"]),
                                proactive_cooldown_s=float(p["cooldown_s"]))

    def inject(self, event: Event) -> bool:
        receipt = self.router.enqueue(event, self.clock.monotonic())
        if receipt.dropped_event_ids:
            self._dropped_events_pending.extend(receipt.dropped_event_ids)
            with self._generation_lock:
                for dropped_event_id in receipt.dropped_event_ids:
                    self._event_generation_by_id.pop(dropped_event_id, None)
        if not receipt.accepted:
            self._diagnostics.append(f"event_rejected:{event.event_id}:{receipt.disposition}")
            return False
        if event.kind == EventKind.USER_MESSAGE.value:
            message_text = event.payload.get("text")
            if isinstance(message_text, str):
                self.affect_personality.observe_user_message(message_text)
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
                          EventKind.USER_MUTE.value, EventKind.USER_QUIET.value, EventKind.USER_SPEECH_STARTED.value,
                          EventKind.TASK_CANCEL.value, EventKind.TASK_PAUSE.value}:
            self._schedule_fast_cancellation(event)
        return True

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
        elif event.kind in {EventKind.TASK_CANCEL.value, EventKind.TASK_PAUSE.value}:
            task_id = event.payload.get("task_id")
            active_task_id = self.task_manager.active_task_id
            if task_id is None or str(task_id) == active_task_id:
                ids = [p.action_id for p in active if self.task_manager.is_task_intent(p.intent_id)]
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
        degraded_this_step = self._degraded_pending
        self._degraded_pending = False
        if degraded_this_step:
            self._diagnostics.append("degraded_local_fallback")
        cfg_version_before = self.config.version
        self.config.apply_pending()  # atomic hot config swap at the decision boundary
        cfg = self.config.get()
        if self.config.version != cfg_version_before:
            self._apply_reloadable_config(cfg)
            self.state.config_version = self.config.version
            self._diagnostics.append(f"config_applied:{self.config.version}")
        self._update_temperament_parameters(cfg)

        latches = self.router.consume_latches()
        cancelled_action_ids: set[str] = set()
        # Task controls have an ingress-time latch as well as an auditable queued event.
        # Apply them before considering any new candidate, so a queued task step cannot
        # race a user cancellation or pause.
        active_task_id = self.task_manager.active_task_id
        current = self.policy.current
        current_is_task = bool(current and self.task_manager.is_task_intent(current.intent_id))
        cancel_task = latches.cancel_all_tasks or (active_task_id is not None and active_task_id in latches.cancelled_task_ids)
        pause_task = latches.pause_all_tasks or (active_task_id is not None and active_task_id in latches.paused_task_ids)
        if cancel_task:
            self.task_manager.cancel(active_task_id, "fast_latched_task_cancel", now)
            if current_is_task:
                cancelled_action_ids.update(self._cancel_current("task_cancelled"))
            self._diagnostics.append("control_latch:task_cancel")
        elif pause_task:
            self.task_manager.pause_active("fast_latched_task_pause")
            if current_is_task:
                cancelled_action_ids.update(self._cancel_current("task_paused"))
            self._diagnostics.append("control_latch:task_pause")
        if latches.degraded:
            self.state.safety_stopped = True
            self.task_manager.pause_active("control_lane_overflow_degraded")
            cancelled_action_ids.update(self._cancel_current("control_lane_overflow_degraded"))
            self._diagnostics.append("control_lane_overflow:entered_safe_degraded_state")
        if latches.quiet_changed:
            self.state.quiet_scope = latches.quiet_scope
            self._diagnostics.append(f"control_latch:quiet_scope:{latches.quiet_scope}")
        self.state.user_speaking = latches.user_speaking
        if latches.safety_stop:
            self.state.safety_stopped = True
            self.task_manager.pause_active("safety_stop")
            cancelled_action_ids.update(self._cancel_current("safety_stop"))
            self._diagnostics.append("control_latch:safety_stop")
        if latches.user_stop:
            self.task_manager.pause_active("user_stop")
            cancelled_action_ids.update(self._cancel_current("user_stop"))
            self._diagnostics.append("control_latch:user_stop")
        if latches.muted is True:
            self.state.output_muted = True
            if self.policy.current and self.policy.current.action_type == "SPEAK":
                if self.task_manager.is_task_intent(self.policy.current.intent_id):
                    self.task_manager.pause_active("output_muted")
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

        # Update context-dependent targets before integrating the drive state, so the
        # current idle interval is reflected in this tick rather than one tick later.
        try:
            self.autonomy.update_targets(self.drives, now)
        except (ValueError, KeyError, TypeError) as exc:
            self._diagnostics.append(f"autonomy_target_update_rejected:{type(exc).__name__}")

        # Tick drives from a validated dt. Update is deterministic for fixed state/input/dt.
        try:
            self.drives.update(dt, now)
            self.state.drives = self.drives.snapshot()
        except (ValueError, OverflowError) as exc:
            self._diagnostics.append(f"drive_update_rejected:{type(exc).__name__}")
            # Preserve the last valid state; the core loop remains available.

        # Optional LIFE_LAYER tick. Organs update only their own state and may emit
        # low-risk IntentCandidates; activation and action dispatch remain under the
        # existing IntentManager + SafetySupervisor.
        try:
            life_candidates = self.life_layer.tick(
                dt=dt, now_mono=now, now_wall=self.clock.wall_time(), drives=self.drives,
                world=self.world_model.snapshot(now), affect=self.affect_personality.snapshot().get("affect", {}),
                current_intent=self.policy.current, quiet_scope=self.state.quiet_scope,
                user_speaking=self.state.user_speaking, safety_stopped=self.state.safety_stopped,
            )
            candidates.extend(life_candidates)
            reunion_pulse = self.life_layer.consume_reunion_pulse()
            if reunion_pulse is not None:
                self.affect_personality.apply_transient_pulse(
                    valence_delta=float(reunion_pulse.get("valence_delta", 0.0)),
                    arousal_delta=min(0.05, float(reunion_pulse.get("attachment_impulse", 0.0)) * 0.1),
                )
                self._diagnostics.append("attachment_reunion_pulse_applied:transient_only")
            if life_candidates:
                self._diagnostics.append("life_layer_candidates:" + ",".join(c.goal for c in life_candidates))
        except (ValueError, KeyError, TypeError) as exc:
            self._diagnostics.append(f"life_layer_tick_rejected:{type(exc).__name__}")

        # Quiet-rest is a bounded internal mode, not an immortal waiting intent.
        # End it by policy after a recovery condition or max duration, then suppress
        # immediate re-entry to avoid oscillating in and out of rest.
        current_before_autonomy = self.policy.current
        if current_before_autonomy is not None and self.autonomy.rest_should_end(current_before_autonomy, self.drives, now):
            self.policy.finish_current(IntentStatus.SUCCEEDED, "quiet_rest_completed")
            self.autonomy.end_rest(now, self.drives)
            self.state.current_intent = None
            self._diagnostics.append("autonomy_rest_completed")

        # Autonomous candidates are generated locally from normalized state, never by
        # directly executing actions. External user/control events still go through the
        # same policy and safety paths.
        try:
            autonomous_candidates = self.autonomy.generate(
                drives=self.drives,
                world=self.world_model.snapshot(now),
                now_mono=now,
                quiet_scope=self.state.quiet_scope,
                user_speaking=self.state.user_speaking,
                safety_stopped=self.state.safety_stopped,
                current_intent=self.policy.current,
            )
            candidates.extend(autonomous_candidates)
            if autonomous_candidates:
                self._diagnostics.append("autonomy_candidates:" + ",".join(c.intent_type for c in autonomous_candidates))
        except (ValueError, KeyError, TypeError) as exc:
            self._diagnostics.append(f"autonomy_generation_rejected:{type(exc).__name__}")

        try:
            if self.task_manager.expire_if_needed(now):
                expired_task_id = self.task_manager.active_plan.task_id if self.task_manager.active_plan else "unknown"
                self._diagnostics.append(f"task_deadline_handled:{expired_task_id}:{self.task_manager.active_plan.status.value if self.task_manager.active_plan else 'UNKNOWN'}")
                current_intent = self.policy.current
                if current_intent is not None and current_intent.goal.startswith(f"task:{expired_task_id}:"):
                    cancelled_action_ids.update(self._cancel_current("task_deadline_expired"))
            task_candidate = self.task_manager.candidate(now)
            if task_candidate is not None:
                candidates.append(task_candidate)
                self._diagnostics.append(f"task_candidate:{task_candidate.goal}")
        except (ValueError, TypeError, TaskPlanError) as exc:
            self._diagnostics.append(f"task_candidate_rejected:{type(exc).__name__}")

        candidates = self._dedupe_candidates(candidates)
        policy_decision = self.policy.evaluate(candidates, now, emergency=emergency, force_replan=force_replan)
        if policy_decision.previous_intent_id:
            previous_id = policy_decision.previous_intent_id
            if self.task_manager.is_task_intent(previous_id):
                self.task_manager.pause_active("task_step_interrupted_by_higher_priority_intent")
                self.task_manager.unbind_unstarted_intent(previous_id, "higher_priority_intent")
                self._diagnostics.append("task_paused:preempted_by_higher_priority_intent")
            with self._active_map_lock:
                previous_action = self._active_action_by_intent.get(previous_id)
            if previous_action:
                cancelled_action_ids.add(previous_action)
            self._pending_proposals.pop(previous_id, None)
            if self.llm_gateway is not None:
                self.llm_gateway.invalidate_intent(previous_id)
        if self.policy.current is not None:
            self.state.current_intent = self.policy.current
            if self.task_manager.bind_intent(self.policy.current.intent_id, self.policy.current.goal):
                self._diagnostics.append(f"task_intent_bound:{self.policy.current.intent_id}")
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
                self.task_manager.on_action_blocked(maybe_intent.intent_id, safety.code, temporary=True)
                if maybe_intent.ordinary_proactive:
                    # Do not leave a stale proactive intent waiting and re-proposing every
                    # tick. Defer generation until the supervisor's current quota/cooldown
                    # window allows a fresh candidate; dispatch still rechecks safety.
                    next_allowed = self.safety.next_proactive_allowed_at(now)
                    if next_allowed is not None:
                        self.autonomy.defer_social_until(next_allowed)
                    self.policy.finish_current(IntentStatus.CANCELLED, f"proactive_deferred:{safety.code}")
                    self.state.current_intent = None
                    self._diagnostics.append(f"autonomy_proactive_deferred:{safety.code}:{next_allowed}")
                else:
                    maybe_intent.status = IntentStatus.WAITING
                    maybe_intent.reason = f"waiting:{safety.code}"
                    self._diagnostics.append(f"action_waiting:{maybe_intent.intent_id}:{safety.code}")
            else:
                self.task_manager.on_action_blocked(maybe_intent.intent_id, safety.code, temporary=False)
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
        self.state.task_state = self.task_manager.snapshot()
        self.state.life_layer_state = self.life_layer.snapshot(self.clock.wall_time())
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
            replan_request=deepcopy(self.task_manager.replan_request()), degraded=degraded_this_step,
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
                if self.task_manager.is_task_intent(self.policy.current.intent_id):
                    self.task_manager.cancel(self.task_manager.active_task_id, "user_cancelled_task", now)
                cancel_ids |= self._cancel_current("user_cancel")
                if target and self.llm_gateway is not None:
                    self.llm_gateway.invalidate_intent(str(target))
            self._invalidate_correlated_generation(payload)
            force_replan = True
        elif kind == EventKind.USER_MUTE.value:
            self.state.output_muted = True
            if self.policy.current and self.policy.current.action_type == "SPEAK":
                if self.task_manager.is_task_intent(self.policy.current.intent_id):
                    self.task_manager.pause_active("output_muted")
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
            self.autonomy.note_user_interaction(now, self.drives)
            if self.policy.current and self.policy.current.action_type == "SPEAK":
                if self.task_manager.is_task_intent(self.policy.current.intent_id):
                    self.task_manager.pause_active("user_barge_in")
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
            # A host-managed CompanionService owns dialogue persistence and output.
            # Do not persist raw text in EngineState or auto-store its memory request;
            # it still participates in generation invalidation and interrupt handling.
            dialogue_service_managed = payload.get("dialogue_service_managed") is True
            self.state.last_user_message = None if dialogue_service_managed else text.strip()
            if not payload.get("suppress_auto_memory", False) and not dialogue_service_managed:
                self._maybe_remember_explicit_request(text.strip(), event.event_id)
            self.life_layer.will.reset_cancelled(explicit_new_request=True)
            self.life_layer.interrupt("user_message_preemption", cancelled=False, now_mono=now)
            self.autonomy.note_user_interaction(now, self.drives)
            if dialogue_service_managed:
                self._diagnostics.append("dialogue_service_input_observed_without_raw_persistence")
                return candidates, emergency, force_replan, cancel_ids
            if self.policy.current is not None and self.task_manager.is_task_intent(self.policy.current.intent_id):
                self.task_manager.pause_active("interrupted_by_user_message")
                self._diagnostics.append("task_paused:user_message_preemption")
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
                normalized_args = dict(action_args)
                expression_cfg = cfg.get("life_layer", {}).get("expression", {})
                if action_type == "SPEAK" and self.life_layer.enabled and bool(expression_cfg.get("enabled", True)) and isinstance(normalized_args.get("text"), str):
                    char_budget = int(max(80, min(int(expression_cfg.get("max_chars", 400)), int(self._temperament_params["verbosity_chars"]))))
                    segments = prepare_speech_segments(normalized_args["text"], verbosity_chars=char_budget,
                                                       bubble_chars=int(expression_cfg.get("bubble_chars", 100)),
                                                       remove_markdown=True, remove_boilerplate=True)
                    cleaned = " ".join(segments).strip()
                    normalized_args["text"] = cleaned if cleaned else str(normalized_args.get("text", "")).strip()
                validated_candidate = IntentCandidate(
                    intent_type=current.intent_type, goal=current.goal, score=current.score,
                    reason="llm_response_ready", action_type=action_type, action_args=normalized_args,
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
            if self.life_layer.enabled and kind == EventKind.NOVEL_EVENT.value and payload.get("event_class") in {"unexpected_sound", "sudden_appearance"}:
                intensity = payload.get("startle_intensity", 0.6)
                if isinstance(intensity, (int, float)) and not isinstance(intensity, bool) and math.isfinite(float(intensity)):
                    familiar = self.life_layer.startle(str(payload.get("event_class")), max(0.0, min(1.0, float(intensity))),
                                                       now_wall=self.clock.wall_time())
                    self._diagnostics.append("startle_habituated:reduced_expression" if familiar else "startle_first_exposure:full_expression")
                else:
                    self._diagnostics.append("startle_expression_skipped:invalid_intensity")
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
            else:
                if status == ActionStatus.SUCCEEDED.value and payload.get("action_type") == "OBSERVE":
                    self._queue_observation_result(
                        action_id=action_id,
                        intent_id=intent_id,
                        requested_key=payload.get("observation_key"),
                        details=payload.get("details", {}) if isinstance(payload.get("details", {}), Mapping) else {},
                        cfg=cfg,
                    )
                task_result = self.task_manager.on_action_result(
                    action_id=action_id, intent_id=intent_id, status=status, now_mono=now,
                    error_code=None if payload.get("error_code") is None else str(payload.get("error_code")),
                    details=payload.get("details", {}) if isinstance(payload.get("details", {}), Mapping) else {},
                )
                if task_result.accepted:
                    self._diagnostics.append(f"{task_result.code.lower()}:{task_result.task_id}")
            if mapped_intent == intent_id and self.policy.current is not None and self.policy.current.intent_id == intent_id:
                self.autonomy.on_action_terminal(self.policy.current, status, now, self.drives)
                if status == ActionStatus.SUCCEEDED.value:
                    self.policy.finish_current(IntentStatus.SUCCEEDED, "action_succeeded")
                elif status == ActionStatus.CANCELLED.value:
                    self.policy.finish_current(IntentStatus.CANCELLED, "action_cancelled")
                elif status == ActionStatus.FAILED.value:
                    self.policy.finish_current(IntentStatus.FAILED, "action_failed")
                else:
                    # UNKNOWN/TIMED_OUT may have external side effects. Do not blindly retry.
                    # A task step is parked in TaskManager as NEEDS_RECONCILIATION; clear
                    # its one-shot policy intent so an explicit resolve+resume can issue a
                    # fresh proposal. Keeping a WAITING intent here would block that step.
                    if self.policy.current.intent_type == "task_step":
                        self.policy.interrupt_current("task_step_requires_reconciliation")
                        self._diagnostics.append(f"task_step_requires_reconciliation:{action_id}:{status}")
                    else:
                        self.policy.current.status = IntentStatus.WAITING
                        self.policy.current.action_type = None
                        self.policy.current.reason = f"action_result_{status.lower()}_requires_replan"
                        self._diagnostics.append(f"action_result_requires_replan:{action_id}:{status}")
                self.state.current_intent = self.policy.current
            with self._active_map_lock:
                self._action_to_intent.pop(action_id, None)
                self._active_action_by_intent.pop(intent_id, None)
        elif kind == EventKind.TASK_PLAN_SUBMITTED.value:
            if not bool(cfg.get("tasks", {}).get("enabled", True)):
                self._diagnostics.append("task_plan_rejected:tasks_disabled")
            else:
                try:
                    result = self.task_manager.submit(payload, now)
                except TaskPlanError as exc:
                    self._diagnostics.append(f"task_plan_rejected:{str(exc)[:180]}")
                    return candidates, emergency, force_replan, cancel_ids
                self._diagnostics.append(f"task_plan:{result.code}:{result.task_id}")
                if result.accepted:
                    force_replan = True
        elif kind == EventKind.TASK_CANCEL.value:
            task_id = payload.get("task_id")
            current = self.policy.current
            current_is_task = bool(current and self.task_manager.is_task_intent(current.intent_id))
            result = self.task_manager.cancel(None if task_id is None else str(task_id),
                                              str(payload.get("reason", "user_cancelled_task")), now)
            self._diagnostics.append(f"task_cancel:{result.code}:{result.task_id}")
            if result.accepted:
                if current_is_task:
                    cancel_ids |= self._cancel_current("task_cancelled")
                force_replan = True
        elif kind == EventKind.TASK_PAUSE.value:
            task_id = payload.get("task_id")
            current = self.policy.current
            current_is_task = bool(current and self.task_manager.is_task_intent(current.intent_id))
            active_task_id = self.task_manager.active_task_id
            if task_id is None or str(task_id) == active_task_id:
                result = self.task_manager.pause_active(str(payload.get("reason", "user_pause_task")))
                self._diagnostics.append(f"task_pause:{result.code}:{result.task_id}")
                if result.accepted and current_is_task:
                    cancel_ids |= self._cancel_current("task_paused")
                force_replan = True
        elif kind == EventKind.TASK_RESUME.value:
            task_id = payload.get("task_id")
            result = self.task_manager.resume(None if task_id is None else str(task_id))
            self._diagnostics.append(f"task_resume:{result.code}:{result.task_id}")
            force_replan = result.accepted
        elif kind == EventKind.TASK_RESOLVE_STEP.value:
            task_id = str(payload.get("task_id", ""))
            step_id = str(payload.get("step_id", ""))
            outcome = str(payload.get("outcome", ""))
            result = self.task_manager.resolve_step(task_id, step_id, outcome, now,
                                                    confirmed_not_completed=payload.get("confirmed_not_completed") is True)
            self._diagnostics.append(f"task_resolve:{result.code}:{result.task_id}")
        elif kind in {EventKind.TIMER.value, EventKind.CONFIG_RELOAD.value}:
            pass
        return candidates, emergency, force_replan, cancel_ids

    def _queue_observation_result(
        self, *, action_id: str, intent_id: str, requested_key: Any,
        details: Mapping[str, Any], cfg: Mapping[str, Any],
    ) -> None:
        """Convert one validated OBSERVE result into a normal world observation event.

        The executor is not allowed to update the world model directly. It can only
        return one value for the exact allowlisted key requested by the action.
        """
        observation = details.get("observation")
        if not isinstance(requested_key, str) or not requested_key:
            self._diagnostics.append(f"observe_result_rejected:{action_id}:missing_requested_key")
            return
        allowlist = set(cfg["llm"].get("world_fact_allowlist", []))
        if requested_key not in allowlist:
            self._diagnostics.append(f"observe_result_rejected:{action_id}:key_not_allowlisted")
            return
        if not isinstance(observation, Mapping):
            self._diagnostics.append(f"observe_result_rejected:{action_id}:missing_observation")
            return
        allowed_fields = {"value", "confidence", "ttl_s"}
        if set(observation) - allowed_fields or "value" not in observation:
            self._diagnostics.append(f"observe_result_rejected:{action_id}:invalid_observation_schema")
            return
        confidence = observation.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(float(confidence)) or not 0.0 <= float(confidence) <= 1.0:
            self._diagnostics.append(f"observe_result_rejected:{action_id}:invalid_confidence")
            return
        ttl_s = observation.get("ttl_s")
        if ttl_s is not None and (isinstance(ttl_s, bool) or not isinstance(ttl_s, (int, float)) or not math.isfinite(float(ttl_s)) or not 0.1 <= float(ttl_s) <= 3600.0):
            self._diagnostics.append(f"observe_result_rejected:{action_id}:invalid_ttl")
            return
        event_payload: dict[str, Any] = {
            "key": requested_key,
            "value": thaw(observation["value"]),
            "observation_type": requested_key,
        }
        if ttl_s is not None:
            event_payload["ttl_s"] = float(ttl_s)
        try:
            event = self.new_event(
                EventKind.WORLD_OBSERVATION.value, event_payload,
                source=f"observe_action:{action_id}", priority=int(Priority.NORMAL),
                expires_in_s=None if ttl_s is None else float(ttl_s),
                confidence=float(confidence), correlation_id=intent_id,
            )
            self.inject(event)
            self._diagnostics.append(f"observe_result_queued:{requested_key}:{action_id}")
        except (BufferError, ValueError, TypeError) as exc:
            self._diagnostics.append(f"observe_result_dropped:{action_id}:{type(exc).__name__}")

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
        explicit_cancel = reason in {"user_cancel", "user_cancel_all", "safety_stop", "user_stop", "output_muted", "control_lane_overflow_degraded"}
        self.life_layer.interrupt(reason, cancelled=explicit_cancel, now_mono=self.clock.monotonic())
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

    def _update_temperament_parameters(self, cfg: Mapping[str, Any]) -> None:
        """Apply small derived parameters while keeping configured safety limits as hard caps."""
        traits = self.affect_personality.snapshot().get("traits", {})
        if self.life_layer.enabled and self.life_layer.temperament.enabled:
            params = self.life_layer.temperament.action_params(
                verbosity_base=float(traits.get("verbosity", .42)),
                initiative_base=float(traits.get("initiative", .35)),
                warmth_base=float(traits.get("warmth", .68)),
                curiosity_base=float(traits.get("curiosity", .66)),
            )
        else:
            params = {"proactive_scale": 1.0, "verbosity_chars": int(round(80 + 320 * float(traits.get("verbosity", .42)))),
                      "warmth": float(traits.get("warmth", .68)), "exploration_threshold_scale": 1.0,
                      "arousal_response_scale": 1.0, "startle_expression_scale": 1.0}
        self._temperament_params = params
        base_autonomy = cfg.get("autonomy", {})
        exploration = base_autonomy.get("exploration", {})
        base_threshold = float(exploration.get("threshold", .55))
        scaled_threshold = max(0.0, min(1.0, base_threshold * float(params["exploration_threshold_scale"])))
        self.autonomy.config = replace(self.autonomy.config, exploration_threshold=scaled_threshold)
        proactive = cfg.get("interaction", {}).get("ordinary_proactive_messages", {})
        hard_cap = int(proactive.get("max_count", 2))
        if self.life_layer.enabled and self.life_layer.temperament.enabled:
            effective_quota = min(hard_cap, max(0, int(math.floor(hard_cap * float(params["proactive_scale"]) + 1e-9))))
        else:
            effective_quota = hard_cap
        self.safety.reconfigure(
            proactive_max_count=effective_quota,
            proactive_window_s=float(proactive.get("rolling_window_s", 3600.0)),
            proactive_cooldown_s=float(proactive.get("cooldown_s", 1200.0)),
        )

    def _apply_reloadable_config(self, cfg: Mapping[str, Any]) -> None:
        self.policy.config = self._policy_config(cfg)
        self.task_manager.minimum_priority = float(cfg["policy"]["ordinary_candidate_threshold"])
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
            dispatch_now = self.clock.monotonic()
            if proposal.deadline_mono is not None and dispatch_now >= proposal.deadline_mono:
                if current.intent_type == "task_step":
                    self.task_manager.unbind_unstarted_intent(current.intent_id, "proposal_deadline_expired")
                    self._pending_proposals.pop(current.intent_id, None)
                    self.policy.finish_current(IntentStatus.EXPIRED, "task_step_proposal_expired")
                    self.state.current_intent = None
                    self.state.task_state = self.task_manager.snapshot()
                    self.state.state_version += 1
                return SafetyDecision("DENY_HARD", "PROPOSAL_EXPIRED", "action proposal deadline has expired"), None
            pending = self._pending_proposals.get(proposal.intent_id)
            if pending is not None and pending.action_id != proposal.action_id:
                return SafetyDecision("DENY_HARD", "STALE_PROPOSAL", "proposal is not the current pending action"), None
            fast_latches = self.router.peek_latches()
            fast_denial = self._fast_latch_denial(proposal, fast_latches)
            if fast_denial is not None:
                return fast_denial, None
            if self.task_manager.is_task_intent(proposal.intent_id):
                task_id = self.task_manager.active_task_id
                if fast_latches.cancel_all_tasks or (task_id is not None and task_id in fast_latches.cancelled_task_ids):
                    return SafetyDecision("DENY_HARD", "TASK_CANCEL_LATCHED", "task cancellation is pending"), None
                if fast_latches.pause_all_tasks or (task_id is not None and task_id in fast_latches.paused_task_ids):
                    return SafetyDecision("DENY_HARD", "TASK_PAUSE_LATCHED", "task pause is pending"), None
            safety = self.safety.check(proposal, self.state, self.clock.monotonic())
            if safety.outcome != "ALLOW":
                self.task_manager.on_action_blocked(current.intent_id, safety.code, temporary=safety.outcome == "DENY_TEMPORARY")
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
            if current.intent_type == "task_step" and not self.task_manager.bind_action(current.intent_id, proposal.action_id, dispatch_now):
                self.task_manager.pause_active("task_action_binding_failed")
                self.policy.finish_current(IntentStatus.CANCELLED, "task_action_binding_failed")
                self.state.current_intent = None
                self.state.task_state = self.task_manager.snapshot()
                self.state.state_version += 1
                return SafetyDecision("DENY_HARD", "TASK_BINDING_FAILURE", "task step/action binding failed"), None
            self.state.task_state = self.task_manager.snapshot()
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
            if current.intent_type == "task_step":
                self.task_manager.unbind_action_before_start(current.intent_id, proposal.action_id, late_denial.code)
                self.state.task_state = self.task_manager.snapshot()
            if late_denial.outcome == "DENY_TEMPORARY" and self.policy.current is not None and self.policy.current.intent_id == proposal.intent_id:
                self.policy.current.status = IntentStatus.WAITING
                self.policy.current.reason = f"waiting:{late_denial.code}"
            return late_denial, None
        self.safety.record_started(proposal, self.clock.monotonic())
        try:
            runner = self.body_controller.execute if proposal.action_type == "BODY" and self.body_enabled and self.body_controller is not None else self.action_executor.execute
            if proposal.deadline_mono is None:
                result = await runner(proposal)
            else:
                remaining = proposal.deadline_mono - self.clock.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                result = await asyncio.wait_for(runner(proposal), timeout=remaining)
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
        except asyncio.TimeoutError:
            try:
                await self.action_executor.cancel(proposal.action_id)
            except Exception:
                pass
            result = ActionResult(proposal.action_id, proposal.intent_id, ActionStatus.TIMED_OUT,
                                  completed_at_mono=self.clock.monotonic(), error_code="ACTION_DEADLINE_EXCEEDED")
        except Exception as exc:
            result = ActionResult(proposal.action_id, proposal.intent_id, ActionStatus.UNKNOWN,
                                  completed_at_mono=self.clock.monotonic(), error_code=f"EXECUTOR_{type(exc).__name__}")
        with self._active_map_lock:
            self._active_proposals.pop(proposal.action_id, None)
        event = self.new_event(EventKind.ACTION_TERMINAL.value, {
            "action_id": result.action_id, "intent_id": result.intent_id, "status": result.status.value,
            "action_type": proposal.action_type,
            "observation_key": proposal.args.get("key") if proposal.action_type == "OBSERVE" else None,
            "error_code": result.error_code, "details": dict(result.details),
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
        """Request cancellation from the owning adapter; acceptance is not physical-stop confirmation."""
        try:
            proposal = self._active_proposals.get(action_id)
            if proposal is not None and proposal.action_type == "BODY" and self.body_controller is not None:
                return await self.body_controller.cancel(action_id)
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

    async def understand_demand(
        self, *, user_input: str, interaction_id: str, generation: int,
        request_context: Mapping[str, Any] | None = None,
        voice_signals: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Return structured demand understanding; this method does not route or execute.

        The engine supplies its reviewed personality and interaction-state projections.
        The caller must pass the current interaction generation, just as for dialogue.
        """
        if self.llm_gateway is None:
            raise RuntimeError("LLM_NOT_CONFIGURED")
        with self._generation_lock:
            current_generation = self._interaction_generation.get(interaction_id)
            if current_generation is None:
                self._interaction_generation[interaction_id] = generation
                current_generation = generation
            if generation != current_generation:
                raise RuntimeError("ENGINE_GENERATION_STALE")
        from ..understanding.demand import LLMDemandInterpreter
        self.llm_gateway.set_generation(interaction_id, generation)
        interpreter = LLMDemandInterpreter(self.llm_gateway)
        demand = await interpreter.understand(
            user_input,
            request_context=request_context,
            personality_snapshot=self.affect_personality.snapshot(),
            affect_guidance=self.affect_personality.expression_guidance(),
            interaction_state_snapshot=self.interaction_state.snapshot(),
            voice_signals=voice_signals,
            interaction_id=interaction_id,
            generation=generation,
        )
        assessment = demand.get("emotion_assessment") if isinstance(demand, Mapping) else None
        if self.life_layer.enabled and isinstance(assessment, Mapping):
            try:
                if self.affect_personality.observe_emotion_assessment(assessment):
                    self._diagnostics.append("emotion_assessment_applied:transient_expression_only")
            except (ValueError, TypeError):
                self._diagnostics.append("emotion_assessment_ignored:invalid")
        return demand

    async def process_user_request(
        self, *, user_input: str, interaction_id: str, generation: int,
        router: Any, request_context: Any = None,
        voice_signals: Mapping[str, Any] | None = None,
        request_id: str | None = None,
    ) -> tuple[Mapping[str, Any], Any]:
        """Canonical request path: understand once, then let host policy route.

        The LLM returns semantic understanding only. The router applies engine-owned
        capability availability, permission, safety and confirmation policy; no legacy
        fine-grained LLM intent schema or resolver path is involved.
        """
        demand = await self.understand_demand(
            user_input=user_input,
            interaction_id=interaction_id,
            generation=generation,
            request_context=(request_context.to_dict() if callable(getattr(request_context, "to_dict", None)) else request_context),
            voice_signals=voice_signals,
        )
        decision = router.route_understanding(
            user_input, demand, request_context, request_id=request_id,
        )
        return demand, decision

    async def request_llm(
        self, *, purpose: str, interaction_id: str, generation: int, user_input: str | None = None,
        intent_id: str | None = None, **view_kwargs: Any,
    ) -> LLMResult:
        """Build a filtered state view, then use the gateway outside `step`.

        This method never commits a response or memory. For dialogue, pass a completed
        response through submit_llm_response(); for plans/memories, use their dedicated
        validated workflows instead of mutating EngineState directly.
        """
        with self._generation_lock:
            current_generation = self._interaction_generation.get(interaction_id)
            if current_generation is None:
                self._interaction_generation[interaction_id] = generation
                current_generation = generation
            if generation != current_generation:
                from ..llm.llm_gateway import LLMStatus
                return LLMResult("", LLMStatus.STALE, error_code="ENGINE_GENERATION_STALE")
        if self.llm_gateway is None:
            from ..llm.llm_gateway import LLMStatus
            if self._fallback_allowed(purpose, user_input, interaction_id=interaction_id,
                                      generation=generation, intent_id=intent_id):
                fallback_text = self._local_dialogue_fallback(user_input or "")
                self._fallback_responses.add((interaction_id, generation))
                if len(self._fallback_responses) > 1000:
                    self._fallback_responses = set(sorted(self._fallback_responses, key=lambda x: (x[0], x[1]))[-500:])
                return LLMResult("", LLMStatus.COMPLETED,
                                 payload={"response_text": fallback_text, "degraded": True},
                                 error_code="DEGRADED_LOCAL_FALLBACK")
            return LLMResult("", status=LLMStatus.FAILED, error_code="LLM_NOT_CONFIGURED")
        if purpose == "multi_step_plan":
            # Generic provider tools are distinct from the internal task action types.
            requested_tools = set(view_kwargs.get("allowed_tools", []))
            configured_tools = set(self.config.get()["llm"].get("allowed_tools", []))
            view_kwargs["allowed_tools"] = sorted(requested_tools & configured_tools)
        elif purpose == "task_plan":
            # Plans can name only reviewed internal action types; they cannot expand the
            # provider's arbitrary tool permissions.
            requested_actions = set(view_kwargs.get("allowed_action_types", []))
            cfg = self.config.get()
            configured_actions = set(cfg.get("tasks", {}).get("llm_plannable_action_types", []))
            task_actions = set(self.task_manager.allowed_action_types)
            allowed_world_keys = sorted(set(self.task_manager.allowed_world_keys) & set(self.state_view_builder.allowed_world_keys))
            allowed_actions = requested_actions & configured_actions & task_actions
            if not allowed_world_keys:
                # OBSERVE requires a reviewed semantic world-fact key. An empty
                # allowlist disables observation plans; it never means unrestricted.
                allowed_actions.discard("OBSERVE")
            view_kwargs["allowed_action_types"] = sorted(allowed_actions)
            view_kwargs["allowed_world_fact_keys"] = allowed_world_keys
            view_kwargs.setdefault("validation_feedback", "")
            view_kwargs["max_steps"] = min(int(view_kwargs.get("max_steps", cfg["tasks"]["max_steps"])), int(cfg["tasks"]["max_steps"]))
        if self.life_layer.enabled and purpose in {"simple_response", "complex_dialogue"} and "relevant_memories" not in view_kwargs and self.memory_repository is not None and isinstance(user_input, str):
            try:
                retrieved = self.memory_repository.search_memories(user_input, limit=5)
                view_kwargs["relevant_memories"] = [
                    {"memory_id": str(row.get("memory_id", "")),
                     "summary": str(row.get("payload", {}).get("summary", row.get("payload", {}).get("fact", ""))),
                     "source_refs": [str(x) for x in row.get("source_ids", [])[:10]],
                     "kind": str(row.get("kind", "unknown"))}
                    for row in retrieved if isinstance(row.get("payload"), Mapping)
                    and isinstance(row.get("payload", {}).get("summary", row.get("payload", {}).get("fact")), str)
                    and row.get("payload", {}).get("summary", row.get("payload", {}).get("fact", "")).strip()
                ]
            except (ValueError, TypeError, AttributeError) as exc:
                self._diagnostics.append(f"memory_retrieval_failed:{type(exc).__name__}")
        # Personality and affect are a small expression-only projection. They cannot
        # alter policy, safety checks, permissions, or the action allowlist.
        view_kwargs.setdefault("personality_snapshot", self.affect_personality.snapshot())
        guidance = self.affect_personality.expression_guidance()
        if self.life_layer.enabled and self.life_layer.temperament.enabled:
            params = self._temperament_params
            guidance += (f"当前受限气质参数：温暖度={float(params['warmth']):.2f}，主动性系数={float(params['proactive_scale']):.2f}。"
                         "这些参数仅微调表达和普通主动性，不代表用户情绪，也不能改变事实、安全或权限。")
        view_kwargs.setdefault("affect_guidance", guidance)
        view_kwargs.setdefault("interaction_state_snapshot", self.interaction_state.snapshot())
        view = self.state_view_builder.build(
            purpose, self.state, now_mono=self.clock.monotonic(), user_input=user_input, **view_kwargs,
        )
        self.llm_gateway.set_generation(interaction_id, generation)
        result = await self.llm_gateway.generate(interaction_id=interaction_id, intent_id=intent_id,
                                                 generation=generation, purpose=purpose, view=view)
        from ..llm.llm_gateway import LLMStatus
        if result.status == LLMStatus.COMPLETED:
            with self._generation_lock:
                newest_generation = self._interaction_generation.get(interaction_id)
            if newest_generation != generation:
                return LLMResult(result.request_id, LLMStatus.STALE, error_code="ENGINE_GENERATION_STALE", latency_s=result.latency_s)
            if intent_id is not None and (self.policy.current is None or self.policy.current.intent_id != intent_id):
                return LLMResult(result.request_id, LLMStatus.STALE, error_code="INTENT_NO_LONGER_ACTIVE", latency_s=result.latency_s)
        elif purpose in {"simple_response", "complex_dialogue"} and result.status in {LLMStatus.FAILED, LLMStatus.TIMED_OUT} and isinstance(user_input, str) and self._fallback_allowed(purpose, user_input, interaction_id=interaction_id,
                                                                                   generation=generation, intent_id=intent_id):
            # Only provider failure/timeout gets a disclosed local fallback. Stale/cancelled
            # generations and all quiet/muted/stopped states remain silent.
            fallback_text = self._local_dialogue_fallback(user_input)
            self._fallback_responses.add((interaction_id, generation))
            if len(self._fallback_responses) > 1000:
                self._fallback_responses = set(sorted(self._fallback_responses, key=lambda x: (x[0], x[1]))[-500:])
            return LLMResult(result.request_id, LLMStatus.COMPLETED,
                             payload={"response_text": fallback_text, "degraded": True},
                             error_code="DEGRADED_LOCAL_FALLBACK", latency_s=result.latency_s)
        return result

    def _maybe_remember_explicit_request(self, text: str, source_id: str) -> None:
        """Store only explicit, bounded 'remember this' requests when a memory store is attached."""
        if self.memory_repository is None:
            return
        import re
        match = re.match(r"^(?:请)?(?:帮我)?(?:记住|记一下|记下来)[：:，,、 ]*(.{2,1800})$", text)
        if not match:
            return
        summary = match.group(1).strip()
        # Never persist likely credentials or authentication secrets, even on explicit asks.
        if any(term in summary.casefold() for term in (
            "密码", "password", "passcode", "验证码", "verification code", "one-time code",
            "api key", "api_key", "access token", "bearer token", "private key", "secret key", "私钥",
            "信用卡号", "credit card", "card number", "cvv", "ssn", "social security number", "银行账号", "bank account",
        )):
            self._diagnostics.append("memory_candidate_rejected:sensitive_credential")
            return
        try:
            memory_id = self.remember(summary, kind="user_explicit_note", source_ids=[source_id])
            self._diagnostics.append(f"memory_explicit_note_saved:{memory_id}")
        except (ValueError, TypeError, RuntimeError) as exc:
            self._diagnostics.append(f"memory_explicit_note_failed:{type(exc).__name__}")

    def _fallback_allowed(self, purpose: str, user_input: str | None, *, interaction_id: str,
                          generation: int, intent_id: str | None = None) -> bool:
        if purpose not in {"simple_response", "complex_dialogue"} or not isinstance(user_input, str):
            return False
        fallback_cfg = self.config.get().get("life_layer", {}).get("fallback", {})
        if not self.life_layer.enabled or not bool(fallback_cfg.get("enabled", True)):
            return False
        # A degraded response must never bypass the independent control lane or revive
        # a request that was cancelled/replaced while a provider call was in flight.
        with self._generation_lock:
            if self._interaction_generation.get(interaction_id) != generation:
                return False
        if intent_id is not None and (self.policy.current is None or self.policy.current.intent_id != intent_id):
            return False
        if (self.state.quiet_scope is not None or self.state.output_muted
                or self.state.safety_stopped or self.state.user_speaking):
            return False
        return bool(user_input.strip())

    @staticmethod
    def _local_dialogue_fallback(user_input: str) -> str:
        text = user_input.strip()[:2000]
        lowered = text.casefold()
        if any(greet in text for greet in ("你好", "嗨", "哈喽", "早上好", "晚上好", "hello", "hi")):
            return "我在。现在想聊些什么？（智能回复暂时不可用。）"
        if any(word in text for word in ("难过", "伤心", "委屈", "压力大", "烦", "焦虑", "累")):
            return "听起来这件事让你不太好受。我可以先听你说，不急着给建议。（智能回复暂时不可用。）"
        if text.endswith(("？", "?")):
            return "我听到了你的问题，但现在暂时无法生成可靠的完整回答。你可以先告诉我最关键的部分。（智能回复暂时不可用。）"
        return "我在听。你可以接着说；目前智能回复暂时不可用，我不想假装已经理解了更多。"

    def attach_memory_repository(self, repository: Any | None) -> None:
        """Attach an existing SQLiteRepository-like memory store; no DB is created implicitly."""
        if repository is not None and not callable(getattr(repository, "search_memories", None)):
            raise TypeError("repository must expose search_memories")
        self.memory_repository = repository

    def remember(self, summary: str, *, memory_id: str | None = None, kind: str = "user_preference",
                 source_ids: list[str] | None = None, payload: Mapping[str, Any] | None = None) -> str:
        """Persist a user-approved concise memory; raw conversation is not accepted as a special field."""
        if self.memory_repository is None:
            raise RuntimeError("memory_repository_not_attached")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 2000:
            raise ValueError("summary must contain 1..2000 characters")
        from ..core.models import validate_json_compatible
        content = dict(payload or {})
        content["summary"] = summary.strip()
        if "raw_dialogue" in content or "conversation_text" in content:
            raise ValueError("raw dialogue is not accepted as memory payload")
        validate_json_compatible(content, "memory.payload")
        memory_id = memory_id or str(uuid.uuid4())
        sources = list(source_ids or [])
        self.memory_repository.put_memory(memory_id, kind, content, sources, self.clock.wall_time())
        return memory_id

    def recall_memories(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        if self.memory_repository is None:
            return []
        return self.memory_repository.search_memories(query, limit=limit)

    def forget_memory(self, memory_id: str) -> dict[str, Any]:
        if self.memory_repository is None:
            raise RuntimeError("memory_repository_not_attached")
        return self.memory_repository.delete_memory(memory_id, self.clock.wall_time())

    def propose_body_event(self, event: str, *, intensity: float = 0.5, sleeping: bool = False) -> bool:
        """Submit a reviewed semantic body command through normal intent and safety policy."""
        if not self.body_enabled or self.body_controller is None:
            self._diagnostics.append("body_event_rejected:driver_not_enabled")
            return False
        command = self.body_controller.mapper.map(event=event, arousal=intensity, sleeping=sleeping)
        if command is None:
            return False
        # Do not accept arbitrary BODY action args from dialogue/model outputs.
        candidate = IntentCandidate(intent_type="body_semantic", goal=f"body:{event}", score=0.72,
                                    reason=f"body_semantic_{event}", action_type="BODY", action_args=command,
                                    interruption_policy="REPLAN")
        self.inject(self.new_event("INTENT_CANDIDATE", {"intent_type": candidate.intent_type, "goal": candidate.goal,
                      "score": candidate.score, "reason": candidate.reason, "action_type": candidate.action_type,
                      "action_args": dict(command), "urgency": 0.0}, source="body_host"))
        return True

    def life_layer_snapshot(self) -> dict[str, Any]:
        """Return a bounded deterministic snapshot of LIFE_LAYER organs."""
        return self.life_layer.snapshot(self.clock.wall_time())

    def record_life_event(self, *, kind: str, summary_key: str, source_id: str, metadata: Mapping[str, Any] | None = None) -> bool:
        """Record a structured event, never raw chat text, in the bounded life journal."""
        if not self.life_layer.enabled:
            return False
        from datetime import datetime, timezone
        now_wall = self.clock.wall_time()
        day = datetime.fromtimestamp(now_wall, timezone.utc).date().isoformat()
        result = self.life_layer.life_stream.record(day=day, kind=kind, summary_key=summary_key,
                                                   source_id=source_id, timestamp_wall=now_wall, metadata=metadata)
        self.state.life_layer_state = self.life_layer.snapshot(now_wall)
        self.state.state_version += 1
        return result

    def set_life_sleeping(self, sleeping: bool) -> None:
        """Explicit host observation/control only; the core never infers sleep from silence."""
        if not self.life_layer.enabled:
            return
        self.life_layer.set_sleeping(sleeping, now_wall=self.clock.wall_time())
        if sleeping:
            self.state.life_layer_state = self.life_layer.snapshot(self.clock.wall_time())
        self.state.state_version += 1

    def drift_temperament(self, axis: str, direction: float, *, step: float | None = None) -> float:
        """Apply one bounded, host-validated temperament event; never alters safety policy."""
        if not self.life_layer.enabled:
            if axis not in self.life_layer.temperament.traits:
                raise KeyError(f"unknown temperament axis: {axis}")
            return self.life_layer.temperament.traits[axis]
        value = self.life_layer.temperament.drift(axis, direction, now_wall=self.clock.wall_time(), step=step)
        self.state.life_layer_state = self.life_layer.snapshot(self.clock.wall_time())
        self.state.state_version += 1
        return value

    def record_attachment_evidence(self, kind: str, *, delta: float = 0.5) -> float:
        """Apply a bounded host-verified shared-experience signal to attachment trust."""
        if not self.life_layer.enabled:
            return self.life_layer.attachment.trust
        self.life_layer.attachment.record_shared_experience(kind, delta=delta, now_wall=self.clock.wall_time())
        self.state.life_layer_state = self.life_layer.snapshot(self.clock.wall_time())
        self.state.state_version += 1
        return self.life_layer.attachment.trust

    def record_personality_feedback(self, feedback: str) -> dict[str, Any]:
        """Apply explicit user style feedback; changes are bounded and persisted locally."""
        result = self.affect_personality.record_preference_feedback(feedback)
        self.state.state_version += 1
        return result

    def personality_snapshot(self) -> dict[str, Any]:
        """Return the current stable personality and transient affect projection."""
        return self.affect_personality.snapshot()

    def interaction_state_snapshot(self) -> dict[str, Any]:
        """Return bounded interaction signals and the approved current strategy."""
        return self.interaction_state.snapshot()

    def observe_interaction_signal(self, name: str, value: Any, **kwargs: Any) -> dict[str, Any]:
        """Record a non-verbatim signal; this never activates a strategy automatically."""
        result = self.interaction_state.observe_signal(name, value, **kwargs)
        self.state.state_version += 1
        return result

    def set_interaction_strategy(self, name: str, instruction: str, **kwargs: Any) -> dict[str, Any]:
        """Set a host-approved current strategy; no goal/priority machinery is involved."""
        result = self.interaction_state.set_strategy(name, instruction, **kwargs)
        self.state.state_version += 1
        return result

    def clear_interaction_strategy(self, *, reason_code: str = "no_longer_applicable") -> bool:
        """Clear the current strategy when the host determines it no longer applies."""
        result = self.interaction_state.clear_strategy(reason_code=reason_code)
        if result:
            self.state.state_version += 1
        return result

    def current_generation(self, interaction_id: str) -> int:
        """Return the current ingress generation for an interaction (0 before first input)."""
        with self._generation_lock:
            return self._interaction_generation.get(interaction_id, 0)

    def invalidate_interactions(self, *, reason_code: str, interaction_id: str | None = None) -> dict[str, int]:
        """Invalidate in-flight dialogue generations without consulting an LLM.

        Host-issued stop/mute/quiet controls call this on the independent control lane.
        A queued/late result cannot become valid again merely because a control latch was
        consumed by the main loop. This invalidates cognition only; physical emergency
        stops must still be implemented independently of this Python process.
        """
        if not isinstance(reason_code, str) or not reason_code or len(reason_code) > 100:
            raise ValueError("reason_code must contain 1..100 characters")
        with self._generation_lock:
            targets = ([interaction_id] if interaction_id is not None else list(self._interaction_generation))
            if interaction_id is not None and interaction_id not in self._interaction_generation:
                targets = [interaction_id]
            updated: dict[str, int] = {}
            for iid in targets:
                next_generation = self._interaction_generation.get(iid, 0) + 1
                self._interaction_generation[iid] = next_generation
                self.state.generation_by_interaction[iid] = next_generation
                updated[iid] = next_generation
                if self.llm_gateway is not None:
                    self.llm_gateway.set_generation(iid, next_generation)
            self.state.state_version += 1
        self._diagnostics.append(f"interaction_generations_invalidated:{reason_code}:{len(updated)}")
        self.interrupt_outputs(reason_code=reason_code, interaction_id=interaction_id)
        return updated

    def register_output_interrupt_callback(self, callback: Any):
        """Register a synchronous host callback for independent stop/mute/barge-in.

        The callback should schedule cancellation promptly and return; it must not block
        the deterministic engine or perform device I/O synchronously.
        """
        if not callable(callback):
            raise TypeError("callback must be callable")
        self._output_interrupt_callbacks.add(callback)
        def unregister() -> None:
            self._output_interrupt_callbacks.discard(callback)
        return unregister

    def interrupt_outputs(self, *, reason_code: str, interaction_id: str | None = None) -> None:
        """Signal host-managed audio/UI output to stop without running a model."""
        callbacks = tuple(self._output_interrupt_callbacks)
        for callback in callbacks:
            try:
                callback(reason_code, interaction_id)
            except Exception as exc:
                self._diagnostics.append(f"output_interrupt_callback_failed:{type(exc).__name__}")

    def submit_llm_response(
        self, *, intent_id: str, interaction_id: str, generation: int, response_text: str, degraded: bool = False,
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
        fallback_key = (interaction_id, generation)
        if fallback_key in self._fallback_responses:
            self._fallback_responses.discard(fallback_key)
            degraded = True
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
        if degraded:
            self._degraded_pending = True
        return True

    def snapshot(self) -> EngineState:
        return deepcopy(self.state)

    def save(self, repository: Any) -> None:
        repository.save_state(self.state, self.clock.wall_time())

    def _release_replay_network_guard(self) -> None:
        if self._replay_network_guard is not None:
            self._replay_network_guard.__exit__(None, None, None)
            self._replay_network_guard = None

    def close(self) -> None:
        """Best-effort synchronous shutdown; async hosts should prefer `await aclose()`."""
        self._release_replay_network_guard()
        close = getattr(self.action_executor, "close", None)
        if callable(close):
            close()

    async def aclose(self) -> None:
        """Await adapter shutdown so subprocesses and action drivers finish cleanup."""
        self._release_replay_network_guard()
        aclose = getattr(self.action_executor, "aclose", None)
        if callable(aclose):
            await aclose()
            return
        close = getattr(self.action_executor, "close", None)
        if callable(close):
            result = close()
            if asyncio.iscoroutine(result):
                await result

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
        self.life_layer.restore(restored.life_layer_state or {})
        self.task_manager.load_snapshot(restored.task_state or {}, recovering=True)
        self.state.task_state = self.task_manager.snapshot()
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
