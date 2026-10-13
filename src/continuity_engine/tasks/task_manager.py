"""Bounded, sequential task plans with explicit action-result reconciliation.

Plans are untrusted proposals. This module validates them and emits one candidate
step at a time; it never executes hardware, calls LLMs, or performs I/O.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
import json
import math
import uuid
from typing import Any, Mapping, Sequence

from ..core.models import ActionStatus, IntentCandidate, validate_json_compatible, thaw


class TaskStatus(str, Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class StepStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


TERMINAL_TASK_STATUSES = {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.EXPIRED}
_ALLOWED_ACTION_ARGS: dict[str, tuple[set[str], set[str]]] = {
    "SPEAK": ({"text"}, {"text", "mode"}),
    "NOTIFY": ({"text"}, {"text"}),
    "OBSERVE": ({"key"}, {"key", "reason"}),
    "WAIT": ({"duration_s"}, {"duration_s"}),
    "LIGHT": ({"pattern"}, {"pattern", "duration_s"}),
}
_SAFE_TO_RETRY_ACTIONS = frozenset({"OBSERVE", "WAIT", "LIGHT"})
_ALLOWED_LIGHT_PATTERNS = frozenset({"idle", "attention", "success", "warning"})


def llm_action_argument_schemas(
    allowed_action_types: Sequence[str], allowed_world_keys: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """Return the exact, bounded argument contract exposed to an LLM planner.

    This is prompt guidance, not an authorization boundary: LLMGateway and
    TaskManager independently validate every returned step against the same policy.
    """
    allowed = {str(value).upper() for value in allowed_action_types}
    world_keys = sorted({str(value) for value in allowed_world_keys if isinstance(value, str) and value})
    schemas: dict[str, dict[str, Any]] = {}
    if "SPEAK" in allowed:
        schemas["SPEAK"] = {
            "required": {"text": {"type": "string", "min_length": 1, "max_length": 2000}},
            "optional": {"mode": {"type": "string", "enum": ["task_plan", "local_template"],
                                    "default": "task_plan"}},
            "notes": ["Do not supply mode unless necessary; the host defaults it to task_plan."],
        }
    if "NOTIFY" in allowed:
        schemas["NOTIFY"] = {
            "required": {"text": {"type": "string", "min_length": 1, "max_length": 2000}},
            "optional": {},
        }
    # An empty world-key allowlist means OBSERVE is unavailable, never unrestricted.
    if "OBSERVE" in allowed and world_keys:
        schemas["OBSERVE"] = {
            "required": {"key": {"type": "string", "enum": world_keys}},
            "optional": {"reason": {"type": "string", "max_length": 200}},
            "notes": ["key must exactly match one allowed_world_fact_keys entry; never invent keys."],
        }
    if "WAIT" in allowed:
        schemas["WAIT"] = {
            "required": {"duration_s": {"type": "number", "minimum": 0, "maximum": 300}},
            "optional": {},
        }
    if "LIGHT" in allowed:
        schemas["LIGHT"] = {
            "required": {"pattern": {"type": "string", "enum": ["idle", "attention", "success", "warning"]}},
            "optional": {"duration_s": {"type": "number", "minimum": 0, "maximum": 60}},
            "pattern_semantics": {
                "idle": "neutral/idle indicator",
                "attention": "draw attention; not a warning",
                "success": "success/normal indicator; green only if the host hardware mapping defines it as green",
                "warning": "warning/amber indicator",
            },
        }
    return schemas


@dataclass(slots=True)
class TaskStep:
    step_id: str
    description: str
    action_type: str
    args: dict[str, Any]
    timeout_s: float
    max_retries: int = 0
    attempts: int = 0
    manual_retries: int = 0
    status: StepStatus = StepStatus.PENDING
    last_error: str | None = None
    bound_intent_id: str | None = None
    bound_action_id: str | None = None
    last_started_at_mono: float | None = None
    completed_at_mono: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id, "description": self.description,
            "action_type": self.action_type, "args": deepcopy(self.args),
            "timeout_s": self.timeout_s, "max_retries": self.max_retries,
            "attempts": self.attempts, "manual_retries": self.manual_retries,
            "status": self.status.value,
            "last_error": self.last_error, "bound_intent_id": self.bound_intent_id,
            "bound_action_id": self.bound_action_id,
            "last_started_at_mono": self.last_started_at_mono,
            "completed_at_mono": self.completed_at_mono,
        }


@dataclass(slots=True)
class TaskPlan:
    task_id: str
    goal: str
    priority: float
    created_at_mono: float
    deadline_mono: float | None
    status: TaskStatus
    steps: list[TaskStep]
    current_step_index: int = 0
    reason: str = "plan_accepted"
    paused_reason: str | None = None
    completed_at_mono: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "goal": self.goal, "priority": self.priority,
            "created_at_mono": self.created_at_mono, "deadline_mono": self.deadline_mono,
            "status": self.status.value, "steps": [s.to_dict() for s in self.steps],
            "current_step_index": self.current_step_index, "reason": self.reason,
            "paused_reason": self.paused_reason, "completed_at_mono": self.completed_at_mono,
        }


@dataclass(frozen=True, slots=True)
class TaskOperationResult:
    accepted: bool
    code: str
    task_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


class TaskPlanError(ValueError):
    pass


class TaskManager:
    """Manage one active sequential plan. Branching/parallelism are intentionally unsupported."""

    def __init__(
        self,
        *,
        allowed_action_types: Sequence[str] = ("SPEAK", "NOTIFY", "OBSERVE", "WAIT", "LIGHT"),
        allowed_world_keys: Sequence[str] = (),
        max_steps: int = 8,
        max_plan_json_bytes: int = 16_384,
        history_limit: int = 20,
        default_priority: float = 0.88,
        default_step_timeout_s: float = 30.0,
        maximum_step_timeout_s: float = 600.0,
        maximum_task_deadline_s: float = 86_400.0,
        minimum_priority: float = 0.65,
        snapshot: Mapping[str, Any] | None = None,
    ):
        if not isinstance(max_steps, int) or isinstance(max_steps, bool) or not 1 <= max_steps <= 32:
            raise ValueError("max_steps must be an integer in [1,32]")
        if not isinstance(max_plan_json_bytes, int) or max_plan_json_bytes < 1024:
            raise ValueError("max_plan_json_bytes must be >= 1024")
        self.allowed_action_types = frozenset(str(x).upper() for x in allowed_action_types) & set(_ALLOWED_ACTION_ARGS)
        self.allowed_world_keys = frozenset(str(x) for x in allowed_world_keys)
        self.max_steps = max_steps
        self.max_plan_json_bytes = max_plan_json_bytes
        self.history_limit = max(1, int(history_limit))
        self.default_priority = self._finite_range(default_priority, "default_priority", 0.0, 1.0)
        self.default_step_timeout_s = self._finite_range(default_step_timeout_s, "default_step_timeout_s", 0.1, 600.0)
        self.maximum_step_timeout_s = self._finite_range(maximum_step_timeout_s, "maximum_step_timeout_s", 0.1, 600.0)
        self.maximum_task_deadline_s = self._finite_range(maximum_task_deadline_s, "maximum_task_deadline_s", 0.1, 86_400.0)
        self.minimum_priority = self._finite_range(minimum_priority, "minimum_priority", 0.0, 1.0)
        if self.default_priority < self.minimum_priority:
            raise ValueError("default_priority must be >= minimum_priority")
        if self.default_step_timeout_s > self.maximum_step_timeout_s:
            raise ValueError("default_step_timeout_s cannot exceed maximum_step_timeout_s")
        self.active_plan: TaskPlan | None = None
        self.history: list[dict[str, Any]] = []
        if snapshot:
            self.load_snapshot(snapshot, recovering=True)

    @property
    def active_task_id(self) -> str | None:
        if self.active_plan is not None and self.active_plan.status not in TERMINAL_TASK_STATUSES:
            return self.active_plan.task_id
        return None

    @property
    def current_step(self) -> TaskStep | None:
        plan = self.active_plan
        if plan is None or plan.status in TERMINAL_TASK_STATUSES or not 0 <= plan.current_step_index < len(plan.steps):
            return None
        return plan.steps[plan.current_step_index]

    @property
    def active_action_id(self) -> str | None:
        step = self.current_step
        return step.bound_action_id if step else None

    def validate_candidate(self, raw_plan: Mapping[str, Any], now_mono: float) -> TaskOperationResult:
        """Validate against an isolated copy so preflight never mutates the live task state."""
        candidate = deepcopy(self)
        return candidate.submit(raw_plan, now_mono)

    def submit(self, raw_plan: Mapping[str, Any], now_mono: float) -> TaskOperationResult:
        if not math.isfinite(now_mono) or now_mono < 0:
            raise TaskPlanError("now_mono must be finite and >= 0")
        if self.active_plan is not None and self.active_plan.status not in TERMINAL_TASK_STATUSES:
            return TaskOperationResult(False, "ACTIVE_TASK_EXISTS", self.active_plan.task_id)
        if not isinstance(raw_plan, Mapping):
            raise TaskPlanError("plan must be a mapping")
        # Events deliberately freeze nested mappings/lists. Convert that immutable DTO
        # into a detached plain-data copy at this trust boundary before parsing.
        normalized = thaw(raw_plan)
        try:
            encoded = json.dumps(normalized, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise TaskPlanError(f"plan is not JSON-compatible: {exc}") from exc
        if len(encoded.encode("utf-8")) > self.max_plan_json_bytes:
            raise TaskPlanError("plan exceeds max_plan_json_bytes")
        allowed_top = {"task_id", "goal", "priority", "deadline_in_s", "steps", "replaces_task_id"}
        unknown = set(normalized) - allowed_top
        if unknown:
            raise TaskPlanError(f"unknown plan fields: {sorted(unknown)}")
        replaces_task_id = normalized.get("replaces_task_id")
        if replaces_task_id is not None:
            if not isinstance(replaces_task_id, str) or not replaces_task_id.strip():
                raise TaskPlanError("replaces_task_id must be a non-empty string")
            previous = self.active_plan
            if previous is None or previous.task_id != replaces_task_id:
                raise TaskPlanError("replaces_task_id does not match the current task")
            if previous.status != TaskStatus.FAILED:
                raise TaskPlanError("only a definitely failed task can be replaced by a replan")
        raw_task_id = normalized.get("task_id")
        if "task_id" not in normalized:
            task_id = str(uuid.uuid4())
        elif not isinstance(raw_task_id, str) or not raw_task_id.strip() or len(raw_task_id) > 128:
            raise TaskPlanError("task_id must be a non-empty string of <= 128 characters")
        else:
            task_id = raw_task_id
        goal = normalized.get("goal")
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 500:
            raise TaskPlanError("goal must be a non-empty string of <= 500 characters")
        priority = self._finite_range(normalized.get("priority", self.default_priority), "priority", 0.0, 1.0)
        if priority < self.minimum_priority:
            raise TaskPlanError(f"priority must be >= policy threshold {self.minimum_priority}")
        deadline_in_s = normalized.get("deadline_in_s")
        if deadline_in_s is None:
            deadline = None
        else:
            deadline_in_s = self._finite_range(deadline_in_s, "deadline_in_s", 0.1, self.maximum_task_deadline_s)
            deadline = now_mono + deadline_in_s
        raw_steps = normalized.get("steps")
        if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= self.max_steps:
            raise TaskPlanError(f"steps must contain 1..{self.max_steps} entries")
        steps: list[TaskStep] = []
        seen_ids: set[str] = set()
        for index, raw_step in enumerate(raw_steps):
            steps.append(self._parse_step(raw_step, index, seen_ids))
        if not task_id.strip() or len(task_id) > 128:
            raise TaskPlanError("task_id must contain 1..128 characters")
        if self.active_plan is not None and self.active_plan.task_id == task_id:
            raise TaskPlanError("task_id must not reuse the previous task ID")
        if any(item.get("task_id") == task_id for item in self.history):
            raise TaskPlanError("task_id already exists in task history")
        if self.active_plan is not None:
            self._archive_active()
        self.active_plan = TaskPlan(task_id, goal.strip(), priority, now_mono, deadline, TaskStatus.ACTIVE, steps)
        return TaskOperationResult(True, "PLAN_ACCEPTED", task_id, {"step_count": len(steps)})

    def _parse_step(self, raw: Any, index: int, seen_ids: set[str]) -> TaskStep:
        if not isinstance(raw, Mapping):
            raise TaskPlanError(f"steps[{index}] must be a mapping")
        allowed_fields = {"step_id", "description", "action_type", "args", "timeout_s", "max_retries"}
        unknown = set(raw) - allowed_fields
        if unknown:
            raise TaskPlanError(f"steps[{index}] has unknown fields: {sorted(unknown)}")
        raw_step_id = raw.get("step_id")
        if "step_id" not in raw:
            step_id = f"step-{index + 1}"
        elif not isinstance(raw_step_id, str) or not raw_step_id.strip() or len(raw_step_id) > 128:
            raise TaskPlanError(f"steps[{index}] has invalid step_id")
        else:
            step_id = raw_step_id
        if step_id in seen_ids:
            raise TaskPlanError(f"steps[{index}] has duplicate step_id")
        seen_ids.add(step_id)
        description = raw.get("description", step_id)
        if not isinstance(description, str) or not description.strip() or len(description) > 500:
            raise TaskPlanError(f"steps[{index}].description must be a non-empty string of <= 500 characters")
        action_type = raw.get("action_type")
        if not isinstance(action_type, str):
            raise TaskPlanError(f"steps[{index}].action_type must be a string")
        action_type = action_type.upper()
        if action_type not in self.allowed_action_types:
            raise TaskPlanError(f"action type {action_type!r} is not available for task plans")
        args = raw.get("args", {})
        if not isinstance(args, Mapping):
            raise TaskPlanError(f"steps[{index}].args must be a mapping")
        args = dict(args)
        required, permitted = _ALLOWED_ACTION_ARGS[action_type]
        if not required <= set(args) or not set(args) <= permitted:
            raise TaskPlanError(f"invalid args for {action_type}: required={sorted(required)}, allowed={sorted(permitted)}")
        self._validate_action_args(action_type, args, index)
        timeout_s = self._finite_range(raw.get("timeout_s", self.default_step_timeout_s), f"steps[{index}].timeout_s", 0.1, self.maximum_step_timeout_s)
        max_retries = raw.get("max_retries", 0)
        if not isinstance(max_retries, int) or isinstance(max_retries, bool) or not 0 <= max_retries <= 2:
            raise TaskPlanError(f"steps[{index}].max_retries must be integer in [0,2]")
        if max_retries > 0 and action_type not in _SAFE_TO_RETRY_ACTIONS:
            raise TaskPlanError(f"automatic retries are prohibited for side-effecting action {action_type}")
        validate_json_compatible(args, f"steps[{index}].args")
        return TaskStep(step_id, description, action_type, args, timeout_s, max_retries)

    def _validate_action_args(self, action_type: str, args: dict[str, Any], index: int) -> None:
        if action_type in {"SPEAK", "NOTIFY"}:
            text = args.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 2000:
                raise TaskPlanError(f"steps[{index}].args.text must contain 1..2000 characters")
            if action_type == "SPEAK" and "mode" in args and (not isinstance(args["mode"], str) or args["mode"] not in {"task_plan", "local_template"}):
                raise TaskPlanError(f"steps[{index}].args.mode is not allowed")
        elif action_type == "OBSERVE":
            key = args.get("key")
            if not isinstance(key, str) or not key:
                raise TaskPlanError(f"steps[{index}].args.key must be non-empty")
            if key not in self.allowed_world_keys:
                raise TaskPlanError(f"world fact key {key!r} is not allowlisted")
            if "reason" in args and (not isinstance(args["reason"], str) or len(args["reason"]) > 200):
                raise TaskPlanError(f"steps[{index}].args.reason must be a string of <= 200 characters")
        elif action_type == "WAIT":
            self._finite_range(args["duration_s"], f"steps[{index}].args.duration_s", 0.0, 300.0)
        elif action_type == "LIGHT":
            if not isinstance(args["pattern"], str) or args["pattern"] not in _ALLOWED_LIGHT_PATTERNS:
                raise TaskPlanError(f"steps[{index}].args.pattern is not allowlisted")
            if "duration_s" in args:
                self._finite_range(args["duration_s"], f"steps[{index}].args.duration_s", 0.0, 60.0)

    @staticmethod
    def _finite_range(value: Any, name: str, low: float, high: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not low <= float(value) <= high:
            raise TaskPlanError(f"{name} must be finite and in [{low}, {high}]")
        return float(value)

    def candidate(self, now_mono: float) -> IntentCandidate | None:
        if not math.isfinite(now_mono) or now_mono < 0:
            raise ValueError("now_mono must be finite and >= 0")
        plan = self.active_plan
        step = self.current_step
        if plan is None or plan.status != TaskStatus.ACTIVE or step is None or step.status != StepStatus.PENDING:
            return None
        if plan.deadline_mono is not None and now_mono >= plan.deadline_mono:
            plan.status = TaskStatus.EXPIRED
            plan.reason = "task_deadline_expired"
            plan.completed_at_mono = now_mono
            if step.status == StepStatus.RUNNING:
                step.status = StepStatus.UNKNOWN
                plan.status = TaskStatus.NEEDS_RECONCILIATION
                plan.paused_reason = "task_deadline_expired_during_action"
            elif step.status == StepStatus.PENDING:
                step.status = StepStatus.CANCELLED
            return None
        deadline = now_mono + step.timeout_s
        if plan.deadline_mono is not None:
            deadline = min(deadline, plan.deadline_mono)
        return IntentCandidate(
            intent_type="task_step", goal=self._goal_for(plan, step), score=plan.priority,
            urgency=0.25, reason=f"task_plan_step:{plan.task_id}:{step.step_id}",
            action_type=step.action_type, action_args=step.args,
            deadline_mono=deadline, interruption_policy="RESUME_IF_VALID",
        )

    @staticmethod
    def _goal_for(plan: TaskPlan, step: TaskStep) -> str:
        return f"task:{plan.task_id}:step:{step.step_id}"

    def bind_intent(self, intent_id: str, goal: str) -> bool:
        plan = self.active_plan
        step = self.current_step
        if plan is None or plan.status != TaskStatus.ACTIVE or step is None or step.status != StepStatus.PENDING:
            return False
        if goal != self._goal_for(plan, step):
            return False
        if step.bound_intent_id not in {None, intent_id}:
            return False
        step.bound_intent_id = intent_id
        return True

    def bind_action(self, intent_id: str, action_id: str, now_mono: float) -> bool:
        plan = self.active_plan
        step = self.current_step
        if plan is None or plan.status != TaskStatus.ACTIVE or step is None:
            return False
        if step.bound_intent_id != intent_id or step.status != StepStatus.PENDING:
            return False
        if step.bound_action_id not in {None, action_id}:
            return False
        step.bound_action_id = action_id
        step.status = StepStatus.RUNNING
        step.attempts += 1
        step.last_started_at_mono = now_mono
        return True

    def unbind_action_before_start(self, intent_id: str, action_id: str, reason: str) -> bool:
        """Release a task action binding when safety vetoes dispatch before executor entry.

        The attempt counter is rolled back because the action was never handed to the
        executor. This is intentionally not used after execute() has been called.
        """
        plan = self.active_plan
        step = self.current_step
        if plan is None or plan.status != TaskStatus.ACTIVE or step is None:
            return False
        if step.status != StepStatus.RUNNING or step.bound_intent_id != intent_id or step.bound_action_id != action_id:
            return False
        step.status = StepStatus.PENDING
        step.bound_intent_id = None
        step.bound_action_id = None
        step.attempts = max(0, step.attempts - 1)
        step.last_started_at_mono = None
        plan.reason = f"action_not_started:{reason}"
        return True

    def replan_request(self) -> dict[str, Any] | None:
        """Return a stable, minimized request for the host planner after definite failure.

        This method never calls an LLM. The host can deduplicate `request_id`, ask its
        planner for a replacement, then submit the plan with `replaces_task_id`.
        """
        plan = self.active_plan
        if plan is None or plan.status != TaskStatus.FAILED:
            return None
        if not 0 <= plan.current_step_index < len(plan.steps):
            return None
        failed = plan.steps[plan.current_step_index]
        if failed.status != StepStatus.FAILED:
            return None
        request_id = f"replan:{plan.task_id}:{failed.step_id}:{failed.attempts}"
        completed = [
            {"step_id": row.step_id, "description": row.description, "action_type": row.action_type}
            for row in plan.steps[:plan.current_step_index] if row.status == StepStatus.SUCCEEDED
        ]
        remaining = [
            {"step_id": row.step_id, "description": row.description, "action_type": row.action_type,
             "status": row.status.value}
            for row in plan.steps[plan.current_step_index:]
        ]
        return {
            "request_id": request_id,
            "failed_task_id": plan.task_id,
            "goal": plan.goal,
            "failed_step_id": failed.step_id,
            "failed_step_description": failed.description,
            "failed_action_type": failed.action_type,
            "error_code": failed.last_error or "ACTION_FAILED",
            "completed_steps": completed,
            "remaining_steps": remaining,
            "allowed_action_types": sorted(self.allowed_action_types),
            "constraints": [
                "Return a bounded linear plan only.",
                "Do not repeat a side-effecting action unless it is known not to have occurred.",
                "Use only allowlisted actions and arguments.",
                "Treat the failed step's outcome as failed, not successful.",
            ],
        }

    def on_action_result(
        self,
        *,
        action_id: str,
        intent_id: str,
        status: str,
        now_mono: float,
        error_code: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> TaskOperationResult:
        plan = self.active_plan
        step = self.current_step
        if plan is None or step is None or step.status != StepStatus.RUNNING:
            return TaskOperationResult(False, "NO_RUNNING_TASK_STEP")
        if step.bound_action_id != action_id or step.bound_intent_id != intent_id:
            return TaskOperationResult(False, "ACTION_CORRELATION_MISMATCH", plan.task_id)
        details = details or {}
        step.last_error = error_code
        step.bound_action_id = None
        step.bound_intent_id = None
        step.completed_at_mono = now_mono
        if status == ActionStatus.SUCCEEDED.value:
            step.status = StepStatus.SUCCEEDED
            plan.current_step_index += 1
            plan.reason = f"step_succeeded:{step.step_id}"
            if plan.current_step_index >= len(plan.steps):
                plan.status = TaskStatus.SUCCEEDED
                plan.completed_at_mono = now_mono
                plan.reason = "all_steps_succeeded"
            return TaskOperationResult(True, "STEP_SUCCEEDED", plan.task_id, {"step_id": step.step_id, "next_step_index": plan.current_step_index})
        if status == ActionStatus.FAILED.value:
            can_retry = (
                step.action_type in _SAFE_TO_RETRY_ACTIONS
                and step.attempts <= step.max_retries
                and details.get("retryable") is True
            )
            if can_retry:
                step.status = StepStatus.PENDING
                plan.reason = f"retry_scheduled:{step.step_id}"
                return TaskOperationResult(True, "SAFE_RETRY_SCHEDULED", plan.task_id, {"step_id": step.step_id, "attempts": step.attempts})
            step.status = StepStatus.FAILED
            plan.status = TaskStatus.FAILED
            plan.reason = f"step_failed:{step.step_id}"
            plan.completed_at_mono = now_mono
            return TaskOperationResult(True, "TASK_FAILED", plan.task_id, {"step_id": step.step_id})
        if status == ActionStatus.CANCELLED.value:
            step.status = StepStatus.CANCELLED
            plan.status = TaskStatus.PAUSED
            plan.paused_reason = f"step_cancelled:{step.step_id}"
            plan.reason = plan.paused_reason
            return TaskOperationResult(True, "TASK_PAUSED_AFTER_CANCEL", plan.task_id, {"step_id": step.step_id})
        # TIMEOUT/UNKNOWN can have external side effects: never replay automatically.
        step.status = StepStatus.UNKNOWN
        plan.status = TaskStatus.NEEDS_RECONCILIATION
        plan.paused_reason = f"outcome_unknown:{step.step_id}"
        plan.reason = plan.paused_reason
        return TaskOperationResult(True, "TASK_NEEDS_RECONCILIATION", plan.task_id, {"step_id": step.step_id})

    def cancel(self, task_id: str | None, reason: str = "user_cancelled_task", now_mono: float | None = None) -> TaskOperationResult:
        plan = self.active_plan
        if plan is None or plan.status in TERMINAL_TASK_STATUSES:
            return TaskOperationResult(False, "NO_ACTIVE_TASK", None if plan is None else plan.task_id)
        if task_id is not None and task_id != plan.task_id:
            return TaskOperationResult(False, "TASK_ID_MISMATCH", plan.task_id)
        for step in plan.steps:
            if step.status in {StepStatus.PENDING, StepStatus.RUNNING}:
                step.status = StepStatus.CANCELLED
                step.bound_action_id = None
                step.bound_intent_id = None
        plan.status = TaskStatus.CANCELLED
        plan.reason = reason
        plan.completed_at_mono = now_mono
        return TaskOperationResult(True, "TASK_CANCELLED", plan.task_id)

    def pause(self, reason: str = "paused") -> TaskOperationResult:
        plan = self.active_plan
        if plan is None or plan.status != TaskStatus.ACTIVE:
            return TaskOperationResult(False, "NO_ACTIVE_TASK", None if plan is None else plan.task_id)
        plan.status = TaskStatus.PAUSED
        plan.paused_reason = reason
        plan.reason = reason
        return TaskOperationResult(True, "TASK_PAUSED", plan.task_id)

    def resume(self, task_id: str | None = None) -> TaskOperationResult:
        plan = self.active_plan
        if plan is None or plan.status != TaskStatus.PAUSED:
            return TaskOperationResult(False, "TASK_NOT_PAUSED", None if plan is None else plan.task_id)
        if task_id is not None and task_id != plan.task_id:
            return TaskOperationResult(False, "TASK_ID_MISMATCH", plan.task_id)
        step = self.current_step
        if step is None or step.status != StepStatus.PENDING:
            return TaskOperationResult(False, "STEP_REQUIRES_RECONCILIATION", plan.task_id)
        plan.status = TaskStatus.ACTIVE
        plan.paused_reason = None
        plan.reason = "task_resumed"
        return TaskOperationResult(True, "TASK_RESUMED", plan.task_id)

    def resolve_step(self, task_id: str, step_id: str, outcome: str, now_mono: float, *, confirmed_not_completed: bool = False) -> TaskOperationResult:
        plan = self.active_plan
        if plan is None or plan.task_id != task_id or plan.status not in {TaskStatus.PAUSED, TaskStatus.NEEDS_RECONCILIATION}:
            return TaskOperationResult(False, "TASK_NOT_RECONCILABLE", task_id)
        step = self.current_step
        if step is None or step.step_id != step_id or step.status not in {StepStatus.UNKNOWN, StepStatus.CANCELLED}:
            return TaskOperationResult(False, "STEP_NOT_RECONCILABLE", task_id)
        if outcome == "succeeded":
            step.status = StepStatus.SUCCEEDED
            step.last_error = None
            step.bound_action_id = None
            step.bound_intent_id = None
            plan.current_step_index += 1
            if plan.current_step_index >= len(plan.steps):
                plan.status = TaskStatus.SUCCEEDED
                plan.completed_at_mono = now_mono
                plan.reason = "resolved_as_succeeded"
            else:
                plan.status = TaskStatus.PAUSED
                plan.reason = "resolved_as_succeeded_continue_paused"
            plan.paused_reason = None
            return TaskOperationResult(True, "STEP_RESOLVED_SUCCEEDED", task_id, {"next_step_index": plan.current_step_index})
        if outcome == "retry":
            if not confirmed_not_completed:
                return TaskOperationResult(False, "RETRY_REQUIRES_NONCOMPLETION_CONFIRMATION", task_id)
            # One human-authorized retry is allowed if an operator/user explicitly
            # confirms that the side effect did not occur. Never allow repeated manual
            # retry loops on a step.
            if step.manual_retries >= 1:
                return TaskOperationResult(False, "MANUAL_RETRY_BUDGET_EXHAUSTED", task_id)
            step.manual_retries += 1
            step.status = StepStatus.PENDING
            step.last_error = None
            step.bound_action_id = None
            step.bound_intent_id = None
            plan.status = TaskStatus.PAUSED
            plan.reason = "step_retry_authorized_but_task_stays_paused"
            plan.paused_reason = "explicit_resume_required"
            return TaskOperationResult(True, "STEP_RETRY_AUTHORIZED", task_id)
        if outcome == "failed":
            step.status = StepStatus.FAILED
            step.bound_action_id = None
            step.bound_intent_id = None
            plan.status = TaskStatus.FAILED
            plan.reason = "step_manually_resolved_failed"
            plan.completed_at_mono = now_mono
            plan.paused_reason = None
            return TaskOperationResult(True, "TASK_FAILED_BY_RESOLUTION", task_id)
        raise TaskPlanError("outcome must be succeeded, retry or failed")

    def expire_if_needed(self, now_mono: float) -> bool:
        plan = self.active_plan
        if plan and plan.status == TaskStatus.ACTIVE and plan.deadline_mono is not None and now_mono >= plan.deadline_mono:
            # Capture the step before changing plan.status: current_step intentionally
            # returns None for terminal plans.
            step = self.current_step
            plan.completed_at_mono = now_mono
            if step is not None and step.status == StepStatus.RUNNING:
                step.status = StepStatus.UNKNOWN
                step.last_error = step.last_error or "task_deadline_expired_during_action"
                plan.status = TaskStatus.NEEDS_RECONCILIATION
                plan.reason = "task_deadline_expired_during_action"
                plan.paused_reason = plan.reason
            else:
                if step is not None and step.status == StepStatus.PENDING:
                    step.status = StepStatus.CANCELLED
                plan.status = TaskStatus.EXPIRED
                plan.reason = "task_deadline_expired"
            return True
        return False

    def is_task_intent(self, intent_id: str | None) -> bool:
        step = self.current_step
        return bool(step is not None and intent_id and step.bound_intent_id == intent_id)

    def is_task_goal(self, goal: str | None) -> bool:
        plan = self.active_plan
        step = self.current_step
        return bool(plan is not None and step is not None and goal == self._goal_for(plan, step))

    def unbind_unstarted_intent(self, intent_id: str, reason: str) -> bool:
        step = self.current_step
        if step is None or step.bound_intent_id != intent_id or step.status != StepStatus.PENDING or step.bound_action_id is not None:
            return False
        step.bound_intent_id = None
        if self.active_plan is not None:
            self.active_plan.reason = f"intent_unbound:{reason}"
        return True

    def on_action_blocked(self, intent_id: str, reason: str, *, temporary: bool) -> bool:
        plan = self.active_plan
        step = self.current_step
        if plan is None or step is None or step.bound_intent_id != intent_id or step.status != StepStatus.PENDING:
            return False
        if temporary:
            plan.reason = f"step_waiting:{reason}"
            return True
        step.bound_intent_id = None
        plan.status = TaskStatus.PAUSED
        plan.paused_reason = f"safety_denied:{reason}"
        plan.reason = plan.paused_reason
        return True

    def pause_active(self, reason: str = "paused") -> TaskOperationResult:
        result = self.pause(reason)
        step = self.current_step
        if step is not None and step.status == StepStatus.PENDING and step.bound_action_id is None:
            step.bound_intent_id = None
        return result

    def load_snapshot(self, snapshot: Mapping[str, Any], *, recovering: bool) -> None:
        if not isinstance(snapshot, Mapping):
            raise TaskPlanError("task snapshot must be a mapping")
        allowed = {"active_plan", "history"}
        if set(snapshot) - allowed:
            raise TaskPlanError("task snapshot has unknown fields")
        raw_plan = snapshot.get("active_plan")
        self.active_plan = self._plan_from_dict(raw_plan) if raw_plan is not None else None
        history = snapshot.get("history", [])
        if not isinstance(history, list):
            raise TaskPlanError("task history must be a list")
        self.history = [deepcopy(x) for x in history[-self.history_limit:] if isinstance(x, dict)]
        if recovering and self.active_plan is not None and self.active_plan.status not in TERMINAL_TASK_STATUSES:
            plan = self.active_plan
            step = self.current_step
            if step and step.status == StepStatus.RUNNING:
                step.status = StepStatus.UNKNOWN
                step.last_error = "process_restarted_during_action"
                step.bound_action_id = None
                step.bound_intent_id = None
                plan.status = TaskStatus.NEEDS_RECONCILIATION
                plan.reason = "restart_during_action_requires_reconciliation"
                plan.paused_reason = plan.reason
            elif step and step.status == StepStatus.PENDING:
                # Unsent pending steps are safe to keep, but require explicit resumption
                # after restart. Drop old in-memory intent bindings before issuing a new one.
                step.bound_intent_id = None
                step.bound_action_id = None
                plan.status = TaskStatus.PAUSED
                plan.reason = "restart_requires_explicit_resume"
                plan.paused_reason = plan.reason

    def snapshot(self) -> dict[str, Any]:
        return {
            "active_plan": None if self.active_plan is None else self.active_plan.to_dict(),
            "history": deepcopy(self.history[-self.history_limit:]),
        }

    def _archive_active(self) -> None:
        if self.active_plan is None:
            return
        self.history.append(self.active_plan.to_dict())
        self.history = self.history[-self.history_limit:]

    def _plan_from_dict(self, raw: Any) -> TaskPlan:
        if raw is None:
            return None  # type: ignore[return-value]
        if not isinstance(raw, Mapping):
            raise TaskPlanError("active plan snapshot must be a mapping")
        allowed_plan_fields = {
            "task_id", "goal", "priority", "created_at_mono", "deadline_mono", "status",
            "steps", "current_step_index", "reason", "paused_reason", "completed_at_mono",
        }
        if set(raw) - allowed_plan_fields:
            raise TaskPlanError("persisted task plan has unknown fields")
        task_id = raw.get("task_id")
        goal = raw.get("goal")
        if not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 128:
            raise TaskPlanError("persisted task_id is invalid")
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 500:
            raise TaskPlanError("persisted task goal is invalid")
        steps_raw = raw.get("steps")
        if not isinstance(steps_raw, list) or not 1 <= len(steps_raw) <= self.max_steps:
            raise TaskPlanError("persisted task plan has invalid steps")
        steps: list[TaskStep] = []
        seen_ids: set[str] = set()
        allowed_step_fields = {
            "step_id", "description", "action_type", "args", "timeout_s", "max_retries",
            "attempts", "manual_retries", "status", "last_error", "bound_intent_id",
            "bound_action_id", "last_started_at_mono", "completed_at_mono",
        }
        for index, row in enumerate(steps_raw):
            if not isinstance(row, Mapping):
                raise TaskPlanError("persisted task step must be a mapping")
            if set(row) - allowed_step_fields:
                raise TaskPlanError("persisted task step has unknown fields")
            step_id = row.get("step_id")
            description = row.get("description", "")
            action_type = row.get("action_type")
            if not isinstance(step_id, str) or not step_id.strip() or len(step_id) > 128 or step_id in seen_ids:
                raise TaskPlanError("persisted task step ID is invalid or duplicated")
            seen_ids.add(step_id)
            if not isinstance(description, str) or not description.strip() or len(description) > 500:
                raise TaskPlanError("persisted task step description is invalid")
            if not isinstance(action_type, str):
                raise TaskPlanError("persisted task action type is invalid")
            action_type = action_type.upper()
            if action_type not in self.allowed_action_types:
                raise TaskPlanError(f"persisted action type {action_type!r} is not allowlisted")
            raw_args = row.get("args", {})
            if not isinstance(raw_args, Mapping):
                raise TaskPlanError("persisted task args must be a mapping")
            args = thaw(raw_args)
            timeout_s = self._finite_range(row.get("timeout_s"), "persisted timeout_s", 0.1, self.maximum_step_timeout_s)
            max_retries = row.get("max_retries", 0)
            attempts = row.get("attempts", 0)
            manual_retries = row.get("manual_retries", 0)
            for label, number, low, high in (
                ("max_retries", max_retries, 0, 2),
                ("attempts", attempts, 0, 1000),
                ("manual_retries", manual_retries, 0, 1),
            ):
                if not isinstance(number, int) or isinstance(number, bool) or not low <= number <= high:
                    raise TaskPlanError(f"persisted {label} is invalid")
            status = StepStatus(row.get("status", "PENDING"))
            last_started = row.get("last_started_at_mono")
            completed_at = row.get("completed_at_mono")
            for label, timestamp in (("last_started_at_mono", last_started), ("completed_at_mono", completed_at)):
                if timestamp is not None and (isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(float(timestamp)) or float(timestamp) < 0):
                    raise TaskPlanError(f"persisted {label} is invalid")
            step = TaskStep(
                step_id=step_id, description=description, action_type=action_type, args=args,
                timeout_s=timeout_s, max_retries=max_retries, attempts=attempts,
                manual_retries=manual_retries, status=status,
                last_error=None if row.get("last_error") is None else str(row.get("last_error")),
                bound_intent_id=None if row.get("bound_intent_id") is None else str(row.get("bound_intent_id")),
                bound_action_id=None if row.get("bound_action_id") is None else str(row.get("bound_action_id")),
                last_started_at_mono=None if last_started is None else float(last_started),
                completed_at_mono=None if completed_at is None else float(completed_at),
            )
            if max_retries and action_type not in _SAFE_TO_RETRY_ACTIONS:
                raise TaskPlanError("persisted retries are not allowed for this action type")
            if manual_retries and step.attempts < 1:
                raise TaskPlanError("persisted manual retry count requires at least one attempt")
            self._validate_action_args(action_type, args, index)
            validate_json_compatible(step.to_dict(), "task_step")
            steps.append(step)
        priority = self._finite_range(raw.get("priority"), "persisted priority", 0.0, 1.0)
        created = raw.get("created_at_mono")
        if isinstance(created, bool) or not isinstance(created, (int, float)) or not math.isfinite(float(created)) or float(created) < 0:
            raise TaskPlanError("persisted created_at_mono is invalid")
        deadline_raw = raw.get("deadline_mono")
        deadline = None
        if deadline_raw is not None:
            if isinstance(deadline_raw, bool) or not isinstance(deadline_raw, (int, float)) or not math.isfinite(float(deadline_raw)) or float(deadline_raw) < float(created):
                raise TaskPlanError("persisted deadline_mono is invalid")
            deadline = float(deadline_raw)
        completed_raw = raw.get("completed_at_mono")
        completed = None
        if completed_raw is not None:
            if isinstance(completed_raw, bool) or not isinstance(completed_raw, (int, float)) or not math.isfinite(float(completed_raw)) or float(completed_raw) < float(created):
                raise TaskPlanError("persisted completed_at_mono is invalid")
            completed = float(completed_raw)
        status = TaskStatus(raw.get("status"))
        current_index = raw.get("current_step_index", 0)
        if not isinstance(current_index, int) or isinstance(current_index, bool) or not 0 <= current_index <= len(steps):
            raise TaskPlanError("persisted task index invalid")
        if status in {TaskStatus.ACTIVE, TaskStatus.PAUSED, TaskStatus.NEEDS_RECONCILIATION, TaskStatus.FAILED, TaskStatus.EXPIRED} and current_index >= len(steps):
            raise TaskPlanError("persisted non-success task points beyond the final step")
        if status == TaskStatus.SUCCEEDED and current_index != len(steps):
            raise TaskPlanError("persisted successful task is missing completed steps")
        plan = TaskPlan(
            task_id=task_id, goal=goal, priority=priority, created_at_mono=float(created),
            deadline_mono=deadline, status=status, steps=steps, current_step_index=current_index,
            reason=str(raw.get("reason", "restored")),
            paused_reason=None if raw.get("paused_reason") is None else str(raw.get("paused_reason")),
            completed_at_mono=completed,
        )
        validate_json_compatible(plan.to_dict(), "task_plan")
        return plan

