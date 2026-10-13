# Real-provider acceptance drill — Volcano Ark (火山方舟) / Doubao

**Result: 17 / 17 verdicts PASS.**

> **Superseded in part by the context-budget re-run.** A later re-run of the same 60-turn
> drill on the v0.25.0-dev build *with* the context budget scored **16/17**: token and latency
> improved (input tokens −17.1%, P50 −21.5%) but `context_continuation` regressed because the
> dialogue budget discarded context older than the last 1–2 messages. That regression was
> **fixed** in v0.25.1.dev0 (assistant-message cap + trim-and-continue scanning) and the
> re-run scored **17/17** with input tokens still 15.4% below this baseline. See
> [`PROVIDER_DRILL_REPORT_EN.md`](PROVIDER_DRILL_REPORT_EN.md) /
> [`PROVIDER_DRILL_REPORT_CN.md`](PROVIDER_DRILL_REPORT_CN.md). The figures below remain valid
> evidence for the pre-context-budget build on this host/account/date.

This report records an explicitly opt-in live acceptance run of the Continuity Engine
against a real commercial model endpoint. It was produced by
`python examples/real_provider_drill.py`, not by the automated test suite. Raw prompts and
raw model responses are not stored anywhere in the report; only statuses, timings, counters
and boolean expectation results are.

---

## 1. Run identity

| field | value |
| --- | --- |
| vendor | Volcano Ark (火山方舟) |
| endpoint host | `ark.cn-beijing.volces.com` |
| endpoint path | `/api/v3/chat/completions` |
| model (requested and reported by the provider) | `doubao-seed-2-1-pro-260915` |
| started | 2026-10-13 16:35:09 (local host time) |
| duration | 512.6 s |
| thinking mode | disabled (`thinking: {"type": "disabled"}`), verified via `reasoning_tokens == 0` |
| streaming | off (aggregated SSE is covered by deterministic tests) |
| reliability layer | enabled |
| retry policy | `max_attempts=3`, `base_delay_s=0.4`, `max_delay_s=4.0`, `max_total_retry_wait_s=10.0`, `jitter_ratio=0.25`, honors `Retry-After` |
| circuit breaker | `failure_threshold=3`, `recovery_timeout_s=2.0` |
| gateway deadline / concurrency | 45 s / 2 |
| action executor | simulated (no actuator was controlled) |
| transcript persistence | enabled, local plaintext SQLite (opt-in) |
| preflight readiness | `COMPLETED` in 1.68 s |
| verdict | **PASS** (all 17 checks) |

Reproduction:

```powershell
$env:CONTINUITY_LLM_ENDPOINT = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
$env:CONTINUITY_LLM_MODEL    = "doubao-seed-2-1-pro-260915"
$env:CONTINUITY_LLM_API_KEY  = "<your-key>"
python examples/real_provider_drill.py --turns 60 --disable-thinking --enable-reliability `
    --data-dir data/ark --out data/drill_reports/ark-drill
