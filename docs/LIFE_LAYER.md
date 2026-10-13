# LIFE_LAYER — Continuity Engine v0.16.0-dev

Status: implemented development module; numeric parameters are starting assumptions, not target-hardware calibration or validation claims.

## Design boundaries

LIFE_LAYER adds deterministic continuity signals to the existing engine. It does not claim consciousness or human emotion. It does not own action authorization, safety, or user control. `IntentManager` remains the only intent activation authority, and `SafetySupervisor` remains the action gate. The default BodyDriver is absent and body output is disabled.

Invariants:

1. Silence and lack of speech do not imply absence, rejection, sadness, anger, or loneliness. Attachment separation advances only from a fresh, confirmed `owner_present=False` world fact with confidence at or above the configured threshold.
2. Explicit cancellation, STOP, safety-stop and output mute enter the independent control path. A cancelled Will state remains terminal until a new explicit user request resets it. Temporary preemption is distinct from cancellation.
3. Any proactive activity remains subject to existing quota/cooldown and quiet-mode controls. LIFE_LAYER itself does not send messages.
4. Traits, drives, breath and attachment cannot bypass SafetySupervisor, action allowlists, confirmation rules or replay constraints.
5. The life stream stores bounded structured event metadata only; raw dialogue/transcripts are rejected recursively.

## Organs

- `state/drive_field.py`: pure snapshot over the existing DriveSystem. It does not introduce a second drive state authority. Action-wave comparisons exclude the safety background wave. Struggle has a low-activation gate and sleep cap.
- `state/will.py`: bounded state machine (`IDLE`, `ATTEMPTING`, `INTERRUPTED`, `RECONSIDERING`, `ABANDONED`, `CANCELLED`) and a `dt` accumulator. It emits candidates only; it does not activate or execute actions. The default useless-candidate probability is zero to avoid high-frequency unsolicited behavior until UX cadence is measured.
- `state/attachment.py`: bounded trust and separation signals with explicit evidence gating. Unknown, stale, probable and conflicting presence facts are ignored. Reunion is classified as brief/away/long-away based on configured elapsed duration.
- `state/life_stream.py`: bounded daily structured event journal and deterministic weighted-variant summaries. Consolidation is idempotent per day across repeated calls and persistence/restart. A UTC date boundary is used for the development default; hosts can call `set_life_sleeping(True)` to consolidate at a supplied sleep transition. The engine never infers sleep from silence.
- `state/temperament.py`: bounded traits with age-dependent plasticity and a maximum offset. Its action-parameter projection is pure; safety or permissions are not parameters.
- `state/breath.py`: analytic rate response with integrated phase, bounded rate/depth, a separate visible shock-burst channel, and a bounded wake window that can return a previously sleeping model to sleep. It is a software state model, not physiological measurement.
- `state/habituation.py`: repeat-stimulus attenuation affects the expressive burst only. Repeated events still produce a non-zero response and continue through world/safety evaluation; a known event is never treated as proof of safety.
- `execution/expression.py`: pure formatting helpers for speech-bound text. It does not edit action arguments. Necessary identity/limitation disclosures are preserved; sentence-aware truncation is best-effort, not language generation.
- `execution/body.py`: semantic command allowlist and injected `BodyDriver` protocol. Body output requires explicit configuration and a real reviewed driver. No driver is bundled; missing/unhealthy drivers fail explicitly.

## Memory and dialogue integration

`SQLiteRepository` exposes bounded `list_memories`, lexical `search_memories`, and `update_memory` methods while preserving source lineage. `ContinuityEngine.attach_memory_repository()` explicitly attaches a repository; `request_llm()` then injects a small relevant-memory projection into `simple_response`/`complex_dialogue` views. `remember`, `recall_memories`, and `forget_memory` are host-callable helpers.

The current implementation does not automatically turn every conversation into permanent memory. Explicit “remember this” requests can create a concise note only when a repository is attached; likely credentials are rejected. A product integration still needs a reviewed memory extraction/approval policy, retention policy, user-visible memory management and an end-to-end deletion audit.

`LLMDemandInterpreter` returns a validated `emotion_assessment`; the engine applies only a conservative, confidence-gated subset to transient affect. The evidence text is not saved, affect is not persisted as a fact, and stable personality traits are not changed from emotional sentiment. Emotion labels remain estimates.

When the LLM is missing or a provider call fails/times out, a local fallback can be returned for ordinary dialogue if configured. It explicitly discloses degraded operation and is suppressed while quiet, output-muted, safety-stopped or the user is speaking. Stale/cancelled generations are never replaced with fallback dialogue.

## Persistence and migration

`EngineState.life_layer_state` is JSON-compatible and additive. SQLite state snapshots write schema `1.2` and continue to load `1.0`/`1.1` snapshots using the existing loader defaults. Each organ validates its own restored state; invalid organ state fails back independently where possible. Monotonic timestamps are not persisted as a clock across process restart; the snapshot records a neutral value for the tick clock and uses wall timestamps for dated journal history.

Configuration changes under `life_layer`, `action_execution.body`, and the attachment drive are marked restart-required. The development config intentionally keeps physical body execution disabled.

## Verification and release limitations

The tests include numeric boundaries, large-`dt` behavior, replay determinism, cancellation, evidence-gated attachment, bounded journal and trait state, state persistence, memory lineage, quiet-mode fallback suppression and missing-driver failure. A successful unit suite does not prove natural companion behavior, calibrated emotion recognition, 90-day stability, real device timing, or production physical safety. Those require longer simulations and target-hardware evaluation before deployment.
