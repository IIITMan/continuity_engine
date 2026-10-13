# Real-provider reliability and acceptance drill

Status: implementation baseline for the `v0.25.0.dev0` development line. The reliability
layer and the drill harness are verified by deterministic tests (mock transports and
fake adapters). The vendor numbers in `docs/PROVIDER_DRILL_REPORT.md` come from a
separate, explicitly opt-in live run against a real endpoint; that run is not part of
CI and no credential is stored in this repository.

## Why a separate layer

`LLMGateway` owns *semantic* concerns: purpose-specific state views, generation
staleness, output-schema validation and a bounded schema-repair loop. It deliberately
does **not** retry network failures, because it cannot know whether re-sending a
request is safe. `continuity_engine.llm.reliability` owns *transport* concerns and is
an opt-in adapter wrapper, so replay mode and the deterministic suite keep their exact
previous behavior.

```
LLMGateway            purpose view -> deadline -> validate -> bounded schema repair
   |
ReliableLLMAdapter    circuit breaker -> classified retry -> rate-limit backoff
   |
OpenAICompatibleAdapter  connection pool -> SSE streaming -> typed HTTP errors -> usage
   |
provider HTTP endpoint
```

## Failure taxonomy

`classify_provider_failure(exc)` maps an exception to one `FailureKind`. Classification
never inspects prompt or response text.

| kind | typical cause | auto-retried |
| --- | --- | --- |
| `CONNECT` | connection/stream-level transport failure: DNS/socket error, reset, truncated read, protocol error | yes |
| `TIMEOUT` | read/write/pool timeout — **outcome unknown** | yes, only when replay-safe |
| `RATE_LIMIT` | HTTP 429, quota/throttle signal | yes, honoring `Retry-After` |
| `SERVER` | HTTP 5xx | yes |
| `AUTH` | HTTP 401/403 | no — fails until credentials are fixed |
| `REQUEST` | other 4xx (malformed or rejected request) | no — the same body will fail again |
| `INVALID_OUTPUT` | provider answered, body violates the JSON contract | no — the gateway's schema repair owns this |
| `CIRCUIT_OPEN` | local fast-fail while the breaker is open | no |
| `UNKNOWN` | anything unrecognized | no (fail closed) |

`AUTH` and `REQUEST` failures are also excluded from opening the circuit: they are
deterministic and would otherwise trip the breaker during a misconfiguration.

## The replay rule (no blanket auto-replay)

A retry re-sends a request whose outcome may be unknown. That is only acceptable when
re-sending cannot duplicate an external side effect.

* `LLMRequest.replay_safe` defaults to `True` because a chat completion is pure text
  generation: the server holds no state and the engine, not the model, decides what to
  do with the text.
* Any caller whose request could trigger an externally visible effect (for example a
  future adapter that performs provider-side tool/function execution) must set
  `replay_safe=False`. `LLMGateway.generate(..., replay_safe=False)` propagates the flag.
* For such a request `ReliableLLMAdapter` performs exactly one attempt, never waits,
  raises the original failure, and increments `retries_suppressed_no_replay`. The host
  is expected to reconcile rather than replay.
* The same flag disables the gateway's output-schema repair, which is also a re-send.

The drill asserts this behaviour explicitly
(`side_effecting_request_is_never_replayed`).

## Controlled retry

`RetryPolicy` is bounded on four independent axes: attempts (`max_attempts`), per-delay
cap (`max_delay_s`), total retry wait (`max_total_retry_wait_s`) and jitter
(`jitter_ratio`). Backoff is exponential with bounded jitter.

Two rules make retries safe under a hard deadline:

1. A retry is abandoned if the computed wait would exceed the request deadline — the
   wrapper fails fast instead of guaranteeing a timeout.
2. A retry is abandoned once the accumulated wait would exceed
   `max_total_retry_wait_s`. A long `Retry-After` therefore cannot stall a turn
   indefinitely.

Rate limits are handled by raising the backoff to at least the server's `Retry-After`
(parsed only when it is a bounded numeric value; HTTP-date form is ignored rather than
guessed). `respect_retry_after=False` disables that.

## Circuit breaker and recovery

`CircuitBreaker` counts consecutive failures, opens after `failure_threshold`, refuses
calls fast while open, allows a single half-open probe after `recovery_timeout_s`, and
closes again on the first success. A failure while half-open re-opens immediately.

Recovery is observable: `ReliableLLMAdapter.snapshot()` (also exposed through
`LLMRuntime.reliability_snapshot()`) reports the state, consecutive failures, open
events and rejections, plus every retry/rate-limit counter. Counters never contain
prompts, responses, credentials or exception text.

`LLMRuntime.reset_reliability()` exists for a host that has independently established
that an outage ended; the drill deliberately does **not** use it, so "recovery" means
the breaker recovered on its own.