```

Machine-readable artefacts for this run: `data/drill_reports/ark-drill.json` and
`data/drill_reports/ark-drill.md`.

---

## 2. Verdicts

| # | check | result |
| --- | --- | --- |
| 1 | `output_contract_all_purposes` | PASS |
| 2 | `thinking_mode_disabled` | PASS |
| 3 | `long_conversation_no_hard_failures` | PASS |
| 4 | `memory_recall_across_turns` | PASS |
| 5 | `context_continuation` | PASS |
| 6 | `no_fabrication_without_live_data` | PASS |
| 7 | `controlled_retry_recovers_from_transient_faults` | PASS |
| 8 | `authentication_failure_is_not_retried` | PASS |
| 9 | `side_effecting_request_is_never_replayed` | PASS |
| 10 | `circuit_opens_during_outage_and_recovers` | PASS |
| 11 | `client_deadline_is_bounded_and_client_still_usable` | PASS |
| 12 | `cancellation_no_leaked_tasks` | PASS |
| 13 | `cancellation_was_inflight` | PASS |
| 14 | `control_lane_interrupts_inflight` | PASS |
| 15 | `restart_restores_context` | PASS |
| 16 | `outage_failures_were_honest` | PASS |
| 17 | `service_recovers_after_outage` | PASS |

---

## 3. Output-contract compatibility

Every purpose was probed through the engine's own state-view builder, i.e. the exact JSON
contract used in production. `violations` counts samples that failed the strict validator.

| purpose | status | contract_ok | samples | violations | latency (s) |
| --- | --- | --- | --- | --- | --- |
| `demand_understanding` | COMPLETED | yes | 1 | 0 | 4.30 |
| `simple_response` | COMPLETED | yes | 1 | 0 | 3.79 |
| `complex_dialogue` | COMPLETED | yes | 1 | 0 | 5.55 |
| `task_plan` | COMPLETED | yes | 1 | 0 | 2.58 |
| `multi_step_plan` | COMPLETED | yes | 1 | 0 | 6.86 |
| `memory_consolidation` | COMPLETED | yes | 1 | 0 | 1.74 |
| `provider_metadata` | COMPLETED | yes | 1 | 0 | 2.43 |

Provider metadata: `model=doubao-seed-2-1-pro-260915`, `prompt_tokens=370`,
`completion_tokens=49`, **`reasoning_tokens=0`** → thinking mode confirmed disabled by
token accounting rather than by assumption.

### Finding F1 — the planner violated a documented numeric bound (fixed)

The first full run failed this verdict: `task_plan` produced **3 violations in 3 samples**
with `INVALID_TASK_ACTION_ARGS`. The cause was precise and reproducible — for the goal
"remind the user to stand up in ten minutes" the model emitted a single step
`WAIT {"duration_s": 600}` while `action_argument_schemas` supplied in the same request
declared `minimum: 0, maximum: 300`. The engine correctly rejected the out-of-range value
(fail-closed), which is the desired behaviour; the defect was that the prompt did not make
the bound actionable.

Fix applied: the `task_plan` system prompt now states that numeric minimum/maximum values
are hard bounds and that a longer duration must be expressed as several steps of the same
action type instead of one out-of-range value. After the change `task_plan` passed with
1 sample / 0 violations. The pre-fix evidence is preserved in
`data/drill_reports/ark-drill-run1-before-prompt-fix.json`.

### Finding F2 — `multi_step_plan` type ambiguity (fixed)

An early probe showed the model emitting `preconditions` as a string (`"无"`) where the
validator requires a list of strings. The prompt now states the required types explicitly.
After the change `multi_step_plan` passed and the strict validator was left unchanged.

---

## 4. Long conversation (60 turns)

| metric | value |
| --- | --- |
| turns | 60 |
| COMPLETED | 59 |
| MEMORY_SAVED (explicit "remember this" turn, no model call needed) | 1 |
| disclosed degraded fallbacks | 0 |
| hard failures (no answer at all) | 0 |
| error rate | 0.0 |
| latency min / p50 / p95 / max (s) | 0.004 / **6.59** / **14.44** / **17.56** |
| latency mean (s) | 7.54 |
| wall duration (s) | 434.6 |
| memory recall across turns | verified |
| context continuation across turns | verified |
| no fabrication without live data | verified |

The authored script alternates small-talk, emotional sharing, a multi-clause request, a
request for live weather data the engine cannot supply, reflection, and advice turns, with
two long-range probes injected into the middle of the run:

| probe | turn | attempts | status | latency (s) | response chars | result |
| --- | --- | --- | --- | --- | --- | --- |
| memory recall (a fact written at turn 1, recalled at turn 5) | 4 | 1 | COMPLETED | 2.13 | 7 | contains the stored token |
| context continuation (a goal stated at turn 10, asked about at turn 13) | 12 | 1 | COMPLETED | 3.85 | 82 | contains the stated token |

Neither probe needed its retry budget, which confirms that both memory recall and
conversational context survived the intervening turns.

The `no_fabrication_without_live_data` check passed: asked whether tomorrow is good for a
run, the model did not invent a temperature or forecast wording, because no weather fact
was present in the vetted world summary.

**Latency note.** `doubao-seed-2-1-pro-260915` answered with a p50 of 6.59 s per turn on
this host. A direct 4-sample probe of the same endpoint with a minimal prompt measured
0.99-1.39 s, so the turn latency is dominated by longer prompts (rolling dialogue context
plus accumulated memory) and by model-side generation, not by connection setup. The
reliability layer's own per-attempt overhead is 1-2 ms (see the fast-fail measurement in
§5, drill 4).

### 4.1 Focused single-request latency probe (thinking disabled)

To separate model/network latency from the conversation loop, 20 sequential requests were
measured with the reliability layer **disabled** (so nothing is retried away) and thinking
disabled, using the same `simple_response` prompt shape as production
(`examples/provider_latency_probe.py`). Prompt size was identical for every request
(409 tokens), so the measurement is controlled.

| statistic | value (s) |
| --- | --- |
| min | 1.262 |
| **p50** | **1.848** |
| mean | 3.045 |
| p90 | 5.564 |
| p95 | 8.427 |
| max | 15.259 |
| stdev | 3.333 |

| | value |
| --- | --- |
| requests / succeeded / failed | 20 / 20 / 0 |
| error rate | 0.0 |
| `reasoning_tokens` across all 20 requests | **0** (thinking genuinely off) |
| completion tokens (total / mean / max) | 599 / 30.0 / 46 |
| aggregate output throughput | 9.84 tok/s |

Interpretation:

- The **median is ~1.85 s** and the **minimum is 1.26 s** for a 409-token prompt with a
  ~30-token answer. This is the honest steady-state figure; it is well inside the 45 s
  gateway deadline.
- The **tail is heavy and provider-side**: 3 of 20 requests took 5.56 s, 8.43 s and
  15.26 s. The request payload and output size were unchanged in those calls, no client
  retry or timeout was involved, and a warm-up call had already established the TLS session
  and keep-alive connection. So the stalls come from the endpoint/network path, not from the
  engine. Two consecutive 10-request batches differed sharply (batch 1: max 5.56 s;
  batch 2: max 15.26 s, with 8.43 s as well), which is consistent with the connectivity
  trouble documented in §12.
- The practical consequence is the same one the drills test for: a p50 under 2 s does not
  mean a turn is guaranteed to finish in 2 s. A 45 s deadline absorbs even the worst
  observed stall, the circuit breaker prevents a sustained degradation from being amplified,
  and a genuinely stuck request is bounded by the deadline rather than hanging the turn.

Artefacts: `data/drill_reports/latency-thinking-disabled.json`,
`data/drill_reports/latency-thinking-disabled-batch2.json`,
`data/drill_reports/latency-thinking-disabled-summary.json`.

---

## 5. Failure drills

| # | drill | result | observed |
| --- | --- | --- | --- |
| 1 | controlled retry across `RATE_LIMIT(retry_after=1s)` → `SERVER(503)` → `CONNECT` | PASS | 4 attempts, 3 retries, `retries_by_kind={RATE_LIMIT:1, SERVER:1, CONNECT:1}`, total retry wait 3.59 s (including the honored 1.0 s `Retry-After`), final call succeeded in 5.68 s |
| 2 | authentication failure is not retried | PASS | injected `AUTH(401)` → `ProviderAuthError`, 1 attempt, **0 retries**, no fabricated success |
| 3 | side-effecting request is never replayed | PASS | `replay_safe=False` + injected `RATE_LIMIT(0.2s)` → 1 attempt, **0 retries**, `retries_suppressed_no_replay=1` |
| 4 | circuit opens during outage and recovers | PASS | after 3 failures state `OPEN`; refusal latency **1.2 ms** vs 164 ms for a real network failure (136× cheaper); 1 rejection counted; after the 2.06 s recovery window a half-open probe succeeded in 1.91 s and the state returned to `CLOSED` |
| 5 | client deadline is bounded and the client stays usable | PASS | deadline 1.0 s → timeout raised at **1.004 s**, and the immediately following call to the live provider succeeded in 1.34 s |

Drill 3 is the acceptance criterion for "retries must consider whether a request has
already produced an external side effect": a request marked `replay_safe=False` is executed
exactly once and never replayed, even though the policy would normally retry a rate limit.
The suppression is counted so the host can reconcile.

Drill 4 shows both halves of the recovery story: the breaker converts an outage into
millisecond fast-fails (136× cheaper than a real network failure) instead of hammering the
provider, and it re-probes and closes automatically once the provider returns.

---

## 6. Cancellation and interruption

A slow fault was injected so that both requests were provably still in flight when the
drill interrupted them.

| observation | value |
| --- | --- |
| request in flight when cancelled | yes |
| request raised | `CancelledError` |
| control request in flight when STOP was injected | yes |
| control-lane result | `CONTROL_ACCEPTED` |
| in-flight turn outcome after STOP | `STALE` (the live answer was correctly invalidated) |
| asyncio tasks before / after cancel / final | 2 / 2 / 2 |
| leaked tasks | none |

Both interruption paths behave as specified: an explicit cancellation cancels the
in-flight request, and a hard `停止` control invalidates the generation so the older answer
can never resurface.

---

## 7. Restart and recovery

| observation | value |
| --- | --- |
| turns persisted before close | 4 |
| turns available in the fresh session | 6 |
| recall of the pre-restart fact | verified (1 attempt) |
| status after restart | `COMPLETED` |

The session (engine, provider adapter, SQLite memory repository, SQLite transcript store)
was closed and rebuilt from scratch. The new process-equivalent session reused the
persisted memory and transcript, recalled a fact written before the restart, and continued
serving turns.

---

## 8. Provider outage and recovery

| observation | value |
| --- | --- |
| injected outage turns | 2 |
| statuses during the outage | `DEGRADED`, `DEGRADED` (explicitly disclosed local fallback) |
| failures were honest (no silent fabricated answer) | yes |
| wait for the circuit recovery window | 2.07 s |
| recovery attempts | 1 |
| status after recovery | `COMPLETED` in 1.73 s |

During the outage the engine never claimed live access. It surfaced an explicitly
disclosed fallback, kept the circuit open, and resumed normal service as soon as the
provider answered again.

---

## 9. Reliability counters for the run

Session gateway:

```json
{"requests": 70, "attempts": 71, "successes": 68, "failures": 3, "retries": 2,
 "retries_suppressed_no_replay": 0, "retry_wait_s": 1.367,
 "circuit_rejections": 1, "failures_by_kind": {"SERVER": 3}, "retries_by_kind": {"SERVER": 2}}
