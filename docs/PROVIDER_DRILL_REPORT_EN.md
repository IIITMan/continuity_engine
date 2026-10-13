# Volcano Ark 60-Turn Acceptance Re-Run (v0.25.1.dev0, fixed build) — English

**Verdict: `context_continuation` is back to PASS. Input tokens are still 15.4% below the no-budget baseline, P50/P95 latency are 10.7%/15.8% lower, and memory recall plus failure rate are all unchanged. 17/17 verdicts PASS.**

This report records a re-run of the same 60-turn acceptance drill on Volcano Ark after the context-budget regression was fixed, compared against the two previous builds. Every number comes from a real provider call; nothing is substituted by an estimate. Only statuses, timings, counters and boolean results appear here — no prompt or response text.

---

## 1. Identity of the three runs

| field | A baseline (no budget) | B budget (regressed) | C fixed |
| --- | --- | --- | --- |
| run id | `b31cfa24…` | `c11a70ed…` | `c7cdb9a9…` |
| started | 16:35:09 | 18:03:02 | **18:46:57** |
| build | v0.25.0-dev (reliability layer, no context budget) | v0.25.0-dev (+ context budget, regressed) | **v0.25.1.dev0 (+ fix)** |
| command | identical across all three | `--turns 60 --disable-thinking --enable-reliability` | same |
| endpoint / model | `ark.cn-beijing.volces.com` / `doubao-seed-2-1-pro-260915` | same | same |
| preflight | 1.68 s | 2.28 s | ready |
| wall duration | 512.6 s | 418.3 s | 440.3 s |
| verdict | 17/17 | **16/17** | **17/17** |

> On the "v0.24 baseline": this repository has no live v0.24 drill data. A is the previous
> v0.25 build's recorded run (reliability layer, no context budget), so the comparison
> measures the net effect of the context-budget change.

---

## 2. Is the regression fixed? **Yes**

| probe | A baseline | B regressed | **C fixed** |
| --- | --- | --- | --- |
| long-range context continuation (goal stated at turn 10, asked at turn 13) | correct, 1 attempt | **failed, 3 attempts** | **correct, 1 attempt (4.20 s, 64 chars)** |

`context_continuation` recovered from B's failure to **PASS**, and it needed only one
attempt — matching the A baseline's behaviour.

### The fix

`DialogueContextBudgeter` changed in two targeted ways:

1. **Assistant messages get a tighter cap** (`max_assistant_message_tokens=120`, user messages
   stay at 220). Long assistant prose no longer consumes the whole rolling window, leaving
   room for user statements, which are the primary source of conversational facts.
2. **Oversized messages are middle-trimmed and the backward scan continues**, instead of
   `break`ing and discarding everything older. If a message still does not fit after
   trimming, it is skipped (`continue`) so an older short fact can still be retained.

Offline verification using the reply lengths actually recorded by the drill (character
counts only, no text):

```
before (B): 10 messages in -> only 2 kept, marathon statement discarded = False
after  (C): 10 messages in -> 8 kept, 4 assistant messages trimmed (~119 tokens each),
             marathon statement retained = True
```

`CompanionMetrics` corroborates this across the runs: estimated variable context per turn
was P50 489 / P95 730 in B, and **P50 523 / P95 632 in C** — more messages retained, with a
tighter distribution because assistant messages are trimmed to a stable ~120 tokens.

---

## 3. The five requested metrics

### 3.1 Input tokens (provider-billed)

| metric | A baseline | B regressed | **C fixed** | C vs A | C vs B |
| --- | --- | --- | --- | --- | --- |
| input tokens, total | 111,493 | 92,465 | **94,282** | **−15.4%** | +2.0% |
| input tokens per response | 1,639.6 | 1,340.1 | **1,366.4** | **−16.7%** | +2.0% |
| responses counted | 68 | 69 | 69 | — | — |

### 3.2 Output tokens (provider-billed)

| metric | A baseline | B regressed | **C fixed** | C vs A | C vs B |
| --- | --- | --- | --- | --- | --- |
| output tokens, total | 10,187 | 7,529 | **8,766** | **−13.9%** | +16.4% |
| output tokens per response | 149.8 | 109.1 | **127.0** | **−15.2%** | +16.4% |

### 3.3 Latency (end-to-end per turn)

| metric | A baseline | B regressed | **C fixed** | C vs A | C vs B |
| --- | --- | --- | --- | --- | --- |
| **P50** | 6.590 s | 5.171 s | **5.883 s** | **−10.7%** | +13.8% |
| **P95** | 14.436 s | 11.193 s | **12.150 s** | **−15.8%** | +8.5% |
| mean | 7.544 s | 5.742 s | 6.478 s | −14.1% | +12.8% |
| max | 17.558 s | 17.258 s | 14.889 s | −15.2% | −13.7% |

### 3.4 Memory recall correctness

