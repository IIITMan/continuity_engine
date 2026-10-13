# Companion continuity — v0.17–v0.20

## Scope and status

This development line layers end-to-end companion behavior on top of the v0.16 LIFE_LAYER. It does not claim machine consciousness or human-equivalent feelings. User-affect labels are uncertain estimates, personality is bounded, memory writes require explicit consent/approval, and physical output remains behind the existing engine safety path.

## v0.17 — Companion Loop

Implemented in `continuity_engine.companion`:

- `CompanionService.chat()` orchestrates one user turn using `ContinuityEngine.request_llm()`; first/simple turns use `simple_response`, subsequent turns use `complex_dialogue` with at most 12 recent messages.
- The service handles exact stop/mute/quiet controls through `EventKind` on the control lane before any LLM call.
- `ContinuityEngine.invalidate_interactions()` increments host-owned generations synchronously; canceled/muted/quieted work remains stale even after a control latch is consumed.
- Transcript context is bounded and volatile by default. Service-managed message events do not place raw user text in `EngineState.last_user_message` and do not spawn a duplicate legacy `respond_to_user` intent.
- Explicit “remember this” requests write only through an attached repository. Heuristic candidates remain pending until explicit approval.

## v0.18 — Emotional Continuity

Implemented in `companion/emotion.py`:

- Bounded user-affect estimate with source, confidence and timestamp; unknown/ambiguous input does not overwrite useful state.
- Analytic exponential decay; no-message time does not create negative affect.
- Explicit listening-first and concise strategies from clear user instructions.
- The dialogue result may optionally include a same-call `emotion_assessment`, avoiding a second LLM call by default. Output validation only accepts the reviewed schema. Unknown labels and low-confidence estimates are ignored.
- The estimate is volatile and separate from stable personality, robot permissions and action policy. It is not a diagnosis.

## v0.19 — Voice Companion

Implemented in `voice/pipeline.py` and `voice/command_adapters.py`:

- Host-driven audio segment → optional keyword spotter → optional VAD → ASR → companion turn → TTS pipeline.
- ASR/VAD/TTS remain injected protocols; construction never opens a microphone.
- Newer turns cancel previous synthesis when the adapter supports `stop()`. Each speech segment rechecks generation and control state.
- `PCM16EnergyVAD` is a dependency-free baseline, not a production neural VAD.
- JSON-over-stdin subprocess adapters permit user-supplied ASR/TTS bridge executables without `shell=True`; executable/model packaging is not included.

## v0.20 — Embodied Companion

Implemented in `execution/embodied.py`:

- `EmbodiedExpressionCoordinator` maps only a reviewed semantic set to `ContinuityEngine.propose_body_event()`.
- Stop, safety-stop, quiet, mute and user-speaking gates are checked before proposal creation; cooldowns and reason codes are explicit.
- The coordinator never calls the driver directly. `BodyController`, the existing `IntentManager`, `SafetySupervisor`, allowlist and injected driver continue to decide whether a physical command can run.
- No physical driver is bundled. A missing/disabled/unhealthy driver must fail; software must not report physical success.

## Safety, privacy and control invariants

1. Silence never implies user absence or rejection; affect decays toward neutral and no negative relation event is invented.
2. Stop/cancel/mute/quiet controls are handled without a model call and invalidate active dialogue generations where appropriate.
3. Memory candidates are not persistent memories until explicit user approval. Exact “remember this” statements are only auto-saved when a repository is attached and the content is not credential-like.
4. Raw audio is not written by the pipeline; session transcript is bounded and in-memory. Clear session context with `CompanionService.clear_session()`; it does not erase durable memories.
5. All physical actions still pass through the engine's safety path. The process-level software stop is not a replacement for hardware emergency-stop circuitry.

## Known limitations

- The pipeline does not capture audio devices or package ASR/TTS models. A host must provide audio buffers and compatible adapters.
- The local energy VAD and keyword protocols are integration aids, not a calibrated production speech stack.
- Memory retrieval is lexical SQLite matching, not embedding/vector retrieval or semantic contradiction resolution. Candidate extraction is intentionally conservative.
- Long-term memory is durable when an explicit repository is attached; conversation session history and current user-affect estimates are volatile by default.
- This package does not include a physical BodyDriver or guarantee any particular robot/servo/LED works.
- Full behavior quality requires an actual configured LLM and scenario evaluation. Deterministic unit tests do not establish realistic companionship quality.

## Host integration sketch

```python
from continuity_engine import CompanionService, VoiceConversationPipeline

companion = CompanionService(engine)
turn = await companion.chat("我今天有点累", interaction_id="owner")
if turn.speak_allowed:
    render_text(turn.response_text)

# For voice, pass a host-produced audio frame/segment and concrete adapters:
voice = VoiceConversationPipeline(companion, recognizer=asr, synthesizer=tts, vad=vad)
voice_turn = await voice.process_audio(audio_bytes, interaction_id="owner")
```

The host should run the engine's deterministic `step()` loop as usual and own microphone permissions, device health, TTS policy, logs and shutdown ordering.
