# Voice integration

The voice pipeline is deliberately adapter-first. It accepts one bounded audio segment from a host capture loop; it does not take over microphone ownership or write raw audio to disk.

## Minimal Python setup

```python
from continuity_engine import VoiceConversationPipeline, PCM16EnergyVAD

pipeline = VoiceConversationPipeline(
    companion=companion_service,
    recognizer=your_asr_adapter,       # async transcribe(audio) -> mapping with text
    synthesizer=your_tts_adapter,      # async speak(text, voice=None) -> mapping
    vad=PCM16EnergyVAD(rms_threshold=450, min_samples=160),
)
turn = await pipeline.process_audio(pcm16_bytes, interaction_id="owner", voice="zh")
```

For `PCM16EnergyVAD`, the input is mono signed 16-bit little-endian PCM. Its threshold is only a starting point; calibrate for sample rate, microphone, gain and noise. It is not a voice-activity neural model.

## JSON command bridge protocol

`JsonCommandSpeechRecognizer` sends one JSON object to the configured executable on stdin:

```json
{"audio_base64":"<base64>","format":"unknown"}
```

The command must print one JSON object containing `text`; optional fields such as `language`, `emotion_label` and `emotion_confidence` are passed through the existing ASR result normalizer. The bridge must decode its audio format/configuration correctly.

`JsonCommandTextToSpeech` sends:

```json
{"text":"spoken words","voice":"zh"}
```

It expects one JSON object on stdout. `status` is recommended; when absent, the adapter treats a well-formed zero-exit response as `SUCCEEDED`. The adapter never uses a shell. Configure absolute executable paths, a restricted environment and process permissions.

These are generic bridge adapters; no concrete model runtime or device is bundled. For reliable barge-in, the TTS bridge must handle process termination or return quickly to cancellation.

## OpenAI-compatible HTTP adapters (v0.23)

For a compatible ASR endpoint:

```python
from continuity_engine import OpenAICompatibleSpeechRecognizer

recognizer = OpenAICompatibleSpeechRecognizer.from_env(prefix="CONTINUITY_ASR_")
```

For compatible TTS, supply a host audio sink explicitly. For example, if `ffplay` is installed and approved, the subprocess player command can be passed without a shell:

```python
from continuity_engine import OpenAICompatibleTextToSpeech, SubprocessAudioSink

sink = SubprocessAudioSink(["ffplay", "-autoexit", "-nodisp", "-loglevel", "error", "-i", "pipe:0"])
synthesizer = OpenAICompatibleTextToSpeech.from_env(sink=sink, prefix="CONTINUITY_TTS_")
```

The ASR endpoint receives the segment as multipart data with the configured filename/content type. Supply a valid supported audio container; raw PCM frames are not automatically wrapped. The TTS endpoint returns encoded audio bytes which are passed to the sink. No audio is written to disk by these adapters. Remote endpoints require HTTPS. Stop/cancel signals call `sink.stop()`; the host remains responsible for audio-device state, output focus and platform-specific reliability.

Configuration variable names are listed in `config/voice.env.example`. This file is a template only; model IDs, credentials, API compatibility and device operation must be verified by the deployer.