| probe | A | B | **C** |
| --- | --- | --- | --- |
| long-term memory recall (stored turn 1, asked turn 5) | correct, 1 attempt | correct, 1 attempt | **correct, 1 attempt (2.78 s)** |
| recall after restart | correct, 1 attempt | correct, 1 attempt | **correct, 1 attempt** |
| no fabrication without live data | pass | pass | **pass** |

### 3.5 Failure rate

| metric | A | B | **C** |
| --- | --- | --- | --- |
| conversation hard failures | 0/60 | 0/60 | **0/60** |
| conversation disclosed degradations | 0/60 | 0/60 | **0/60** |
| conversation error rate | 0.0 | 0.0 | **0.0** |
| turn pass rate | 59/60 (one memory-write turn) | 58/60 | **60/60** |
| provider transport failures / retries / circuit rejections | 3 / 2 / 1 | 3 / 2 / 1 | **3 / 2 / 1** (all from the injected outage drill) |

---

## 4. Remaining verdicts (all PASS in C)

| group | result |
| --- | --- |
| output contract | 7/7 purposes `contract_ok=true`, **0 violations** |
| thinking mode | `reasoning_tokens=0`, confirmed by token accounting |
| transport drills | controlled retry / auth-not-retried / side-effecting-request-not-replayed / circuit recovery / deadline — 5/5 |
| cancellation and interruption | request confirmed in flight, `CancelledError`, `STALE` after `停止`, **no leaked tasks** |
| restart | recall correct and history restored (4 → 6 turns) |
| provider outage | 2 disclosed degradations (honest failure, no fabrication), then `COMPLETED` on the first attempt after recovery (1.59 s) |
| resources | RSS 38.0–78.3 MB (mean 66.7 MB), Python heap peak 8.8 MB, 1 thread, asyncio tasks back to 1 |

---

## 5. What can and cannot be concluded

**Attributable to this change (mechanism + billed tokens)**

- Input tokens are 15.4% below A, from (a) no longer duplicating the current user input
  inside `recent_dialogue` and (b) bounded dialogue/memory trimming. C costs only 2.0% more
  than B, so the fix is cheap: it re-admits messages that were previously discarded
  wholesale while keeping assistant messages trimmed to 120 tokens, so the total stays
  bounded.
- The `context_continuation` recovery is a deterministic result, corroborated three ways:
  offline reproduction, three new regression tests, and the live probe.

**Directionally consistent but not attributable (n=1)**

- P50 −10.7% / P95 −15.8% versus A. Shorter input points this way, but the endpoint's
  latency variance spans 1.26–15.26 s on a fixed prompt and each build was run once, so the
  latency improvement cannot be separated from provider-side variance. At least three runs
  per build would be needed.
- Output tokens −13.9% versus A is simultaneously a behavioural change (reply length) and is
  subject to the same n=1 limitation.

**Not regressed**: memory recall, failure rate, output contract, cancellation/interruption,
restart, outage recovery and resource usage are all equal to or better than A.

---

## 6. Tests

The original command `PYTHONPATH=src pytest -q --disable-warnings --maxfail=1` stopped on a
**pre-existing Windows platform test** (`test_macos_notification_quotes_strings_for_applescript`,
unrelated to this change). Running the suite to completion:

**347 passed / 2 failed / 1 skipped**

- Both failures are pre-existing Windows platform tests (`osascript` path simulation; inline
  `python -c` stdin JSON encoding) that pass on the Linux container used for earlier release
  reports.
- The single skip is the Fernet test guarded by `pytest.importorskip("cryptography")`.
- The three new regression tests all pass and map exactly onto this root cause:
  - `test_long_assistant_reply_does_not_drop_older_context_needed_for_continuation`
  - `test_over_budget_old_message_is_trimmed_and_scan_continues_to_older_short_fact`
  - `test_assistant_prose_cannot_evict_user_goal_from_recent_context`

> Incidental fix: `src/continuity_engine/__init__.py` had been reverted to an older version
> that was missing the `provider_errors` / `reliability` / `validation.provider_drill`
> `import *` lines and their `__all__` entries, which prevented
> `tests/test_provider_reliability.py` from being collected and broke
> `examples/real_provider_companion.py` (`CircuitBreaker` import). Those three imports and
> their exported names have been restored to match the current code.

---

## 7. Artefacts

| file | contents |
| --- | --- |
| `data/drill_reports/ark-drill.json` | A baseline (no context budget) |
| `data/drill_reports/ark-drill-v025-contextbudget.json` | B budget build (`context_continuation` failed) |
| `data/drill_reports/ark-drill-v0251-fixed.json` | **C fixed build (17/17)** |
| `data/drill_reports/ark-drill-v0251-fixed.md` | raw Markdown report of C |
| `examples/drill_compare.py` | comparison tool |

Chinese version: [`PROVIDER_DRILL_REPORT_CN.md`](PROVIDER_DRILL_REPORT_CN.md). Design and
limitations: [`PROVIDER_RELIABILITY.md`](PROVIDER_RELIABILITY.md). Context-budget design:
[`ACCEPTANCE_OPTIMIZATIONS.md`](ACCEPTANCE_OPTIMIZATIONS.md).
