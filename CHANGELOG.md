# Changelog

## 0.25.1-dev — Context continuation regression fix

- Fixed `DialogueContextBudgeter.fit_dialogue()` so an oversized older message is middle-trimmed rather than terminating the backwards scan and discarding all earlier context. If a trim marker cannot fit in the remaining budget, that message is skipped and the scan continues to older short items.
- Added a tighter, configurable assistant-message ceiling (`max_assistant_message_tokens`, default 120 estimated tokens) while user messages retain the 220-token ceiling. This prevents verbose assistant prose from consuming the whole rolling window and evicting user-stated goals/preferences. The overall dialogue budget remains bounded at 700 estimated tokens by default.
- Added regression cases for a long assistant reply followed by a question that refers to a goal stated several messages earlier, plus older short facts behind oversized messages.
- Local verification: 315 tests passed on the recorded Linux run; **347 passed, 2 failed, 1 skipped** on the Windows development host (both failures are pre-existing platform-specific tests). This includes deterministic reproduction of the `context_continuation` failure from the user-provided Volcano Ark drill.
- **Live re-run of the same 60-turn drill on the patched build (2026-10-13 18:46:57): 17/17 verdicts, `context_continuation` restored to PASS.** Provider-billed input tokens 111,493 (no-budget baseline) → 92,465 (budget, regressed) → **94,282** (fixed), i.e. −15.4% versus the baseline and +2.0% versus the regressed build. P50 6.590 → 5.171 → **5.883 s**, P95 14.436 → 11.193 → **12.150 s**. Memory recall correct on the first attempt (mid-run and after restart); hard failures 0, disclosed degradations 0, error rate 0.0; all transport, cancellation, restart and outage-recovery drills pass; `reasoning_tokens=0` confirms thinking stayed disabled. Latency/output-token figures remain n=1 per build and are not attributed to the change alone. See [`docs/PROVIDER_DRILL_REPORT_EN.md`](docs/PROVIDER_DRILL_REPORT_EN.md) and [`docs/PROVIDER_DRILL_REPORT_CN.md`](docs/PROVIDER_DRILL_REPORT_CN.md).
- Incidental: restored the `provider_errors` / `reliability` / `validation.provider_drill` exports in `continuity_engine/__init__.py`, which had been reverted and blocked `tests/test_provider_reliability.py` collection and `examples/real_provider_companion.py`.

## 0.25.0-dev — Acceptance hardening and context efficiency

- Added `DialogueContextBudgeter` with fixed prior-dialogue and memory budgets, recent-first truncation, per-message/memory caps, and a conservative mixed-language token estimator. The current user turn is no longer duplicated in `recent_dialogue` when also passed as `user_input`.
- Added approximate context token samples/P50/P95 to privacy-safe `CompanionMetrics`, plus per-turn trimming counters. These estimates are explicitly separate from provider-reported/billed token counts.
- Fixed the recall-to-state-view boundary: internal retrieval scores are no longer included in the strict LLM memory schema, preventing schema failures when real stored memories are actually retrieved.
- Optimized hybrid retrieval to bulk-load model-scoped cached vectors and lazily compute only a bounded shortlist of missing vectors per query. `index_all()` remains the full offline/startup indexing path.
- Hardened SQLite storage with `secure_delete` and best-effort POSIX owner-only permissions for memory/state and transcript database, WAL and SHM files.
- Added opt-in `TranscriptCipher` support and optional `FernetTranscriptCipher`; transcript-store mode/key mismatch fails closed, and existing plaintext databases are not silently converted in place.
- Added offline context budget benchmark and regressions for context trimming, duplicate-turn removal, stored-memory strict schema, bounded embedding calls, encrypted transcript storage, file permissions and metrics privacy.
- No live provider was called as part of this optimization run; the synthetic context benchmark is not a claim of live-provider token or latency reduction.

## 0.24.0-dev — Reality integration and field validation

