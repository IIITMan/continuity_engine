"""LLM-backed task-plan proposal orchestration.

This module never executes actions. It requests a purpose-filtered plan from the
LLM gateway, validates it against the live TaskManager's policy in an isolated
preflight copy, then enqueues a normal TASK_PLAN_SUBMITTED event. Acceptance is
reported only after the core processes that event.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
import threading
import uuid
from typing import Any, Mapping, Sequence, TYPE_CHECKING

from .llm_gateway import LLMResult, LLMStatus
from ..core.models import EventKind, Priority, thaw
from ..tasks.task_manager import TaskPlanError, TaskStatus

if TYPE_CHECKING:  # pragma: no cover
    from ..core.engine import ContinuityEngine


class PlanningStatus(str, Enum):
    QUEUED = "QUEUED"
    ALREADY_QUEUED = "ALREADY_QUEUED"
    IN_PROGRESS = "IN_PROGRESS"
    REJECTED = "REJECTED"
    LLM_FAILED = "LLM_FAILED"
    CANCELLED = "CANCELLED"
    STALE = "STALE"
    QUEUE_REJECTED = "QUEUE_REJECTED"


@dataclass(frozen=True, slots=True)
class TaskPlanningResult:
    request_id: str
    status: PlanningStatus
    task_id: str | None = None
    event_id: str | None = None
    llm_status: str | None = None
    error_code: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def queued(self) -> bool:
        return self.status in {PlanningStatus.QUEUED, PlanningStatus.ALREADY_QUEUED}


class LLMTaskPlanner:
    """Generate safe linear plans from a controlled LLM response.

    The model can select only configured internal action types. It cannot choose
    arbitrary Python functions, shell commands, provider tools, retry counts, or
    execution deadlines. TaskManager remains the final authority for plan validity.
    """

    _BASE_CONSTRAINTS = (
        "Return a sequential linear plan only; no branches, loops, or parallel steps.",
        "Use only the supplied allowlisted action types and each action's valid arguments.",
        "Do not invent observations or treat uncertain facts as confirmed.",
        "Do not repeat a side-effecting action unless the task context explicitly proves it did not occur.",
        "Choose the shortest safe plan that achieves the goal; if the goal cannot be achieved with available actions, do not fabricate a tool.",
        "Output only the required structured object; do not include prose outside the schema.",
    )

    def __init__(self):
        self._lock = threading.RLock()
        self._inflight: dict[str, str] = {}  # planning request ID -> gateway interaction ID
        self._cancelled: set[str] = set()
        self._queued_results: dict[str, TaskPlanningResult] = {}

    async def plan_goal(
        self,
        engine: "ContinuityEngine",
        goal: str,
        *,
        request_id: str | None = None,
        constraints: Sequence[str] = (),
        priority: float | None = None,
        deadline_in_s: float | None = None,
    ) -> TaskPlanningResult:
        """Ask the model for a plan for a new goal, then enqueue it for core validation."""
        rid = request_id or str(uuid.uuid4())
        if not isinstance(rid, str) or not rid.strip() or len(rid) > 256:
            return TaskPlanningResult("invalid", PlanningStatus.REJECTED, error_code="INVALID_REQUEST_ID")
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 1000:
            return TaskPlanningResult(rid, PlanningStatus.REJECTED, error_code="INVALID_GOAL")
        with self._lock:
            cached = self._queued_results.get(rid)
            if cached is not None:
                return TaskPlanningResult(rid, PlanningStatus.ALREADY_QUEUED, cached.task_id, cached.event_id,
                                          cached.llm_status, cached.error_code, {"original_status": cached.status.value})
            if rid in self._inflight:
                return TaskPlanningResult(rid, PlanningStatus.IN_PROGRESS, error_code="REQUEST_ALREADY_IN_PROGRESS")
        if engine.llm_gateway is None:
            return TaskPlanningResult(rid, PlanningStatus.LLM_FAILED, error_code="LLM_NOT_CONFIGURED")
        if not engine.config.get().get("tasks", {}).get("enabled", True):
            return TaskPlanningResult(rid, PlanningStatus.REJECTED, error_code="TASKS_DISABLED")
        if engine.state.safety_stopped:
            return TaskPlanningResult(rid, PlanningStatus.REJECTED, error_code="SAFETY_STOP_ACTIVE")
        if engine.task_manager.active_task_id is not None:
            return TaskPlanningResult(rid, PlanningStatus.REJECTED, error_code="ACTIVE_TASK_EXISTS",
                                      details={"task_id": engine.task_manager.active_task_id})
        try:
            clean_constraints = self._constraints(constraints)
            numeric_priority = engine.config.get()["tasks"]["default_priority"] if priority is None else priority
            self._validate_deadline(deadline_in_s, engine.config.get()["tasks"]["maximum_task_deadline_s"])
            if not math.isfinite(float(numeric_priority)) or isinstance(numeric_priority, bool):
                raise ValueError("priority must be finite")
        except (TypeError, ValueError) as exc:
            return TaskPlanningResult(rid, PlanningStatus.REJECTED, error_code=f"INVALID_REQUEST:{exc}")
        return await self._generate_and_enqueue(
            engine, request_id=rid, goal=goal.strip(), constraints=clean_constraints,
            task_progress={}, failed_plan={}, replaces_task_id=None,
            priority=float(numeric_priority), deadline_in_s=deadline_in_s,
        )

    async def replan(
        self,
        engine: "ContinuityEngine",
        replan_request: Mapping[str, Any],
        *,
        constraints: Sequence[str] = (),
    ) -> TaskPlanningResult:
        """Replan only a definitely failed current task using its stable request ID."""
        if not isinstance(replan_request, Mapping):
            return TaskPlanningResult("invalid", PlanningStatus.REJECTED, error_code="INVALID_REPLAN_REQUEST")
        req = thaw(replan_request)
        rid = req.get("request_id")
        failed_task_id = req.get("failed_task_id")
        goal = req.get("goal")
        if not all(isinstance(v, str) and v.strip() for v in (rid, failed_task_id, goal)):
            return TaskPlanningResult(str(rid or "invalid"), PlanningStatus.REJECTED, error_code="INVALID_REPLAN_REQUEST")
        if len(rid) > 256:
            return TaskPlanningResult(rid[:256], PlanningStatus.REJECTED, error_code="INVALID_REQUEST_ID")
        with self._lock:
            cached = self._queued_results.get(rid)
            if cached is not None:
                return TaskPlanningResult(rid, PlanningStatus.ALREADY_QUEUED, cached.task_id, cached.event_id,
                                          cached.llm_status, cached.error_code, {"original_status": cached.status.value})
            if rid in self._inflight:
                return TaskPlanningResult(rid, PlanningStatus.IN_PROGRESS, error_code="REQUEST_ALREADY_IN_PROGRESS")
        current = engine.task_manager.active_plan
        if current is None or current.task_id != failed_task_id or current.status != TaskStatus.FAILED:
            return TaskPlanningResult(rid, PlanningStatus.STALE, error_code="FAILED_TASK_NO_LONGER_CURRENT")
        if engine.llm_gateway is None:
            return TaskPlanningResult(rid, PlanningStatus.LLM_FAILED, error_code="LLM_NOT_CONFIGURED")
        if engine.state.safety_stopped:
            return TaskPlanningResult(rid, PlanningStatus.REJECTED, error_code="SAFETY_STOP_ACTIVE")
        try:
            caller_constraints = self._constraints(constraints)
            request_constraints = req.get("constraints", [])
            if not isinstance(request_constraints, list) or any(not isinstance(x, str) for x in request_constraints):
                raise ValueError("replan constraints must be strings")
            combined_constraints = self._constraints([*request_constraints, *caller_constraints])
            completed = req.get("completed_steps", [])
            remaining = req.get("remaining_steps", [])
            if not isinstance(completed, list) or not isinstance(remaining, list):
                raise ValueError("failed plan step lists are malformed")
            progress = {
                "completed_steps": [self._step_summary(row) for row in completed],
                "status": "failed",
                "current_step": str(req.get("failed_step_description", req.get("failed_step_id", "failed step")))[:300],
                "blocked_reason": str(req.get("error_code", "ACTION_FAILED"))[:300],
                "percent_complete": len(completed) / max(1, len(completed) + len(remaining)),
            }
            failed_plan = {
                key: req[key] for key in (
                    "failed_step_id", "failed_step_description", "failed_action_type", "error_code",
                    "completed_steps", "remaining_steps",
                ) if key in req
            }
        except (TypeError, ValueError) as exc:
            return TaskPlanningResult(rid, PlanningStatus.REJECTED, error_code=f"INVALID_REPLAN_REQUEST:{exc}")
        return await self._generate_and_enqueue(
            engine, request_id=rid, goal=goal.strip(), constraints=combined_constraints,
            task_progress=progress, failed_plan=failed_plan, replaces_task_id=failed_task_id,
            priority=float(engine.config.get()["tasks"]["default_priority"]), deadline_in_s=None,
        )

    def cancel(self, engine: "ContinuityEngine", request_id: str) -> bool:
        """Invalidate an inflight planning request; any eventual result is discarded."""
        with self._lock:
            if request_id in self._queued_results:
                return False
            interaction_id = self._inflight.get(request_id)
            if interaction_id is None:
                return False
            self._cancelled.add(request_id)
        if engine.llm_gateway is not None:
            engine.llm_gateway.invalidate_interaction(interaction_id)
        return True

    async def _generate_and_enqueue(
        self,
        engine: "ContinuityEngine",
        *,
        request_id: str,
        goal: str,
        constraints: list[str],
        task_progress: dict[str, Any],
        failed_plan: dict[str, Any],
        replaces_task_id: str | None,
        priority: float,
        deadline_in_s: float | None,
    ) -> TaskPlanningResult:
        with self._lock:
            if request_id in self._queued_results:
                previous = self._queued_results[request_id]
                return TaskPlanningResult(request_id, PlanningStatus.ALREADY_QUEUED, previous.task_id,
                                          previous.event_id, previous.llm_status, previous.error_code)
            if request_id in self._inflight:
                return TaskPlanningResult(request_id, PlanningStatus.IN_PROGRESS,
                                          error_code="REQUEST_ALREADY_IN_PROGRESS")
            self._cancelled.discard(request_id)
            interaction_id = f"task-plan:{request_id}:{uuid.uuid4().hex}"
            self._inflight[request_id] = interaction_id

        try:
            cfg = engine.config.get()
            tasks_cfg = cfg["tasks"]
            allowed_actions = sorted(set(tasks_cfg.get("llm_plannable_action_types", [])) & set(engine.task_manager.allowed_action_types))
            allowed_world_keys = sorted(set(engine.task_manager.allowed_world_keys) & set(engine.state_view_builder.allowed_world_keys))
            if not allowed_world_keys:
                allowed_actions = [action for action in allowed_actions if action != "OBSERVE"]
            if not allowed_actions:
                return TaskPlanningResult(request_id, PlanningStatus.REJECTED, error_code="NO_LLM_PLANNABLE_ACTIONS")
            all_constraints = self._constraints([*self._BASE_CONSTRAINTS, *constraints])
            max_attempts = int(tasks_cfg.get("plan_generation_max_attempts", 2))
            max_steps = int(tasks_cfg["max_steps"])
            task_id = f"task-{uuid.uuid5(uuid.NAMESPACE_URL, request_id).hex[:24]}"
            feedback = ""
            last_error: str | None = None

            for attempt in range(1, max_attempts + 1):
                with self._lock:
                    if request_id in self._cancelled:
                        return TaskPlanningResult(request_id, PlanningStatus.CANCELLED, error_code="PLANNING_CANCELLED",
                                                  details={"attempts": attempt - 1})
                llm_result = await engine.request_llm(
                    purpose="task_plan", interaction_id=interaction_id, generation=0,
                    goal=goal, constraints=all_constraints, allowed_action_types=allowed_actions,
                    allowed_world_fact_keys=allowed_world_keys, validation_feedback=feedback,
                    task_progress=task_progress, failed_plan=failed_plan, max_steps=max_steps,
                )
                with self._lock:
                    if request_id in self._cancelled:
                        return TaskPlanningResult(request_id, PlanningStatus.CANCELLED,
                                                  llm_status=llm_result.status.value, error_code="PLANNING_CANCELLED",
                                                  details={"attempts": attempt})

                if llm_result.status != LLMStatus.COMPLETED:
                    code = llm_result.error_code or llm_result.status.value
                    if (llm_result.status == LLMStatus.REJECTED and attempt < max_attempts
                            and self._repairable_rejection(code)):
                        feedback = self._repair_feedback(code, attempt)
                        last_error = code
                        continue
                    if llm_result.status == LLMStatus.CANCELLED:
                        status = PlanningStatus.CANCELLED
                    elif llm_result.status == LLMStatus.STALE:
                        status = PlanningStatus.STALE
                    elif llm_result.status == LLMStatus.REJECTED:
                        status = PlanningStatus.REJECTED
                    else:
                        status = PlanningStatus.LLM_FAILED
                    return TaskPlanningResult(request_id, status, llm_status=llm_result.status.value,
                                              error_code=code, details={"attempts": attempt})

                # Revalidate the task relationship after every remote call. It may have
                # changed while the provider was generating the candidate.
                if replaces_task_id is not None:
                    current = engine.task_manager.active_plan
                    if current is None or current.task_id != replaces_task_id or current.status != TaskStatus.FAILED:
                        return TaskPlanningResult(request_id, PlanningStatus.STALE, llm_status=llm_result.status.value,
                                                  error_code="FAILED_TASK_CHANGED_DURING_PLANNING",
                                                  details={"attempts": attempt})
                elif engine.task_manager.active_task_id is not None:
                    return TaskPlanningResult(request_id, PlanningStatus.STALE, llm_status=llm_result.status.value,
                                              error_code="ACTIVE_TASK_STARTED_DURING_PLANNING",
                                              details={"attempts": attempt})

                candidate, candidate_error = self._candidate_plan(
                    llm_result.payload, task_id=task_id, goal=goal, priority=priority,
                    deadline_in_s=deadline_in_s, replaces_task_id=replaces_task_id,
                )
                if candidate_error is None and candidate is not None:
                    try:
                        validated = engine.task_manager.validate_candidate(candidate, engine.clock.monotonic())
                    except TaskPlanError as exc:
                        candidate_error = f"PLAN_VALIDATION:{exc}"
                    else:
                        if not validated.accepted:
                            candidate_error = f"PLAN_VALIDATION:{validated.code}"

                if candidate_error:
                    last_error = candidate_error[:700]
                    if attempt < max_attempts and self._repairable_rejection(candidate_error):
                        feedback = self._repair_feedback(candidate_error, attempt)
                        continue
                    return TaskPlanningResult(request_id, PlanningStatus.REJECTED, task_id=task_id,
                                              llm_status=llm_result.status.value, error_code=candidate_error,
                                              details={"attempts": attempt, "max_attempts": max_attempts})

                assert candidate is not None
                # Enqueue and idempotency commit atomically against cancel(). The core
                # remains authoritative and revalidates the plan when it consumes event.
                with self._lock:
                    if request_id in self._cancelled:
                        return TaskPlanningResult(request_id, PlanningStatus.CANCELLED, task_id=task_id,
                                                  llm_status=llm_result.status.value, error_code="PLANNING_CANCELLED",
                                                  details={"attempts": attempt})
                    event = engine.new_event(
                        EventKind.TASK_PLAN_SUBMITTED.value, candidate,
                        source="llm_task_planner", priority=int(Priority.HIGH),
                        expires_in_s=30.0, correlation_id=request_id,
                    )
                    if not engine.inject(event):
                        return TaskPlanningResult(request_id, PlanningStatus.QUEUE_REJECTED, task_id=task_id,
                                                  event_id=event.event_id, llm_status=llm_result.status.value,
                                                  error_code="TASK_PLAN_QUEUE_REJECTED", details={"attempts": attempt})
                    result = TaskPlanningResult(
                        request_id, PlanningStatus.QUEUED, task_id=task_id, event_id=event.event_id,
                        llm_status=llm_result.status.value,
                        details={"step_count": len(candidate["steps"]), "replaces_task_id": replaces_task_id,
                                 "attempts": attempt, "max_attempts": max_attempts,
                                 "repaired": attempt > 1},
                    )
                    self._queued_results[request_id] = result
                    return result

            return TaskPlanningResult(request_id, PlanningStatus.REJECTED, error_code=last_error or "PLAN_REJECTED",
                                      details={"attempts": max_attempts, "max_attempts": max_attempts})
        except Exception as exc:
            # Do not leak prompts or provider exceptions into public diagnostics.
            return TaskPlanningResult(request_id, PlanningStatus.LLM_FAILED, error_code=f"PLANNER_ERROR:{type(exc).__name__}")
        finally:
            with self._lock:
                self._inflight.pop(request_id, None)

    @staticmethod
    def _repairable_rejection(code: str) -> bool:
        """Only retry deterministic content/schema rejection, never timeouts or cancellation."""
        repairable_prefixes = (
            "UNEXPECTED_OUTPUT_FIELDS", "INVALID_TASK_PLAN_STEPS", "INVALID_TASK_PLAN_STEP_SCHEMA",
            "INVALID_TASK_PLAN_DESCRIPTION", "DISALLOWED_TASK_ACTION", "INVALID_TASK_ACTION_ARGS",
            "PLAN_STEPS_MISSING", "PLAN_STEP_NOT_OBJECT", "PLAN_STEP_ARGS_NOT_OBJECT", "PLAN_VALIDATION:",
        )
        return any(code.startswith(prefix) for prefix in repairable_prefixes)

    @staticmethod
    def _repair_feedback(error_code: str, attempt: int) -> str:
        return (
            f"Attempt {attempt} was rejected by the engine validator with code: {error_code[:700]}. "
            "Return a corrected plan for the same goal. Follow action_argument_schemas and allowed_world_fact_keys "
            "exactly; use only permitted enum values and bounded arguments. Change the invalid field(s), do not "
            "repeat the rejected form, and do not invent sensors, keys, action types, or tools."
        )[:1200]

    @staticmethod
    def _candidate_plan(
        payload: Mapping[str, Any], *, task_id: str, goal: str, priority: float,
        deadline_in_s: float | None, replaces_task_id: str | None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        raw_steps = payload.get("steps")
        if not isinstance(raw_steps, (list, tuple)):
            return None, "PLAN_STEPS_MISSING"
        steps: list[dict[str, Any]] = []
        for index, step in enumerate(raw_steps, start=1):
            if not isinstance(step, Mapping):
                return None, "PLAN_STEP_NOT_OBJECT"
            args = thaw(step.get("args", {}))
            if not isinstance(args, dict):
                return None, "PLAN_STEP_ARGS_NOT_OBJECT"
            action_type = str(step.get("action_type", "")).upper()
            if action_type == "SPEAK":
                args.setdefault("mode", "task_plan")
            steps.append({
                "step_id": f"step-{index:02d}",
                "description": str(step.get("description", "")),
                "action_type": action_type,
                "args": args,
            })
        candidate: dict[str, Any] = {"task_id": task_id, "goal": goal, "priority": priority, "steps": steps}
        if deadline_in_s is not None:
            candidate["deadline_in_s"] = deadline_in_s
        if replaces_task_id is not None:
            candidate["replaces_task_id"] = replaces_task_id
        return candidate, None

    @staticmethod
    def _constraints(values: Sequence[str]) -> list[str]:
        if not isinstance(values, (list, tuple)) or len(values) > 40:
            raise ValueError("constraints must be a list/tuple of at most 40 strings")
        clean: list[str] = []
        for value in values:
            if not isinstance(value, str) or not value.strip() or len(value) > 500:
                raise ValueError("each constraint must be a non-empty string up to 500 chars")
            clean.append(value.strip())
        # Preserve order but remove exact duplicates.
        return list(dict.fromkeys(clean))

    @staticmethod
    def _validate_deadline(deadline_in_s: float | None, maximum: float) -> None:
        if deadline_in_s is None:
            return
        if isinstance(deadline_in_s, bool) or not isinstance(deadline_in_s, (int, float)):
            raise ValueError("deadline_in_s must be numeric")
        if not math.isfinite(float(deadline_in_s)) or not 0.1 <= float(deadline_in_s) <= float(maximum):
            raise ValueError("deadline_in_s is out of range")

    @staticmethod
    def _step_summary(row: Any) -> str:
        if not isinstance(row, Mapping):
            return str(row)[:300]
        step_id = row.get("step_id", "step")
        description = row.get("description", "")
        return f"{step_id}: {description}"[:300]
