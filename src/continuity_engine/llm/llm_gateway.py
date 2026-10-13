"""Async LLM gateway with purpose views, bounded concurrency and stale-result rejection.

A provider implementation is intentionally not bundled. Applications must inject a
provider, and replay mode only accepts ReplayLLMAdapter.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
import math
import json
from enum import Enum
import time
import uuid
from threading import RLock
from typing import Any, Mapping, Protocol

from .state_views import StateView
from ..core.models import validate_json_compatible


class LLMStatus(str, Enum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    STALE = "STALE"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class LLMRequest:
    request_id: str
    interaction_id: str
    intent_id: str | None
    generation: int
    purpose: str
    created_at_mono: float
    deadline_mono: float
    context_json: str
    output_token_budget: int = 1000
    #: Whether re-sending this request is free of externally visible side effects.
    #: Pure text generation defaults to True; a request that could trigger an external
    #: effect (e.g. provider-side tool execution) must set this to False so the
    #: reliability layer refuses to replay it after an unknown-outcome failure.
    replay_safe: bool = True


@dataclass(frozen=True, slots=True)
class LLMResult:
    request_id: str
    status: LLMStatus
    payload: Mapping[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    latency_s: float = 0.0


class LLMAdapter(Protocol):
    is_replay_adapter: bool

    async def generate(self, request: LLMRequest) -> Mapping[str, Any]: ...


class ReplayLLMAdapter:
    """Fixture-only adapter; never performs network I/O."""
    is_replay_adapter = True

    def __init__(
        self,
        fixtures: Mapping[str, Mapping[str, Any]] | None = None,
        delay_s: float = 0.0,
        *,
        response_sequences: Mapping[str, list[Mapping[str, Any]]] | None = None,
    ):
        self.fixtures = {k: dict(v) for k, v in (fixtures or {}).items()}
        # Optional deterministic sequences support retry/recovery tests while keeping
        # the exact ReplayLLMAdapter type required by ReplayGuard.
        self.response_sequences = {k: [dict(item) for item in rows] for k, rows in (response_sequences or {}).items()}
        self.delay_s = delay_s
        self.calls: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> Mapping[str, Any]:
        self.calls.append(request)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        key = request.request_id if request.request_id in self.fixtures else request.purpose
        if key in self.response_sequences and self.response_sequences[key]:
            return dict(self.response_sequences[key].pop(0))
        if key not in self.fixtures:
            raise LookupError(f"no replay fixture for {key}")
        return dict(self.fixtures[key])


class StubLLMAdapter(ReplayLLMAdapter):
    """Deterministic test adapter for ordinary unit tests; no external connection."""


class ReplayModeError(RuntimeError):
    pass


class ReplayGuard:
    @staticmethod
    def validate(*, replay_mode: bool, llm_adapter: object | None, action_executor: object) -> None:
        if not replay_mode:
            return
        if llm_adapter is not None and type(llm_adapter) is not ReplayLLMAdapter:
            raise ReplayModeError("replay mode requires the exact ReplayLLMAdapter type or no LLM adapter")
        from ..execution.actions import ReplayActionExecutor
        if type(action_executor) is not ReplayActionExecutor:
            raise ReplayModeError("replay mode requires the exact ReplayActionExecutor type; subclasses are not trusted")


class LLMGateway:
    def __init__(self, adapter: LLMAdapter, *, timeout_s: float = 12.0, max_concurrent: int = 1,
                 context_token_budgets: Mapping[str, int] | None = None, replay_mode: bool = False,
                 action_executor: object | None = None, max_output_retries: int = 2):
        if not math.isfinite(timeout_s) or timeout_s <= 0 or max_concurrent < 1:
            raise ValueError("timeout_s must be finite and positive; max_concurrent must be >= 1")
        if replay_mode:
            if action_executor is None:
                raise ReplayModeError("replay mode requires an injected simulated action executor")
            ReplayGuard.validate(replay_mode=True, llm_adapter=adapter, action_executor=action_executor)
        self.adapter = adapter
        self.timeout_s = timeout_s
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self.context_token_budgets = dict(context_token_budgets or {
            "demand_understanding": 3500,
            "simple_response": 2000, "complex_dialogue": 4000,
            "multi_step_plan": 6000, "task_plan": 6000, "memory_consolidation": 4000,
        })
        if any(not isinstance(v, int) or isinstance(v, bool) or v < 1 for v in self.context_token_budgets.values()):
            raise ValueError("context token budgets must be positive integers")
        if isinstance(max_output_retries, bool) or not isinstance(max_output_retries, int) or not 0 <= max_output_retries <= 5:
            raise ValueError("max_output_retries must be an integer in [0,5]")
        self.max_output_retries = max_output_retries
        self.replay_mode = replay_mode
        self._active_generation: dict[str, int] = {}
        self._inflight: dict[str, asyncio.Task] = {}
        self._request_meta: dict[str, tuple[str, int, str | None]] = {}
        self._state_lock = RLock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def _cancel_tasks_threadsafe(self, tasks: list[asyncio.Task]) -> None:
        tasks = [task for task in tasks if not task.done()]
        if not tasks:
            return
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        def cancel_all() -> None:
            for task in tasks:
                if not task.done():
                    task.cancel()
        if running_loop is loop:
            cancel_all()
        else:
            loop.call_soon_threadsafe(cancel_all)

    def set_generation(self, interaction_id: str, generation: int) -> None:
        if not interaction_id or not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise ValueError("interaction_id must be non-empty and generation a non-negative integer")
        tasks: list[asyncio.Task] = []
        with self._state_lock:
            previous = self._active_generation.get(interaction_id)
            self._active_generation[interaction_id] = generation
            if previous is not None and previous != generation:
                for request_id, (iid, req_generation, _intent_id) in list(self._request_meta.items()):
                    worker = self._inflight.get(request_id)
                    if iid == interaction_id and req_generation != generation and worker is not None:
                        tasks.append(worker)
        self._cancel_tasks_threadsafe(tasks)

    def invalidate_interaction(self, interaction_id: str) -> None:
        with self._state_lock:
            generation = self._active_generation.get(interaction_id, 0)
        self.set_generation(interaction_id, generation + 1)

    def invalidate_intent(self, intent_id: str) -> None:
        """Cancel inflight cognition work tied to an intent; callers still validate late results."""
        with self._state_lock:
            tasks = [
                self._inflight[request_id]
                for request_id, (_iid, _gen, req_intent_id) in list(self._request_meta.items())
                if req_intent_id == intent_id and request_id in self._inflight
            ]
        self._cancel_tasks_threadsafe(tasks)

    async def generate(
        self, *, interaction_id: str, intent_id: str | None, generation: int,
        purpose: str, view: StateView, timeout_s: float | None = None,
        replay_safe: bool = True,
    ) -> LLMResult:
        if view.purpose != purpose:
            return LLMResult("", LLMStatus.REJECTED, error_code="PURPOSE_VIEW_MISMATCH")
        if purpose not in self.context_token_budgets:
            return LLMResult("", LLMStatus.REJECTED, error_code="UNKNOWN_PURPOSE")
        context_json = view.to_json()
        estimated_tokens = _conservative_token_estimate(context_json)
        if estimated_tokens > self.context_token_budgets[purpose]:
            return LLMResult("", LLMStatus.REJECTED, error_code="CONTEXT_BUDGET_EXCEEDED")
        timeout = self.timeout_s if timeout_s is None else timeout_s
        if not math.isfinite(timeout) or timeout <= 0:
            return LLMResult("", LLMStatus.REJECTED, error_code="INVALID_TIMEOUT")

        loop = asyncio.get_running_loop()
        now = time.monotonic()
        request_id = str(uuid.uuid4())
        req = LLMRequest(request_id, interaction_id, intent_id, generation, purpose, now, now + timeout,
                         context_json, replay_safe=bool(replay_safe))
        # Register worker and generation check under one lock so a concurrent ingress
        # generation bump cannot slip between validation and registration.
        with self._state_lock:
            self._loop = loop
            active = self._active_generation.setdefault(interaction_id, generation)
            if generation != active:
                return LLMResult("", LLMStatus.STALE, error_code="GENERATION_ALREADY_STALE")
            worker = loop.create_task(self._execute_request(req, timeout))
            self._inflight[request_id] = worker
            self._request_meta[request_id] = (interaction_id, generation, intent_id)
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # Cancellation of the outer application task is propagated; the provider
            # worker is cancelled too. Internal invalidation is caught by the child and
            # returned as an ordinary LLMStatus.CANCELLED/STALE result.
            worker.cancel()
            raise
        finally:
            with self._state_lock:
                self._inflight.pop(request_id, None)
                self._request_meta.pop(request_id, None)

    async def _execute_request(self, req: LLMRequest, timeout: float) -> LLMResult:
        started = time.monotonic()
        try:
            feedback: str | None = None
            for attempt in range(self.max_output_retries + 1):
                attempt_req = req
                if feedback:
                    try:
                        attempt_context = json.loads(req.context_json)
                        if isinstance(attempt_context, dict):
                            attempt_context["_output_validation_feedback"] = feedback
                            attempt_req = replace(req, context_json=json.dumps(attempt_context, ensure_ascii=False, separators=(",", ":")))
                    except (ValueError, TypeError):
                        pass

                async def call_with_semaphore() -> Mapping[str, Any]:
                    async with self._semaphore:
                        return await self.adapter.generate(attempt_req)

                try:
                    raw = await asyncio.wait_for(call_with_semaphore(), timeout=max(0.001, req.deadline_mono - time.monotonic()))
                except Exception as exc:
                    # Retry only malformed provider output, not network/auth/server failures.
                    # A non-replay-safe request is never re-sent, even for schema repair.
                    if (type(exc).__name__ != "ProviderResponseError" or attempt >= self.max_output_retries
                            or not req.replay_safe):
                        raise
                    feedback = "The previous response was discarded because it was not valid JSON matching the required output schema. Return a corrected response with exactly the required fields and types; no markdown or extra text."
                    continue

                with self._state_lock:
                    active_generation = self._active_generation.get(req.interaction_id)
                if active_generation != req.generation:
                    return LLMResult(req.request_id, LLMStatus.STALE, error_code="RESULT_GENERATION_STALE", latency_s=time.monotonic() - started)
                validation_error = self._validate_payload(req.purpose, raw, req.context_json)
                if validation_error is None:
                    return LLMResult(req.request_id, LLMStatus.COMPLETED, dict(raw), latency_s=time.monotonic() - started)
                # Task-plan repair already has a domain-specific bounded retry loop in
                # LLMTaskPlanner; avoid stacking a second retry loop on top of it.
                if (req.purpose == "task_plan" or attempt >= self.max_output_retries
                        or not req.replay_safe):
                    return LLMResult(req.request_id, LLMStatus.REJECTED, error_code=validation_error, latency_s=time.monotonic() - started)
                # Discard invalid output; provide only the schema error code for a bounded retry.
                feedback = f"The previous response was discarded: {validation_error}. Return a corrected JSON object matching the exact required schema. Do not include markdown, commentary, or additional fields."
            return LLMResult(req.request_id, LLMStatus.REJECTED, error_code="OUTPUT_RETRY_EXHAUSTED", latency_s=time.monotonic() - started)
        except asyncio.TimeoutError:
            return LLMResult(req.request_id, LLMStatus.TIMED_OUT, error_code="TIMEOUT", latency_s=time.monotonic() - started)
        except asyncio.CancelledError:
            with self._state_lock:
                stale = self._active_generation.get(req.interaction_id) != req.generation
            status = LLMStatus.STALE if stale else LLMStatus.CANCELLED
            return LLMResult(req.request_id, status, error_code="INVALIDATED" if stale else "CANCELLED", latency_s=time.monotonic() - started)
        except Exception as exc:
            return LLMResult(req.request_id, LLMStatus.FAILED, error_code=type(exc).__name__, latency_s=time.monotonic() - started)

    @staticmethod
    def _validate_payload(purpose: str, payload: Mapping[str, Any], context_json: str = "{}") -> str | None:
        if not isinstance(payload, Mapping):
            return "OUTPUT_NOT_OBJECT"
        try:
            validate_json_compatible(payload, "llm.output")
        except (TypeError, ValueError):
            return "INVALID_OUTPUT_TYPES"
        if purpose == "demand_understanding":
            expected = {"schema_version", "request_type", "intent", "entities", "emotion_assessment", "clarification_needed", "response_candidate"}
            if set(payload) != expected:
                return "INVALID_DEMAND_FIELDS"
            if payload.get("schema_version") != "1.0":
                return "INVALID_DEMAND_SCHEMA_VERSION"
            allowed_types = {"emotional_sharing", "casual_chat", "information_request", "task_request", "device_control", "memory_update", "other", "unclear"}
            if payload.get("request_type") not in allowed_types:
                return "INVALID_REQUEST_TYPE"
            intent = payload.get("intent")
            if not isinstance(intent, str) or not intent.strip() or len(intent) > 1000:
                return "INVALID_DEMAND_INTENT"
            entities = payload.get("entities")
            if not isinstance(entities, list) or len(entities) > 32:
                return "INVALID_DEMAND_ENTITIES"
            for entity in entities:
                if not isinstance(entity, Mapping) or set(entity) != {"type", "value"}:
                    return "INVALID_DEMAND_ENTITY"
                if not isinstance(entity.get("type"), str) or not entity["type"].strip() or len(entity["type"]) > 64:
                    return "INVALID_DEMAND_ENTITY"
                if not isinstance(entity.get("value"), str) or len(entity["value"]) > 500:
                    return "INVALID_DEMAND_ENTITY"
            emotion = payload.get("emotion_assessment")
            if not isinstance(emotion, Mapping) or set(emotion) != {"emotion", "confidence", "evidence"}:
                return "INVALID_EMOTION_ASSESSMENT"
            label, score, evidence = emotion.get("emotion"), emotion.get("confidence"), emotion.get("evidence")
            if label is not None and (not isinstance(label, str) or len(label) > 80):
                return "INVALID_EMOTION_LABEL"
            if score is not None and (isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(float(score)) or not 0 <= score <= 1):
                return "INVALID_EMOTION_CONFIDENCE"
            if not isinstance(evidence, list) or len(evidence) > 5 or any(not isinstance(x, str) or len(x) > 200 for x in evidence):
                return "INVALID_EMOTION_EVIDENCE"
            if not isinstance(payload.get("clarification_needed"), bool):
                return "INVALID_CLARIFICATION_FLAG"
            candidate = payload.get("response_candidate")
            if candidate is not None and (not isinstance(candidate, str) or len(candidate) > 4000):
                return "INVALID_RESPONSE_CANDIDATE"
        elif purpose in {"simple_response", "complex_dialogue"}:
            # Optional user-affect estimate is produced in the same provider request,
            # preserving single-call-by-default dialogue. It is never an action or
            # diagnostic, and low-confidence/unknown labels are ignored by the host.
            if set(payload) not in ({"response_text"}, {"response_text", "emotion_assessment"}):
                return "UNEXPECTED_OUTPUT_FIELDS"
            text = payload.get("response_text")
            if not isinstance(text, str) or not text.strip() or len(text) > 8000:
                return "INVALID_RESPONSE_TEXT"
            if "emotion_assessment" in payload:
                emotion = payload["emotion_assessment"]
                if not isinstance(emotion, Mapping) or set(emotion) != {"emotion", "confidence", "evidence"}:
                    return "INVALID_EMOTION_ASSESSMENT"
                label, score, evidence = emotion.get("emotion"), emotion.get("confidence"), emotion.get("evidence")
                if label is not None and (not isinstance(label, str) or len(label) > 80):
                    return "INVALID_EMOTION_LABEL"
                if score is not None and (isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(float(score)) or not 0 <= float(score) <= 1):
                    return "INVALID_EMOTION_CONFIDENCE"
                if not isinstance(evidence, list) or len(evidence) > 5 or any(not isinstance(x, str) or len(x) > 200 for x in evidence):
                    return "INVALID_EMOTION_EVIDENCE"
        elif purpose == "multi_step_plan":
            if set(payload) != {"steps"}:
                return "UNEXPECTED_OUTPUT_FIELDS"
            steps = payload.get("steps")
            allowed_step_keys = {"description", "tool", "arguments", "preconditions", "expected_result"}
            if not isinstance(steps, list) or len(steps) > 30:
                return "INVALID_PLAN_STEPS"
            try:
                context = json.loads(context_json)
                allowed_tools = set(context.get("allowed_tools", []))
            except Exception:
                return "INVALID_PLAN_CONTEXT"
            for step in steps:
                if not isinstance(step, Mapping) or set(step) - allowed_step_keys:
                    return "INVALID_PLAN_STEPS"
                if not isinstance(step.get("description"), str) or not step["description"].strip() or len(step["description"]) > 1000:
                    return "INVALID_PLAN_STEPS"
                if "tool" in step and (not isinstance(step["tool"], str) or step["tool"] not in allowed_tools):
                    return "DISALLOWED_TOOL_PROPOSAL"
                if "arguments" in step and not isinstance(step["arguments"], Mapping):
                    return "INVALID_PLAN_STEPS"
                for key in ("preconditions",):
                    if key in step and (not isinstance(step[key], list) or any(not isinstance(v, str) for v in step[key])):
                        return "INVALID_PLAN_STEPS"
                if "expected_result" in step and not isinstance(step["expected_result"], str):
                    return "INVALID_PLAN_STEPS"
        elif purpose == "task_plan":
            if set(payload) != {"steps"}:
                return "UNEXPECTED_OUTPUT_FIELDS"
            try:
                context = json.loads(context_json)
                allowed_actions = set(context.get("allowed_action_types", []))
                allowed_world_keys = context.get("allowed_world_fact_keys", [])
                max_steps = int(context.get("max_steps", 0))
            except Exception:
                return "INVALID_TASK_PLAN_CONTEXT"
            if not isinstance(allowed_world_keys, list) or any(not isinstance(key, str) or not key for key in allowed_world_keys):
                return "INVALID_TASK_PLAN_CONTEXT"
            steps = payload.get("steps")
            if not isinstance(steps, list) or not 1 <= len(steps) <= max_steps:
                return "INVALID_TASK_PLAN_STEPS"
            for step in steps:
                if not isinstance(step, Mapping) or set(step) != {"description", "action_type", "args"}:
                    return "INVALID_TASK_PLAN_STEP_SCHEMA"
                description = step.get("description")
                action_type = step.get("action_type")
                args = step.get("args")
                if not isinstance(description, str) or not description.strip() or len(description) > 500:
                    return "INVALID_TASK_PLAN_DESCRIPTION"
                if not isinstance(action_type, str) or action_type not in allowed_actions:
                    return "DISALLOWED_TASK_ACTION"
                if not isinstance(args, Mapping) or not _validate_task_action_args(
                    action_type, args, allowed_world_keys=allowed_world_keys,
                ):
                    return "INVALID_TASK_ACTION_ARGS"
        elif purpose == "memory_consolidation":
            if set(payload) != {"candidate_memories"}:
                return "UNEXPECTED_OUTPUT_FIELDS"
            memories = payload.get("candidate_memories")
            if not isinstance(memories, list) or len(memories) > 100:
                return "INVALID_MEMORY_PROPOSALS"
            try:
                context = json.loads(context_json)
                known_refs = {str(event.get("event_id")) for event in context.get("candidate_events", []) if isinstance(event, Mapping)}
                known_refs.update(str(ref) for event in context.get("candidate_events", []) if isinstance(event, Mapping) for ref in event.get("source_refs", []))
            except Exception:
                return "INVALID_MEMORY_CONTEXT"
            allowed_memory_keys = {"summary", "source_refs", "kind", "fact_status"}
            for memory in memories:
                if not isinstance(memory, Mapping) or set(memory) - allowed_memory_keys:
                    return "MEMORY_PROPOSAL_MISSING_PROVENANCE_OR_INVALID_FIELDS"
                if not isinstance(memory.get("summary"), str) or not memory["summary"].strip() or len(memory["summary"]) > 2000:
                    return "INVALID_MEMORY_SUMMARY"
                refs = memory.get("source_refs")
                if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) for ref in refs):
                    return "MEMORY_PROPOSAL_MISSING_PROVENANCE_OR_INVALID_FIELDS"
                if not known_refs or any(ref not in known_refs for ref in refs):
                    return "MEMORY_PROVENANCE_NOT_IN_INPUT"
                if "kind" in memory and (not isinstance(memory["kind"], str) or not memory["kind"].strip()):
                    return "INVALID_MEMORY_KIND"
                if memory.get("fact_status", "candidate") not in {"candidate", "confirmed", "inferred", "unknown"}:
                    return "INVALID_MEMORY_FACT_STATUS"
        return None


def _validate_task_action_args(
    action_type: str, args: Mapping[str, Any], *, allowed_world_keys: list[str] | None = None,
) -> bool:
    """Shape-only validation; TaskManager performs final policy/world-key validation."""
    schemas = {
        "SPEAK": ({"text"}, {"text", "mode"}),
        "NOTIFY": ({"text"}, {"text"}),
        "OBSERVE": ({"key"}, {"key", "reason"}),
        "WAIT": ({"duration_s"}, {"duration_s"}),
        "LIGHT": ({"pattern"}, {"pattern", "duration_s"}),
    }
    if action_type not in schemas:
        return False
    required, permitted = schemas[action_type]
    if not required <= set(args) or not set(args) <= permitted:
        return False
    try:
        validate_json_compatible(args, "llm.task_action.args")
    except (TypeError, ValueError):
        return False
    if action_type in {"SPEAK", "NOTIFY"}:
        text = args.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            return False
        if "mode" in args and (action_type != "SPEAK" or args["mode"] not in {"task_plan", "local_template"}):
            return False
    elif action_type == "OBSERVE":
        allowed_keys = set(allowed_world_keys or [])
        if not isinstance(args.get("key"), str) or args["key"] not in allowed_keys:
            return False
        if "reason" in args and (not isinstance(args["reason"], str) or len(args["reason"]) > 200):
            return False
    elif action_type == "WAIT":
        value = args.get("duration_s")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not 0 <= float(value) <= 300:
            return False
    elif action_type == "LIGHT":
        if args.get("pattern") not in {"idle", "attention", "success", "warning"}:
            return False
        if "duration_s" in args:
            value = args["duration_s"]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not 0 <= float(value) <= 60:
                return False
    return True


def _conservative_token_estimate(text: str) -> int:
    # Provider tokenizers vary. Count non-ASCII characters conservatively and
    # approximate ASCII at 4 chars/token; inject the provider tokenizer in production.
    non_ascii = sum(1 for char in text if ord(char) > 127)
    ascii_chars = len(text) - non_ascii
    return max(1, non_ascii + (ascii_chars + 3) // 4)
