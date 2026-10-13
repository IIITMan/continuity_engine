# LLM Task Planner (v0.7 development increment)

## Purpose

`LLMTaskPlanner` connects the existing filtered LLM gateway to the sequential `TaskManager`.
It does not execute actions or mutate engine state directly. It builds a purpose-specific
`task_plan` view, gets a candidate plan, performs local preflight using an isolated copy of
the live task manager, and enqueues a normal `TASK_PLAN_SUBMITTED` event. The main core loop
remains the final acceptance authority.

## Model output contract

The only accepted top-level field is `steps`. Each step is exactly:

```json
{
  "description": "Observe the room activity",
  "action_type": "OBSERVE",
  "args": {"key": "room_activity"}
}
```

Supported action types are the intersection of:

- `tasks.llm_plannable_action_types` configuration;
- the `TaskManager` supported action types;
- the action schema and world-key allowlists.

The model cannot set task IDs, priorities, deadlines, retries, parallelism, branches,
Python callables, shell commands, or arbitrary provider tools. Step IDs, default timeouts,
and retry budgets are supplied by local policy. `TaskManager.submit()` validates the plan
again when the event is consumed.

## New task flow

1. Host calls `await planner.plan_goal(engine, goal, request_id=...)`.
2. Planner asks `engine.request_llm(purpose="task_plan", ...)` with filtered state, allowed
   action types, the exact `action_argument_schemas`, the exact `allowed_world_fact_keys`,
   maximum step count, constraints, and bounded `validation_feedback`.
3. LLM Gateway checks structured output and action-specific field/range/enum rules; planner
   then preflights the complete action plan against TaskManager.
4. Planner enqueues `TASK_PLAN_SUBMITTED` and returns `QUEUED` (not `ACCEPTED`).
5. Host calls `engine.step()`. The core performs authoritative validation and records diagnostics.
6. Host dispatches returned proposals, feeds results back through the event queue, and continues ticking.

## Bounded repair on validator rejection

`tasks.plan_generation_max_attempts` defaults to `2` (one initial proposal plus at most one repair).
Only deterministic output/schema/plan rejections are eligible for repair. The retry receives the
validator error code and the same exact action/world-key schemas in `validation_feedback`. Provider
timeouts, cancellations, stale interactions, network failures and authorization denials are not retried.
Every attempt is counted in `TaskPlanningResult.details`; an invalid plan is never enqueued.

The prompt includes exact constraints, not just field names. For example, `LIGHT.pattern` is limited
to `idle`, `attention`, `success`, or `warning` (`success` is the green/normal pattern); `WAIT.duration_s`
must be within `[0, 300]`; `OBSERVE.key` must be a member of `allowed_world_fact_keys`. If the world-key
allowlist is empty, `OBSERVE` is removed from the model-visible action set. `TaskManager` independently
enforces the same allowlist and fails closed when it is empty.

A queued plan can still be rejected by the core if the live state changes between preflight and
consumption. Hosts must inspect core diagnostics/task state to learn whether the plan was accepted.

## Replanning flow

On definite failure, `EngineStepResult.replan_request` contains a stable request ID and minimized
failure progress. The host calls `await planner.replan(engine, result.replan_request)`. The service
checks that the same task is still in `FAILED`, passes only the bounded failure summary to the
LLM, and requires `replaces_task_id` to match that exact failed task. The task manager remains
the authority for replacement acceptance.

Ambiguous results (`UNKNOWN`/timeouts) are not automatically replanned because the action may
have caused side effects. First use the task reconciliation API to establish the outcome.

## Cancellation and idempotency

- `request_id` is an idempotency key. Once a plan event is queued, a duplicate request returns
  `ALREADY_QUEUED` and does not call the model again.
- An inflight duplicate returns `IN_PROGRESS`.
- `planner.cancel(engine, request_id)` invalidates the gateway interaction. The final cancellation
  check and enqueue/cache commit are synchronized to avoid a successful cancel racing with enqueue.
- Request IDs are bounded. Rejected/failed generation attempts are not cached as queued and may be retried.

## Provider adapter and replay boundary

An optional `OpenAICompatibleAdapter` is available through the `provider` extra (`httpx`). Configure an
explicit endpoint URL and model using `CONTINUITY_LLM_ENDPOINT`, `CONTINUITY_LLM_MODEL`, and optionally
`CONTINUITY_LLM_API_KEY` / `CONTINUITY_LLM_TIMEOUT_S`. Keep credentials out of YAML/source control. The
adapter uses purpose-specific system constraints and requests JSON-object output; the gateway and task
manager still perform independent validation. Different compatible endpoints may not implement every
request field identically, so test the chosen service in a controlled environment before user-facing use.

In replay mode, only `ReplayLLMAdapter` and `ReplayActionExecutor` are accepted by the existing guard.
`examples/llm_task_plan_demo.py` uses a fixture adapter and performs no network or hardware I/O. The
provider adapter's own tests use `httpx.MockTransport`, not a real endpoint. Process/container egress
restriction remains recommended for replay tests.

## Concurrency contract

`llm.max_concurrent_requests` and the `LLMGateway` default are `1`, matching the measured provider
behavior from the v0.6.1 integration report. A semaphore serializes provider calls, but does **not** make
multiple stateful turns on one `ContinuityEngine` independent. The host must serialize turn orchestration
per engine instance, or use separate engine instances for truly separate sessions. A newer turn may
invalidate an older generation; hosts should treat `STALE`/`CANCELLED` as expected supersession states,
not as provider failures to blindly retry.
