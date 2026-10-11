# Implementation status — v0.2.0-dev

Current local verification: **65 automated tests passed**; `compileall`, end-to-end demo, and a no-build-isolation wheel build succeeded in the development container. See `TEST_REPORT.md` for exact commands and limitations.

This status distinguishes working local code from integration work that is not claimed to be complete.

## Implemented and covered by automated tests

- Event DTO validation and 0.9 → 1.0 migration; unknown schemas are quarantined.
- Event queue prioritization, coalescing, dedupe, explicit drops and protected-control overflow handling.
- Evidence-based world model, finite-value checks, TTL/stale semantics and conflict detection.
- First-order drive dynamics and prediction EMA.
- Intent switch margin, dwell time, repeated confirmation, urgent override and clarification round cap.
- Filtered LLM state views, output schema validation, token-budget estimate and stale/timeout/cancellation handling.
- LLM-backed dialogue bridge: ingress generations, stale-request invalidation, action-ready events, and rejection of responses before the associated user turn/state generation has been committed.
- Local safety checks, mute/quiet mode and ordinary proactive quotas.
- Simulated action dispatch, cancellation, terminal receipts, cancellation-safe stale dispatch checks.
- SQLite event/state/memory persistence, state corruption refusal, memory lineage cascading, prediction rebuild jobs and reconciliation.
- Replay-mode adapter restrictions and common Python socket API guard.

## Implemented but intentionally conservative / not production-calibrated

- World-source conflict logic is a small evidence heuristic, not calibrated Bayesian sensor fusion.
- Token counting is a conservative character-based estimate, not a provider tokenizer.
- Drive values and policy weights are engineering baselines, not tuned behavior parameters.
- Default LLM world-fact allowlist is intentionally small; a product must review each additional key and its possible value contents.
- Replay networking guard is process-wide monkeypatching, not an OS security sandbox.
- Ordinary proactive interaction counters are in-memory; a product requiring quota enforcement across restart should persist the supervisor ledger.
- Persisted intent is never automatically resumed as an old hardware/speech action. It is converted to a re-plan candidate.

## Not implemented

- Real provider clients (OpenAI / other local or cloud services), credentials, retries tuned to service-specific rate limits.
- Actual audio/VAD/ASR/TTS or vision adapters.
- Hardware drivers, motor controllers, emergency-stop circuitry, and physical safety certification.
- Automatic background tick scheduler and production service lifecycle supervisor.
- Full privacy deletion from external backups, provider logs or device-level forensic storage.
- Production metrics exporter, structured rotating log implementation and an operations dashboard.
- Formal performance measurements on target hardware.

## Known integration obligations before a real device pilot

1. Supply a concrete provider adapter that preserves user input as untrusted content, uses strict response formats, and honors cancellation where supported.
2. Connect physical stop to an independent hardware/controller path; Python cancellation is not a physical safety guarantee.
3. Add an app-level loop that calls `step`, dispatches proposals, processes `cancel_action_ids`, and persists state.
4. Always reconcile `SQLiteRepository.pending_prediction_rebuilds()` before prediction-dependent behavior after startup.
5. Audit all world fact keys and memory fields that may enter `StateViewBuilder`.
6. Decide and test persistent proactive quotas, data-retention policy, and deletion across backups for the target product and jurisdiction.
7. Run soak tests, fault injection, replay isolation at process/container level, and physical latency tests on target hardware.
