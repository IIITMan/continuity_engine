# Test report — Continuity Engine 0.25.1-dev

## Reproducible checks

- Source-tree command: `PYTHONPATH=src python -m pytest -q`.
- Source-tree regression result on the Windows development host (2026-10-13): **347 passed, 2 failed, 1 skipped**.
  Both failures are pre-existing platform-specific tests (`osascript` path simulation; inline `python -c` stdin JSON
  encoding) that pass on the Linux container; the single skip is the Fernet test guarded by
  `pytest.importorskip("cryptography")`. The recorded Linux run reported **315 passed**.
- Focused context-budget tests: **14 passed**, including the new long-assistant-reply/context-continuation regressions.
- Compile check: `PYTHONPATH=src python -m compileall -q src examples tests`.
- Offline context fitter reproduction: with 700 estimated dialogue tokens, 220-token user-message ceiling and 120-token assistant-message ceiling, the earlier user-stated goal remains in the fitted context after several long assistant replies; fitted context remains within budget.
- The deterministic tests do not call the Volcano Ark endpoint. The real-provider 60-turn drill **has** been rerun on this build; see the live section below.
- Incidental: `src/continuity_engine/__init__.py` had been reverted to a version missing the `provider_errors`,
  `reliability` and `validation.provider_drill` `import *` lines and their `__all__` entries, which prevented
  `tests/test_provider_reliability.py` from being collected and broke `examples/real_provider_companion.py`
  (`CircuitBreaker` import). Restored to match the current code.

## Regression fixed

The user-provided pre-fix live drill recorded **16/17** verdicts. `context_continuation` failed after three attempts even though long-term memory recall remained correct. The offline root cause was confirmed in `DialogueContextBudgeter.fit_dialogue()`: an oversized older message caused an immediate `break`, so all messages older than it were discarded even when dialogue budget remained. Simply trimming that message was not sufficient when subsequent assistant responses consumed the whole budget.

The fix now:

- middle-trims oversized messages at their allowed per-message/remaining budget instead of breaking the scan;
- continues scanning older messages if even a trim marker cannot fit;
- applies an independent default 120-token estimated ceiling to assistant messages while user messages retain the 220-token ceiling;
- keeps the overall dialogue budget capped at 700 estimated tokens and the maximum recent-message count at 8.

The new tests reproduce a goal stated several messages earlier, followed by long assistant replies. They assert that the goal remains in `recent_dialogue` within the token budget.

## Provider-run data supplied for comparison (pre-fix build)

The reported baseline run at 2026-10-13 16:35:09 passed **17/17**. The pre-fix context-budget run at 18:03:02 passed **16/17**. Reported provider-billed input tokens changed from 111,493 to 92,465 (-17.1%); P50 latency from 6.590 s to 5.171 s; P95 from 14.436 s to 11.193 s. These values are from the user-provided live report, not measurements made by this local test run. The report itself notes that latency/output-token comparisons have n=1 per build and cannot be separated from provider variance.

## Live re-run on the patched build (2026-10-13 18:46:57)

The same 60-turn drill, same command, endpoint, model, account, host and conversation fixtures, run on the
v0.25.1.dev0 patched build:

- **17/17 verdicts**; `context_continuation` restored to **PASS** on the first attempt (4.20 s, 64 chars).
- Provider-billed input tokens **94,282** (68/69/69 responses counted) — **−15.4%** versus the no-budget
  baseline and only **+2.0%** versus the regressed build, so the fix is cheap.
- Output tokens **8,766** (−13.9% vs baseline). P50 **5.883 s** (−10.7%), P95 **12.150 s** (−15.8%),
  mean 6.478 s, max 14.889 s.
- Memory recall correct on the first attempt in both the mid-run probe and after a full session restart.
- Hard failures **0**, disclosed degradations **0**, error rate **0.0**, turn pass rate **60/60**.
- All five transport drills, cancellation, control-lane interruption, restart and outage-recovery checks pass;
  `reasoning_tokens=0` confirms thinking stayed disabled; 7/7 output contracts with 0 violations.
- Resources: RSS 38.0–78.3 MB (mean 66.7 MB), Python heap peak 8.8 MB, 1 thread, asyncio tasks back to 1.
- Per-turn estimated variable context: P50 523 / P95 632 (the estimator is not a provider tokenizer).

Latency and output-token figures remain n=1 per build on an endpoint whose latency spans 1.26–15.26 s on a
fixed prompt, so they are directionally consistent but not attributed to the change alone. The input-token
reduction and the `context_continuation` recovery are the two results with a mechanism behind them.

Full analysis: [`PROVIDER_DRILL_REPORT_EN.md`](PROVIDER_DRILL_REPORT_EN.md) and
[`PROVIDER_DRILL_REPORT_CN.md`](PROVIDER_DRILL_REPORT_CN.md).

## Historical and environment note

Earlier versions of this report documented **312 passed** for v0.25.0-dev before three new context regressions were added. This version supersedes that count for the v0.25.1-dev source tree. Provider acceptance is separate from the unit-test suite; live ASR/TTS hardware and physical actuators are not tested here.