## Connection pool, timeouts and streaming

`OpenAICompatibleAdapter` accepts granular timeouts (`connect_timeout_s`,
`read_timeout_s`, `pool_timeout_s`) and an explicit pool size
(`max_connections`, `max_keepalive_connections`), so a long-lived companion process
reuses keep-alive connections instead of opening a socket per turn.

`stream=True` switches the request to SSE: deltas are aggregated into the same final
JSON string that is validated, and an optional `on_delta` callback (sync or async)
receives each fragment for progressive rendering. Streaming does not weaken validation
and does not change the retry/circuit behavior.

HTTP status codes are converted to typed errors (`ProviderRateLimitError`,
`ProviderAuthError`, `ProviderServerError`, `ProviderRequestError`) carrying the status
and optional `Retry-After`. Response bodies are never placed in the exception message.

Token totals reported by the provider are accumulated in `usage_snapshot()` for cost and
latency accounting. Only counts and the reported model id are retained.

## Thinking mode

`disable_thinking=True` adds `{"thinking": {"type": "disabled"}}` to the request body
(the Volcano Ark / Doubao convention) and `extra_body` merges any additional
provider-specific field without touching the engine's own parameters. The drill verifies
the effect only when the provider reports `completion_tokens_details.reasoning_tokens`;
if that field is absent the drill says so instead of claiming verification.

## The acceptance drill

`continuity_engine.validation.provider_drill` runs seven scenario groups against an
injected `DrillEnvironment` (the host supplies the real adapter):

1. **Output contracts** — per-purpose probes (`demand_understanding`, `simple_response`,
   `complex_dialogue`, `task_plan`, `multi_step_plan`, `memory_consolidation`) plus a
   provider-metadata probe. Each probe takes up to `compat_attempts` independent samples
   and reports the observed violation rate, because a single sample failing a documented
   bound is probabilistic model behavior rather than a deterministic incompatibility.
2. **Long conversation** — 50-100 turns through `CompanionService`, including an
   explicit memory write and two long-range probes (memory recall and context
   continuation) plus a guard that the model does not invent live data (weather) it does
   not have.
3. **Transport drills** — controlled retry across rate-limit/server/connect faults,
   non-retryable auth failure, no-replay suppression, circuit open + recovery, and a
   bounded client deadline followed by a healthy call.
4. **Cancellation** — an in-flight `asyncio` cancellation and a control-lane `停止`
   interruption, checking that no task leaks and the in-flight turn is invalidated.
5. **Restart** — sessions are closed and recreated over the same SQLite memory and
   transcript stores; memory recall and restored history are verified in the new session.
6. **Outage and recovery** — a sustained outage on the companion path must degrade
   honestly (failure or a clearly disclosed local fallback, never a silent fabricated
   answer), and service must resume once the provider returns.
7. **Resources** — RSS (via `psutil`, `resource`, or the Windows process API),
   `tracemalloc` Python memory, thread count and asyncio task count are sampled
   throughout.

Reports are written as JSON and Markdown. They contain statuses, timings, counters and
boolean expectation results only — never prompts, responses, audio or credentials.

### Scenario ordering matters

The cancellation scenario runs last. `USER_STOP` is a sticky engine latch: running it
earlier would silence every later scenario on the same session. This is correct engine
behavior (a stop must not be silently forgotten), so the harness adapts to it rather
than clearing the latch.

## Running it

```bash
# PowerShell
$env:CONTINUITY_LLM_ENDPOINT = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
$env:CONTINUITY_LLM_MODEL    = "doubao-seed-2-1-pro-260915"
$env:CONTINUITY_LLM_API_KEY  = "<your-key>"

python examples/real_provider_drill.py --turns 60 --disable-thinking --enable-reliability \
    --out data/drill_reports/ark-drill
```

The process exits non-zero if any verdict failed. `--no-<scenario>` flags allow running a
subset. The drill makes real calls and can consume provider quota; it never controls an
actuator (the action executor stays simulated).

## Limitations

- Simulated faults are injected client-side by `FaultInjectingAdapter`. They exercise the
  real classification/retry/circuit code paths and then reach the live provider, but they
  are not a substitute for provoking a genuine vendor 429 or a real multi-hour outage.
- `Retry-After` handling is bounded to 600 s; longer or HTTP-date values are ignored.
- The circuit breaker is per adapter instance. Multiple processes or a restarted process
  do not share breaker state.
- Streaming aggregation requires the provider to emit OpenAI-style `choices[].delta.content`
  frames; other SSE dialects need a dedicated adapter.
- A passing drill does not certify conversational quality, safety, or long-duration
  stability. It is an acceptance gate for provider compatibility and failure handling.
- `field_validated`/`passed` in a report is evidence from one run on one host and one
  account, not a cryptographic proof or a vendor endorsement.
