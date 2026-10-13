# v0.21–v0.24 — Reality Integration & Evaluation

Status: implementation baseline for `0.24.0.dev0`. Tests use mock HTTP transports, replay adapters, temporary SQLite databases and injected probes. No live provider, microphone, camera, physical actuator or target-hardware field test was performed as part of this source build.

## v0.21 — Reality Integration

- `LLMRuntime.attach()` explicitly configures an OpenAI-compatible provider from a file or environment and attaches it to a non-replay engine.
- `LLMRuntime.probe()` distinguishes configuration-only checks from an explicitly requested live inference probe. A live probe may incur cost and send a minimal diagnostic prompt.
- `CompanionMetrics` tracks bounded status/error/latency counters without raw user text, prompts, audio, keys or exception details.
- `SQLiteConversationStore` enables bounded recent-context recovery across service restarts only with explicit `enabled=True`; retention and maximum messages are enforced. Plaintext transcript storage is opt-in and is not an encrypted vault.

## v0.22 — Hybrid Semantic Memory

- `HybridMemoryRetriever` ranks relevant memory rows using lexical overlap, vector similarity and a small recency tie-breaker.
- The default `FeatureHashEmbedder` is deterministic, dependency-free and uses a limited synonym map. It is a fallback retriever, not a trained semantic model.
- `SentenceTransformerEmbedder` provides an optional local trained embedding backend (`pip install 'continuity-engine[semantic]'`) if the model is installed/cached and the host injects it into `CompanionService(memory_retriever=...)`.
- SQLite persists vectors by model ID and invalidates stale vectors on overwrite, user-directed correction or memory deletion. `index_all()` is available for startup/offline index jobs.
- `MemoryLifecycle.correct()` requires a target memory ID and an explicit correction. Similarity alone never overwrites a personal fact. Source lineage is retained and extended.

## v0.23 — Voice Companion

- Added OpenAI-compatible multipart ASR and JSON-to-audio TTS adapters. They send audio only for the live request and do not persist it.
- TTS requires an explicit host-supplied `AudioSink`. `SubprocessAudioSink` pipes bytes to a configured executable with no shell; the host is responsible for installing and configuring an audio player.
- Capture, permissions, codec/container validity and sound-device ownership remain application responsibilities. The ordinary tests use HTTP mock transports and fake sinks; they do not verify a real microphone or speaker.

## v0.24 — Field Validation

- `FieldValidationRunner` performs non-destructive engine/provider/store/voice/body preflight checks, accepts host-provided async probes, and emits JSON reports.
- Reports distinguish `SIMULATED` and `FIELD` modes. `field_validated` remains false unless the caller explicitly declares at least one probe result with `physical_evidence: true`; this is a host attestation, not cryptographic proof that hardware was connected.
- `CompanionEvaluationRunner` runs authored behavior scenarios and reports pass rates, latency percentiles and expectation flags without copying prompts or responses into the report.
- Deployments must run their own long-duration tests against the approved model provider and target hardware before describing a build as field-validated.

## Safety and privacy boundaries

1. Replay mode cannot attach a real remote provider.
2. STOP/CANCEL/QUIET and generation invalidation remain engine-owned and do not depend on model output.
3. Embeddings are derived data and are deleted/rebuilt with the memory they describe. Explicit memory correction requires a memory ID.
4. Recent transcript persistence defaults to disabled. When enabled, it stores plaintext recent turns locally; use restrictive file permissions/encrypted storage and a retention policy suitable for the deployment.
5. HTTP voice adapters reject non-loopback plain HTTP and URLs containing credentials; remote endpoints require HTTPS.
6. Field checks never actuate a physical device themselves. Real-device checks must be supplied by the host and still pass through the installed safety controller.