- Added `LLMRuntime` to explicitly attach an OpenAI-compatible provider to a live (non-replay) engine, report configuration separately from live reachability, optionally perform a minimal inference probe, and close provider resources.
- Added privacy-preserving `CompanionMetrics`, bounded recent-dialogue persistence through opt-in `SQLiteConversationStore` (disabled by default), and restart-safe context rebasing for engine generations.
- Added `HybridMemoryRetriever`, model-scoped durable vector caches in SQLite, offline `index_all()`, a deterministic feature-hash fallback, optional `SentenceTransformerEmbedder`, explicit memory correction by ID, and vector invalidation on update/deletion.
- Added OpenAI-compatible multipart ASR and HTTP TTS adapters with a separately injected audio sink; `SubprocessAudioSink` uses argv-only process creation and cancellation instead of a shell.
- Added `CompanionEvaluationRunner` for authored turn scenarios and `FieldValidationRunner` for readiness reports. A report cannot say `field_validated=true` without an explicitly host-attested physical probe; this flag is not cryptographic proof.
- Added environment/provider, ASR/TTS, memory, transcript retention, correction, metrics and preflight regression tests. Full suite: 301 passed using local/replay fixtures, temporary SQLite databases and HTTP mock transports. No live provider, microphone, physical speaker/actuator or target field environment was used.

## 0.21–0.23 development milestones

- v0.21: real-provider runtime setup, explicit probing, runtime metrics and opt-in recent-context restoration.
- v0.22: hybrid memory retrieval, durable vectors, offline indexing and explicit correction/lineage handling.
- v0.23: HTTP ASR/TTS adapters and injectable audio playback contract; device capture/playback remains host-owned.

## 0.20.0-dev — Companion continuity, voice and guarded embodiment

- Added `CompanionService` as a text conversation orchestrator over the existing engine and LLM gateway, with bounded volatile session context, simple/complex dialogue purposes, generation checks and independent control-lane handling.
- Added `MemoryLifecycle`: explicit non-sensitive “remember this” requests persist only through an attached repository; heuristic memory candidates require explicit approval; lexical recall and forget APIs reuse the existing SQLite memory store and lineage behavior. Credential-like explicit memory requests are redacted and rejected before reaching the model.
- Added conservative `EmotionContinuity` with confidence-gated estimates, neutral decay, explicit listening-first/concise preferences and no negative inference from silence. Optional same-call emotion assessments have a strict response schema.
- Added host-driven `VoiceConversationPipeline` and PCM16 energy VAD baseline. New voice turns, text barge-in, mute/quiet/stop and stale generations can cancel active TTS. Audio capture and real ASR/TTS model runtimes remain host-provided.
- Added bounded JSON-stdin/stdout subprocess adapters for optional ASR/TTS command bridges; subprocesses are created without a shell and support timeout/cancellation.
- Added `EmbodiedExpressionCoordinator`, a small reviewed semantic mapping that submits body proposals through the engine's current safety path and fails closed when disabled or unavailable. No production physical driver is bundled.
- Added companion, emotion, memory, voice and embodiment integration tests and a runnable offline companion-loop demo.
- Source-tree regression suite: 287 passed. No live model endpoint, public network, microphone/camera or physical actuator is exercised by ordinary tests.

## 0.16.0-dev

- Added optional LIFE_LAYER organs: pure drive-field/struggle view, bounded WillArbiter candidates with explicit cancellation, evidence-gated Attachment, idempotent structured LifeStream consolidation, bounded Temperament drift, event-class expressive habituation and analytic BreathModel with bounded wake/sleep return.
- Added expression formatting helpers and optional semantic BodyDriver protocol. Hardware dispatch remains disabled unless a reviewed driver is injected; missing/unhealthy drivers return explicit failures.
- Extended `EngineState` snapshots to carry versioned LIFE_LAYER state and added compatible SQLite state-snapshot schema migration. Existing drive system, IntentManager, SafetySupervisor, quiet/mute/stop controls, and action allowlists remain authoritative.
- Added bounded SQLite memory listing/search/update APIs that retain source lineage, engine-facing remember/recall/forget helpers, and memory retrieval injection into dialogue views when a repository is explicitly attached. This is not automatic unrestricted transcript storage.
- Routed validated LLM emotion assessments into confidence-gated transient affect updates only; stable personality changes still require explicit style feedback.
- Added clearly disclosed local dialogue fallback for provider failure, with fallback suppression under quiet, output-muted, safety-stop, and user-speaking states.
- Added deterministic LIFE_LAYER tests for formulas, cancellation, persistence, evidence gating, boundedness, idempotent consolidation, body-driver failures and emotion assessment integration.
- No real LLM endpoint, public network, microphone, camera or physical body driver was exercised by the test suite.

