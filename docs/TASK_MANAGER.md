# Task Manager — v0.4.0-dev

## Scope

The first task manager supports one active linear plan at a time. It accepts a plan proposal as `TASK_PLAN_SUBMITTED`, validates every step against a small action allowlist, emits one step as a normal `IntentCandidate`, and advances only after a correlated terminal action result. It performs no I/O and never calls the LLM or hardware itself.

Parallel steps, conditional branches, arbitrary tool names, and automatic execution of an LLM-generated replan are intentionally unsupported. After a **definite** step failure, `EngineStepResult.replan_request` exposes a stable, minimized request to the host application. The host may ask its planner for an alternative plan, then submit it with `replaces_task_id` equal to the failed task ID. This does not happen automatically inside the core. Unknown/timed-out outcomes do not produce a replan request because the previous side effect may have occurred.

## Plan event payload

```json
{
  "task_id": "prepare-001",
  "goal": "Give a short greeting then observe room activity",
  "priority": 0.88,
  "deadline_in_s": 180,
  "steps": [
    {"step_id": "greet", "action_type": "SPEAK", "args": {"text": "你好。"}, "timeout_s": 15},
    {"step_id": "observe", "action_type": "OBSERVE", "args": {"key": "room_activity"}, "timeout_s": 10, "max_retries": 1}
  ]
}
```

The `OBSERVE.key` must be in the configured LLM/world fact allowlist. All plan payloads are treated as untrusted, regardless of source. The action args are schema-checked and bounded. `SPEAK`/`NOTIFY` are side-effecting and cannot configure automatic retries. `OBSERVE`, `WAIT` and `LIGHT` retries are considered only when the executor explicitly reports `details.retryable=true` and the retry budget remains.

## Task lifecycle

- `ACTIVE`: next pending step may be proposed.
- `PAUSED`: no step runs until explicit resume; a cancelled/ambiguous step needs resolution first.
- `NEEDS_RECONCILIATION`: action result is unknown/timed out, or restart occurred during a running action. No automatic replay.
- `SUCCEEDED`, `FAILED`, `CANCELLED`, `EXPIRED`: terminal.

Step lifecycle: `PENDING -> RUNNING -> SUCCEEDED`; retryable failure may return to `PENDING`. `CANCELLED` or `UNKNOWN` blocks automatic progress. `TASK_RESOLVE_STEP` must explicitly record `succeeded`, `retry` (with `confirmed_not_completed=true`), or `failed`. `TASK_RESUME` is a separate explicit operation.

## Event API

- `TASK_PLAN_SUBMITTED`: payload is the plan itself.
- `TASK_CANCEL`: optional `task_id`, optional `reason`; uses the protected control queue and requests prompt cancellation of a running task action.
- `TASK_PAUSE`: optional `task_id`, optional `reason`.
- `TASK_RESUME`: optional `task_id`.
- `TASK_RESOLVE_STEP`: `task_id`, `step_id`, `outcome`, and optional `confirmed_not_completed`.

Example:

```python
engine.inject(engine.new_event(
    EventKind.TASK_PLAN_SUBMITTED.value,
    plan_payload,
    source="task_planner",
    priority=90,
))
result = engine.step(dt=0.0)
for proposal in result.proposals:
    await engine.dispatch_action(proposal)
# Call engine.step() again to consume ACTION_TERMINAL and advance the next step.
```

## Recovery and idempotency

Task snapshots are stored in `EngineState.task_state`; new snapshots use schema 1.1. Snapshot schema 1.0 remains readable with an empty task state. A pending (not yet dispatched) step is paused on recovery and its stale intent binding is cleared. A running step becomes `UNKNOWN` and its task becomes `NEEDS_RECONCILIATION`; the system refuses to replay it automatically because the action may have had external effects before the crash.

## Observation feedback contract

A successful `OBSERVE` action may return one `details.observation` object containing only `value`, `confidence`, and optional `ttl_s`. The engine binds that result to the exact `OBSERVE.key` in the dispatched proposal, checks the requested key against the configured world-fact allowlist, validates confidence and TTL, then queues a normal `WORLD_OBSERVATION` event. The executor cannot choose a different key or write directly to the world model. Missing or malformed observations do not fail the whole task step; they produce a diagnostic and no world update.

## Host-side replanning contract

`EngineStepResult.replan_request` is non-null only when the current task has a definitively failed step. It contains a stable `request_id`, the failed task/step, completed step summaries, remaining step summaries, error code, and allowed action types. It intentionally omits raw step arguments and must be treated as planner context, not permission to execute.

The host should deduplicate by `request_id`, call an LLM only if policy allows, validate the returned JSON shape, add `replaces_task_id` using `failed_task_id`, and inject `TASK_PLAN_SUBMITTED`. `TaskManager` re-validates all fields and refuses to replace a non-failed or mismatched task. This does not happen automatically inside the core. Unknown/timed-out outcomes do not produce replan requests because the previous side effect may have occurred. If a proposed replacement plan is invalid, leave the old plan failed and report the failure; never fall back to arbitrary tool execution.

Example: `python examples/task_replan_demo.py`. The example uses deterministic fallback planning and replay adapters, so it makes no external calls.

## Safety boundaries

The task manager is not a safety controller. Each step still passes the engine policy, `SafetySupervisor`, and action executor. A real adapter must implement cancellation/timeout semantics correctly and must not claim that local coroutine cancellation guarantees a physical action stopped. All real physical actuation requires a separate hardware safety controller.
