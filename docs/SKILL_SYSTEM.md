# Pluggable skill interface (v0.12.0-dev)

`SkillRegistry` provides runtime registration, enable/disable, dispatch, permission checks, bounded concurrency, timeouts, execution results, and a bounded feedback snapshot.

- A skill must implement `async execute(SkillCall)`.
- `SkillSpec.required_permissions` is checked against host-granted permissions on each dispatch. Registration/discovery does not grant permissions.
- Disabled or missing skills return `UNAVAILABLE`; missing grants return `DENIED`.
- Exceptions and timeouts become failure results without exposing exception messages or secrets.
- A skill cannot be disabled, replaced, or unloaded while it has active executions. An active call is counted while waiting for its concurrency slot as well as while running.
- Feedback is returned to the host; it does not automatically modify policy or interaction strategy.

Voice boundaries are optional protocols in `voice.py`: `KeywordSpotter`, `VoiceActivityDetector`, `SpeechRecognizer`, and `TextToSpeech`. The core imports no Sherpa-ONNX package and remains usable without a microphone or TTS adapter. `normalize_asr_result` carries only explicit text/emotion-related fields and records the source; model emotion output is not treated as verified truth. Concrete KWS/VAD/SenseVoice/TTS adapters and hardware-specific lifecycle management are not bundled in this stage.

## Skill authoring standard

The normative development rules and minimum test requirements are in [`SKILL_DEVELOPMENT_STANDARD.md`](SKILL_DEVELOPMENT_STANDARD.md). The executable reference implementation is [`examples/skills/echo_skill.py`](../examples/skills/echo_skill.py).