## 0.13–0.15 development milestones

- v0.13: DriveField and WillArbiter primitives; Will only proposes candidates and never authorizes or dispatches actions.
- v0.14: Attachment and LifeStream; confirmed presence evidence gates separation/reunion, structured summaries retain no raw dialogue.
- v0.15: temperament-to-action projections, expression helpers and explicit degraded fallback signaling.


## 0.12.0-dev

- Added `LLMDemandInterpreter` and strict schema v1.0 for structured demand understanding; output includes request type, intent summary, entities, bounded emotion assessment, clarification flag, and optional response candidate. It does not issue route/action instructions.
- Added `ContinuityEngine.understand_demand()` to provide personality and interaction-state projections to the new interpreter.
- Added runtime `SkillRegistry` with explicit permissions, bounded concurrency/timeouts, feedback records, and safe unload/disable rules for active executions.
- Added optional KWS/VAD/ASR/TTS protocols and source-aware ASR result normalization without a hard Sherpa-ONNX dependency.
- Kept the v0.11.1-dev fine-grained intent router as a compatibility path; the new demand contract is additive and must be paired with engine-owned routing policy by new integrations.

## v0.11.0-dev — Bounded interaction state (no short-term-goal manager)
- Added an in-memory `InteractionState` that stores bounded, expiring semantic signals and at most one host-approved current conversational strategy. It has no candidate-goal, priority, activation, or goal-destruction mechanism.
- Signals never activate a strategy automatically; the host must explicitly set or clear the current strategy. Raw dialogue is not stored in this state manager.
- Added explicit strategy/signal projection to dialogue state views and bounded extraction into the OpenAI-compatible dialogue system prompt. Safety, privacy, system constraints, and the user's explicit current boundary remain higher priority.
- Added engine APIs for observing signals and setting/clearing the current strategy, plus deterministic tests for validation, expiry, boundedness, projection, and adapter extraction.
- This is an initial integration: strategy creation and semantic applicability remain host responsibilities; the package does not claim to infer emotional state accurately or enforce arbitrary natural-language strategy semantics deterministically.


## v0.10.0-dev — LLM-first semantic intent interpretation
- Added `LLMIntentInterpreter`, backed by the existing `LLMGateway`, with a dedicated `intent_classification` purpose, a constrained JSON schema, and explicit intent/slot allowlists.
- Changed `RequestRouter.route_async()` to ask the LLM to classify every ordinary request, not only locally ambiguous phrases. The deterministic classifier is retained for the STOP/CANCEL/QUIET control lane and the synchronous legacy API.
- Removed silent keyword fallback on the async path: missing, failed, or malformed LLM interpretation returns an unsupported/fail-closed decision without executing an action.
- Added bounded `RequestContext.conversation_context` so the host can supply sanitized recent turns or memory for contextual references; the model also receives the actual registered capability summary and safe request context.
- LLM classification is not authorization. The router still binds only registered capabilities, validates slots and required arguments, and preserves confirmation, allowlist, safety-stop, and pre-dispatch constraints.
- Added deterministic tests for LLM-first routing, outage behavior, schema rejection, and interpreter/gateway integration. Full suite: 232 passed; no live LLM or hardware calls.

## v0.9.1-dev — Affect & Personality
- Enabled a stable, configurable personality profile with bounded traits (warmth, curiosity, playfulness, directness, verbosity, initiative).
- Added transient affect signals (valence, arousal, social energy) from current user messages; raw text is not persisted.
- Added explicit style-preference learning in small 0.05 increments, persisted atomically to a local JSON profile.
- Injected bounded expression guidance into dialogue system prompts. Affect/personality never changes safety rules, permissions, action allowlists, or quiet-mode controls.
- Added deterministic regression tests for persistence, bounded learning, transient affect, state-view projection, and prompt extraction.

## v0.9.0-dev — Persistent reminders, live weather and source-backed knowledge

