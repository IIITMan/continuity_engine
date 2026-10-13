# Implementation status — v0.25.1-dev

This patch responds to the user-provided Volcano Ark/Doubao acceptance report: a context-budget build passed 16/17 verdicts because `context_continuation` failed after long assistant responses caused older user context to disappear. The fix is implemented, covered by deterministic regression tests, and **verified by a live 60-turn re-run of the same drill (17/17, `context_continuation` restored to PASS)** — see [`PROVIDER_DRILL_REPORT_EN.md`](PROVIDER_DRILL_REPORT_EN.md) / [`PROVIDER_DRILL_REPORT_CN.md`](PROVIDER_DRILL_REPORT_CN.md).

- `DialogueContextBudgeter` now middle-trims over-budget older messages instead of breaking the scan.
- Assistant messages have a separate default 120-estimated-token ceiling, while user messages retain a 220-token ceiling. This saves room for user-stated goals/preferences under the shared 700-token dialogue budget.
- If a trimmed message still cannot fit the remaining budget, it is skipped without terminating the scan; truncation metrics count only messages that remain in the selected context.
- Added regressions for a multi-turn goal surviving after several long assistant responses, for older short messages behind oversized messages, and for the overall context ceiling.
- Prior v0.25 optimization remains: current user input is not duplicated in `recent_dialogue`; memory retrieval work is bounded; internal retrieval scores are excluded from strict model context.

## Current verification

- Source suite on the Windows development host: **347 passed, 2 failed, 1 skipped**. Both failures are
  pre-existing platform-specific tests (`osascript` path simulation; inline `python -c` stdin JSON
  encoding) that pass on the Linux container; the single skip is the Fernet test guarded by
  `pytest.importorskip("cryptography")`.
- Focused optimization tests: **14 passed**.
- `compileall` and offline context-budget benchmark passed.
- Deterministic reproduction of the reported continuation regression passes: the earlier user-stated goal remains in the fitted context with the context estimate at or below 700.
- **Live re-run on the patched build** (2026-10-13 18:46, same endpoint/model/account/host/command as the
  pre-fix run): **17/17 verdicts**, `context_continuation` PASS on the first attempt. Provider-billed input
  tokens 111,493 (pre-fix baseline) → 92,465 (budget, regressed) → **94,282** (fixed), i.e. **−15.4%** versus
  the no-budget baseline and only +2.0% versus the regressed build. P50 6.590 → 5.171 → **5.883 s**
  (−10.7% vs baseline), P95 14.436 → 11.193 → **12.150 s** (−15.8%). Output tokens 10,187 → 7,529 → 8,766.
  Memory recall correct on the first attempt in both the mid-run and post-restart probes; hard failures 0,
  disclosed degradations 0, error rate 0.0; all five transport drills, cancellation, restart and
  outage-recovery checks pass; `reasoning_tokens=0` confirms thinking stayed disabled.

## Still pending

- Run at least three independent live runs per build before drawing conclusions about latency or output-token improvements; the figures above are one run per build on an endpoint whose latency spans 1.26–15.26 s on a fixed prompt.
- Continue reporting provider-billed tokens separately from the local context estimator (per-turn estimated variable context was P50 523 / P95 632 on the fixed build).
- Real microphone, speaker, actuator and target-hardware validation remain outside the local test suite.

---

# Implementation status — v0.9.0-dev

Local verification on the development host: **222 automated tests passed**, `compileall` passed, the request-router demo completed, the v0.9 capabilities demo help/smoke path completed, and the built wheel was installed into an isolated target directory with the same 222-test suite passing.

## Implemented in v0.9

- Persistent SQLite reminder provider with an async background due-time worker and timezone-aware Chinese/ISO time parsing.
- Real reminder operations wired through RequestRouter: create, list and uniquely matched cancellation.
- Platform desktop notification delivery via the existing native driver. The provider stays unavailable if its OS delivery backend fails startup health checks.
- Conservative notification semantics: a due reminder is claimed before the external side effect; successful delivery becomes `FIRED`; definite delivery error becomes `FAILED`; unknown outcome or a crash during `FIRING` becomes `UNKNOWN` rather than automatic redelivery.
- Open-Meteo current condition and forecast provider, including geocoding, source/timestamp metadata, ambiguity handling, timeout and fail-closed result states.
- Wikipedia/MediaWiki search provider returning bounded plain-text extracts and canonical URLs, with bounded TTL cache and explicit errors.
- `build_v09_provider_bundle()` to wire providers to configuration; network services remain disabled by default and require explicit flags.
- Interactive `examples/v09_capabilities_demo.py` showing reminder lifecycle and opt-in weather/knowledge network capabilities.
- Integration tests use an injected fake notification delivery and `httpx.MockTransport`, not public APIs or real desktop notifications.

## Verified locally

- Full suite: 222 passed.
- Wheel: built as `continuity_engine-0.9.0.dev0-py3-none-any.whl`; installed into a separate target directory; full suite passed using the installed package.
- In this headless Linux container, native notification health check reports unavailable. The interactive demo correctly logs a startup error and does not claim reminder capability is active. Actual desktop notification delivery still needs smoke testing on the target Windows/desktop host.
- No live weather/Wikipedia HTTP call was made.

## Known limits

- Exactly-once delivery across process crashes cannot be guaranteed for OS notifications. Records found in `FIRING` become `UNKNOWN` and require reconciliation.
- Current reminder backend uses desktop notifications, not mandatory voice. Voice delivery can be added via a separately configured output provider if desired.
- Open-Meteo/Wikipedia adapters are implemented but live endpoint status, commercial terms, attribution and rate limits must be reviewed before deployment.
- Knowledge queries currently return source-backed extracts and URLs. LLM summarization of retrieval evidence can be layered above the provider, but the provider itself does not fabricate a synthesis.
- Camera, microphone capture, mobile base, timer, media player and smart-home control still require real registered providers and relevant safety guards.


## Historical development status — v0.16.0-dev

The latest development line includes LIFE_LAYER organs, additive state snapshot persistence, bounded SQLite memory search/update helpers, confidence-gated transient affect updates, an opt-in semantic body-driver contract, expression formatting, and a disclosed local dialogue fallback. LIFE_LAYER never authorizes actions directly; existing intent and safety policy remain authoritative. The current suite is deterministic and runs without a live LLM/network/sensor/physical actuator. See `docs/LIFE_LAYER.md` for module contracts and known limitations.

Known integration boundary: applications still need to attach a persistent memory repository explicitly and decide which events become memories. The current project does not provide a production hardware BodyDriver, microphone/camera capture, calibrated emotion classification, or a general-purpose autonomous companion service.