```

All 3 `SERVER` failures and the single circuit rejection come from the deliberately
injected outage drill in §8; the live provider itself produced no failures during this
run. Final circuit state: `CLOSED`, 0 consecutive failures.

Isolated transport drills (separate adapter instances):

```json
{"requests": 10, "attempts": 12, "successes": 3, "failures": 8,
 "retries": 3, "retries_suppressed_no_replay": 1, "retry_after_observed_s": 1.0,
 "failures_by_kind": {"RATE_LIMIT": 2, "SERVER": 1, "CONNECT": 4, "AUTH": 1}}
```

Companion-level metrics (no raw text stored):

```json
{"conversation_count": 64, "statuses": {"COMPLETED": 61, "MEMORY_SAVED": 1, "DEGRADED": 2},
 "errors": {"DEGRADED_LOCAL_FALLBACK": 2}, "latency_p50_s": 6.41, "latency_p95_s": 14.44}
```

---

## 10. Provider token usage

| metric | value |
| --- | --- |
| billed responses | 68 |
| prompt tokens | 111,493 |
| completion tokens | 10,187 |
| total tokens | 121,680 |
| reasoning tokens | **0** (confirms thinking disabled) |

Prompt tokens dominate because every turn carries the system contract plus rolling
dialogue context. Only counts are retained; no prompt or completion text is stored.

---

## 11. Resource usage

1,922 samples over the run (RSS via the Windows process API, Python memory via
`tracemalloc`, threads, asyncio tasks):

| metric | min | mean | max |
| --- | --- | --- | --- |
| process RSS | 37.4 MB | 66.0 MB | 78.2 MB |
| Python-traced memory | 9.9 KB | 8.0 MB | 8.9 MB (peak 9.0 MB) |
| threads | 1 | 1.0 | 1 |
| asyncio tasks | 1 | 2.96 | 4 |

The service is single-threaded, stays under ~80 MB RSS, and its asyncio task count returns
to 1 after every scenario, which is consistent with the "no leaked tasks" cancellation
finding. Python heap growth is bounded (peak ≈ 9 MB), so a 60-turn conversation plus the
persisted SQLite stores did not accumulate unbounded in-process state.

---

## 12. Behaviour under a real provider outage (observed, not injected)

Two earlier runs in the same session happened to coincide with genuine connectivity
trouble between this host and `ark.cn-beijing.volces.com`. They are reported here because
they are stronger evidence than any simulation.

| run | started | duration | live transport failures | circuit | conversation outcome | verdicts failed |
| --- | --- | --- | --- | --- | --- | --- |
| 2 | 16:12:18 | 492.5 s | `UNKNOWN:1, CONNECT:3, SERVER:1` | opened 3×, 4 rejections | 57 COMPLETED, 2 disclosed fallbacks | 4 (see below) |
| 3 | 16:27:27 | 82.1 s | `CONNECT:5, SERVER:1` | opened 4×, **59 rejections** | 4 COMPLETED, 55 disclosed fallbacks, **0 hard failures** | 2 |
| 4 | 16:35:09 | 512.6 s | none | recovered, `CLOSED` | 59 COMPLETED, 0 fallbacks | none |

What run 3 demonstrates:

- **No fabrication under sustained outage.** All 55 affected turns returned an explicitly
  disclosed local fallback. `long_conversation_no_hard_failures` still passed, because no
  turn failed to produce an answer — the conversation degraded rather than broke.
- **The breaker protected the provider and the host.** 59 additional turns were answered
  from the local circuit refusal at ~27 ms p50 instead of issuing 59 more requests into a
  broken connection. Total run time collapsed from ~500 s to 82 s.
- **Recovery is automatic.** Circuit state returned to `CLOSED` once connectivity was
  restored (verified by a direct 4-sample probe: 200 OK at 0.99-1.39 s before run 4).

Run 3's two failing verdicts (`context_continuation`, `service_recovers_after_outage`) are
attributable to provider unavailability rather than engine behaviour, and the harness now
records that distinction explicitly: a `provider was not reachable during preflight` /
`provider was still not answering after the injected outage ended` note is written into the
report, labelled conversation probes get a bounded retry budget, and the outage-recovery
check retries before deciding. Run 2 also exposed that a disclosed fallback was being
counted as a conversation hard failure; that definition was corrected.

One classification improvement came out of run 2: a single live failure was classified
`UNKNOWN` and therefore not retried. Truncated reads, broken streams and protocol errors
are now classified `CONNECT` (bounded-retry eligible) instead of failing closed.

---

## 13. What this run does and does not prove

**It does prove.** For this model, this endpoint, this account and this host, on
2026-10-13:

- the engine's per-purpose JSON contracts are satisfied in production form;
- a 60-turn conversation holds memory recall and context continuation with zero hard
  failures and zero fabricated live facts;
- thinking mode is genuinely disabled (`reasoning_tokens == 0`);
- transient rate-limit, 5xx and connection faults are recovered by controlled retry with
  the server's `Retry-After` honored;
- an authentication failure is **not** retried, and a request that could cause an external
  side effect is **never** replayed;
- a circuit breaker opens during an outage, fast-fails 136× cheaper than a live network
  failure, and closes itself after a successful half-open probe;
- a hard client deadline is respected to the millisecond and does not poison the client;
- cancellation and control-lane `停止` interrupt in-flight work with no leaked tasks;
- the service continues correctly after a full close/rebuild over persisted stores;
- and, when the real network failed for an extended period, the companion degraded
  honestly, protected the provider, and recovered on its own.

**It does not prove.**

- vendor-certified compatibility in general: numbers are specific to one model id and
  account, and a different model or region would need its own run;
- conversational quality, safety or long-duration stability — the authored checks are
  observable smoke assertions (contains / must-not-contain / minimum length), not a
  quality metric;
- throughput or cost efficiency: the measured p50 of 6.59 s per turn is specific to this
  model and this host's network path;
- that simulated faults equal real ones: `FaultInjectingAdapter` reproduces the code paths
  deterministically and then reaches the live provider, but it is not a synthetic vendor
  throttle;
- any physical outcome: the action executor stayed simulated, and no actuator was
  controlled;
- that a passing report is a certificate. It is evidence from one run, and the JSON/Markdown
  reports are generated artefacts rather than attestations.

---

## 14. Artefacts

| file | contents |
| --- | --- |
| `data/drill_reports/ark-drill.json` | machine-readable report of the passing run (§1-§11) |
| `data/drill_reports/ark-drill.md` | the same report rendered as Markdown |
| `data/drill_reports/ark-drill-run1-before-prompt-fix.json` | pre-fix evidence for Finding F1 |
| `data/drill_reports/ark-drill-run2.json` | real provider incident, 4 verdicts failed |
| `data/drill_reports/ark-drill-run3.json` | real sustained outage, honest degradation |
| `data/drill_reports/latency-thinking-disabled.json` | focused latency probe, batch 1 (10 requests) |
| `data/drill_reports/latency-thinking-disabled-batch2.json` | focused latency probe, batch 2 (10 requests) |
| `data/drill_reports/latency-thinking-disabled-summary.json` | pooled 20-request latency summary (§4.1) |
| `data/ark/*.sqlite3` | local memory and transcript stores used by the run (plaintext) |

All report files contain statuses, timings, counters and boolean results only; no prompt,
response, audio or credential content is included. The API key was supplied through the
environment (`CONTINUITY_LLM_API_KEY`) and is not present in any artefact or in source
control.