- Added an SQLite-backed reminder scheduler with a background due-time worker, timezone-aware Chinese/ISO time parsing, list and cancel routes, a platform desktop-notification delivery adapter, and a host-managed async lifecycle.
- Reminder requests report success only after the reminder row is committed. Delivery outcomes are tracked; an in-flight reminder recovered after a process crash is marked `UNKNOWN` rather than automatically re-fired, avoiding duplicate external side effects.
- Added an Open-Meteo geocoding/current-weather/forecast provider with source metadata, forecast day selection, timeout handling and explicit network opt-in.
- Added a Wikipedia MediaWiki search provider with source-backed extracts/URLs, bounded results and a TTL cache. Knowledge intent routes to retrieval when that provider is explicitly wired; otherwise it follows the existing LLM or fail-closed path.
- Added `build_v09_provider_bundle()` and `examples/v09_capabilities_demo.py` for lifecycle, request routing, reminder management and opt-in weather/knowledge access.
- Added deterministic integration tests for automatic reminder delivery, create/list/cancel through the configured router, and end-to-end weather/knowledge providers using `httpx.MockTransport`; no live endpoint was called.


## v0.8.1-dev — Request routing edge-case fixes

- Expanded locally handled quiet/stop aliases including “闭嘴”, “安静点”, “别吵”, “别再说了” and standalone “停一下”; standalone aliases do not shadow “暂停音乐”.
- Routed common microphone-status phrasings such as “你能听见我说话吗” to the read-only `audio.input` capability query, preserving freshness checks.
- Generalized composite navigation + observation recognition for “去 X 看看有什么” and related inspection wording across recognized locations, rather than depending on a short hard-coded list of rooms and people-specific phrases.
- Added observation query text to `vision.observe_location` calls so the provider knows what to inspect; movement and observation remain sequential and are still gated by provider availability, location allowlists, protected-location confirmation, and live pre-dispatch checks.
- Recognized common light-control paraphrases such as “把灯关掉” as `DEVICE_CONTROL`; actual execution still requires an injected, authorized provider.
- Added 15 routing regression cases. Local deterministic suite: 207 passed. No live provider, network, sensor, or hardware call was made in this development pass.

## v0.8.0-dev — Capability-based user request routing

- Added `RequestRouter` intent taxonomy for user controls, conversation, knowledge/current information, time/weather, capability-status questions, visual observation, mobility, composite navigate-and-observe requests, reminders, timers, media/device control, and general task delegation.
- Added an explicit capability registry. Provider-backed features remain unavailable unless the relevant config/product gates are enabled and a real handler is injected; the built-in clock is the only built-in data provider.
- Added constrained optional async intent resolution for ambiguous inputs. The resolver cannot invent capability IDs, claim explicit-command authority, or synthesize quiet/stop/cancel controls.
- Added request-level clarification/confirmation flows, location-map checks, protected-room confirmation, and explicit distinction between "can you see me?" capability queries and actual visual observations.
- Added timer duration normalization and media-control intent handling; both remain disabled until a real provider is configured.
- Added a live pre-dispatch guard contract: high/critical provider calls fail closed without a guard, and compound requests re-check the guard before every step.
- Added separate timer and media-control intents, a current-object observation intent, safer scene/location disambiguation, and freshness-aware microphone capability reporting.
- LLM-inferred actions involving side effects or medium/high/critical risk require explicit confirmation.
- Added 23 request-router regression tests and a dry-run request-routing demo; no real provider, network service, camera, mobile base, reminder, media player or device was used.
- Classified “你能看见我吗 / Can you see me?” as an explicit one-shot visual observation request; pure camera capability/status questions remain read-only.

## v0.7.0-dev — Schema-guided plan repair and real-context filtering

- Added exact per-action argument schemas and OBSERVE world-key allowlist to the task-plan state view.
- Made an empty OBSERVE allowlist fail closed and split keys visible to LLMs from keys an observation provider may actually observe.
- Added a bounded plan-repair attempt for deterministic schema/plan rejection; repair feedback carries the error code and the same schema constraints. Provider failures, timeouts, cancellation and stale results are not retried.
- Defaulted provider concurrency to one and documented that stateful dialogue turns on one engine must be serialized by the host.
- Added current `world_summary` to simple-response views while filtering facts that have expired according to monotonic time.
- Added `OpenAICompatibleAdapter.from_file()` for explicitly selected YAML configuration, strict config-key validation, a secret-safe example file and `.gitignore` rules.
- Added deterministic regression tests for invalid LIGHT patterns, unallowlisted OBSERVE keys, bounded repair, empty allowlists, expired context facts and file-based provider configuration.
- No live provider calls were made by this development pass; provider behavior remains covered by fixtures/MockTransport.

## v0.6.1-dev — Replay network guard platform compatibility

- Fixed replay-mode network guard to allow IPv4/IPv6 loopback TCP and Unix-domain IPC required by local event-loop implementations, including Windows ProactorEventLoop `socketpair()` fallback.
- Continued to block non-loopback socket connections, external host name resolution, non-loopback UDP sends, and sends through pre-connected non-loopback sockets.
- Updated guard tests to assert the intended policy: external egress is blocked while local asyncio/loopback IPC remains usable.
- Replay guard remains defense-in-depth; OS/container-level egress restrictions are still required for security-grade isolation.

## v0.6.0-dev — Real host action executor

- Added `RealActionExecutor` as an explicit opt-in executor. The checked-in configuration remains simulated by default to prevent surprise side effects.
- Added platform-native host speech drivers: Linux `espeak-ng`/`espeak`, macOS `say`, and Windows PowerShell + `System.Speech`; commands are started without a shell.
- Added platform desktop notifications via Linux `notify-send`, macOS `osascript`, and Windows Forms `NotifyIcon`.
- Added real cancellable `WAIT`, callback-based `OBSERVE` with strict result validation, and a Linux sysfs LED-class `LIGHT` adapter with configured LED names and restore-on-duration/cancellation.
- Added action-ID idempotency, duplicate-call joining, collision detection, cancellation-aware subprocess termination, and conservative `UNKNOWN` status when external effects may have occurred.
- Added explicit `action_executor_from_config`, engine async shutdown, opt-in smoke-test CLI, driver documentation and tests.
- Verified using fake output drivers, temporary sysfs files, local test subprocesses and the existing engine safety dispatch path. No real speech/notification, physical LED, sensor hardware or mobile robot was exercised in this development run.

## v0.5.0-dev — LLM plan integration

- Added a dedicated `task_plan` state-view schema with bounded failed-plan context and an explicit action allowlist.
- Added strict LLM output validation for linear plans and supported action argument shapes.
- Added `LLMTaskPlanner` for new-goal planning and definite-failure replanning, including preflight through a copied TaskManager, stable request IDs, cancellation, stale-state checks, and idempotent plan-event enqueue.
- Added an end-to-end replay example and task-planning integration tests.
- Added an optional OpenAI-compatible async HTTP adapter with purpose-specific instructions, JSON-object response handling, endpoint validation, and environment-based secret loading.
- Added MockTransport tests for provider request construction and end-to-end task planning; no live provider endpoint was contacted.

## 0.4.0.dev0 — Sequential task execution and replan contract

- Added bounded sequential task plans with allowlisted actions/arguments, task and step deadlines, retry limits, persistence and explicit result reconciliation.
- Added stable `EngineStepResult.replan_request` after definite task-step failure; the core never invokes an LLM from the synchronous decision loop.
- Added `replaces_task_id` binding for replans, strict plan snapshot validation, one-time operator-confirmed retry and recovery-safe action unbinding before executor entry.
- Fixed task deadline expiry during an in-flight action to become `NEEDS_RECONCILIATION`; uncertain actions are not treated as definitely unexecuted.
- Successful `OBSERVE` results may feed one configured world fact through a strict result schema; arbitrary keys are rejected and the engine, not the executor, writes the world model.

## 0.3.0.dev0 — Autonomous behavior generation

- Added rule-based candidates for quiet rest, observation, and gated social check-in.
- Added bounded rest duration, recovery threshold, satisfaction impulse, observation cooldown, proactive rate limits and suppression.

## 0.2.0.dev0 — Executable MVP scaffold

- Initial deterministic engine core, event system, drive dynamics, intent manager, action/safety interfaces, filtered LLM gateway, persistence, and replay tests.

## 0.12.0.dev1

- Removed the legacy `LLMIntentInterpreter` / `RequestRouter.route_async()` request path and the old `intent_classification` LLM output contract.
- Added `ContinuityEngine.process_user_request()` as the canonical structured-demand -> engine policy-routing entry point.
- Reorganized `src/continuity_engine` into responsibility packages: `core`, `understanding`, `routing`, `state`, `llm`, `tasks`, `skills`, `execution`, and `voice`.
- Added a reference echo skill and the normative skill development standard, including permissions, lifecycle, validation, result and testing requirements.
