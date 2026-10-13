"""Async audio-in / text-dialogue / audio-out orchestration.

Audio capture is supplied by the host. This pipeline deliberately does not open a
microphone at import or construction time. Raw audio is passed to the ASR adapter and
never persisted by the pipeline.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import math
import struct
from typing import Any, Mapping

from ..core.models import EventKind, Priority
from ..companion.service import CompanionResult, CompanionService, CompanionStatus
from .voice import KeywordSpotter, SpeechRecognizer, TextToSpeech, VoiceActivityDetector, normalize_asr_result


@dataclass(frozen=True, slots=True)
class VoiceTurnResult:
    status: str
    transcript: str = ""
    companion: CompanionResult | None = None
    speech_results: tuple[Mapping[str, Any], ...] = ()
    error_code: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


class PCM16EnergyVAD:
    """Small dependency-free VAD for mono signed 16-bit PCM supplied by the host.

    It is a useful baseline/noise gate, not a production speech recognizer. Calibrate
    the threshold for the target microphone and feed fixed-size frames.
    """
    def __init__(self, *, rms_threshold: float = 450.0, min_samples: int = 160):
        if not math.isfinite(rms_threshold) or rms_threshold < 0:
            raise ValueError("rms_threshold must be finite and non-negative")
        if isinstance(min_samples, bool) or not isinstance(min_samples, int) or min_samples < 1:
            raise ValueError("min_samples must be positive")
        self.rms_threshold = float(rms_threshold)
        self.min_samples = min_samples

    async def detect_speech(self, audio: Any) -> bool:
        if not isinstance(audio, (bytes, bytearray, memoryview)):
            raise TypeError("PCM16EnergyVAD expects bytes-like mono PCM16 audio")
        frame = bytes(audio)
        if len(frame) % 2:
            frame = frame[:-1]
        samples = len(frame) // 2
        if samples < self.min_samples:
            return False
        values = struct.unpack("<" + "h" * samples, frame)
        rms = math.sqrt(sum(float(x) * x for x in values) / samples)
        return rms >= self.rms_threshold


class VoiceConversationPipeline:
    """Cancellable audio turn processor around a `CompanionService`.

    `process_audio` expects one bounded audio segment supplied by a host capture loop.
    Streaming endpoint detection, microphone ownership and device permissions stay at
    the application boundary. A newer turn cancels the prior session's active TTS turn.
    """
    def __init__(self, companion: CompanionService, recognizer: SpeechRecognizer,
                 synthesizer: TextToSpeech, *, vad: VoiceActivityDetector | None = None,
                 keyword_spotter: KeywordSpotter | None = None, max_audio_bytes: int = 16_000_000,
                 max_tts_segment_chars: int = 180):
        if max_audio_bytes < 1024 or max_audio_bytes > 64_000_000:
            raise ValueError("max_audio_bytes must be in [1024,64000000]")
        if not 20 <= max_tts_segment_chars <= 1000:
            raise ValueError("max_tts_segment_chars must be in [20,1000]")
        self.companion = companion
        self.recognizer = recognizer
        self.synthesizer = synthesizer
        self.vad = vad
        self.keyword_spotter = keyword_spotter
        self.max_audio_bytes = max_audio_bytes
        self.max_tts_segment_chars = max_tts_segment_chars
        self._active: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._unregister_output_interrupt = self.companion.engine.register_output_interrupt_callback(self._on_output_interrupt)

    def _on_output_interrupt(self, reason_code: str, interaction_id: str | None) -> None:
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        def cancel_active() -> None:
            selected = [task for session, task in self._active.items()
                        if interaction_id is None or session == interaction_id]
            if not selected:
                return
            for task in selected:
                if not task.done():
                    task.cancel()
            async def stop_output() -> None:
                stop = getattr(self.synthesizer, "stop", None)
                if callable(stop):
                    try:
                        await stop()
                    except Exception:
                        pass
            loop.create_task(stop_output())
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            cancel_active()
        else:
            loop.call_soon_threadsafe(cancel_active)

    async def process_audio(self, audio: Any, *, interaction_id: str = "default", voice: str | None = None) -> VoiceTurnResult:
        self._loop = asyncio.get_running_loop()
        if isinstance(audio, (bytes, bytearray, memoryview)) and len(audio) > self.max_audio_bytes:
            return VoiceTurnResult("REJECTED", error_code="AUDIO_TOO_LARGE")
        async with self._lock:
            previous = self._active.get(interaction_id)
            current = asyncio.current_task()
            if previous is not None and previous is not current and not previous.done():
                previous.cancel()
                stop = getattr(self.synthesizer, "stop", None)
                if callable(stop):
                    try:
                        await stop()
                    except Exception:
                        pass
            task = asyncio.create_task(self._process(audio, interaction_id=interaction_id, voice=voice))
            self._active[interaction_id] = task
        try:
            return await task
        except asyncio.CancelledError:
            stop = getattr(self.synthesizer, "stop", None)
            if callable(stop):
                try:
                    await stop()
                except Exception:
                    pass
            raise
        finally:
            async with self._lock:
                if self._active.get(interaction_id) is task:
                    self._active.pop(interaction_id, None)

    async def _process(self, audio: Any, *, interaction_id: str, voice: str | None) -> VoiceTurnResult:
        if self.keyword_spotter is not None:
            if not await self.keyword_spotter.detect(audio):
                return VoiceTurnResult("NO_WAKE_WORD")
        if self.vad is not None and not await self.vad.detect_speech(audio):
            return VoiceTurnResult("NO_SPEECH")
        engine = self.companion.engine
        started = engine.new_event(EventKind.USER_SPEECH_STARTED.value, {"interaction_id": interaction_id},
                                   source="voice_pipeline", priority=int(Priority.HIGH))
        engine.inject(started)
        try:
            raw = await self.recognizer.transcribe(audio)
            normalized = normalize_asr_result(raw, source=str(raw.get("source", "voice_asr")) if isinstance(raw, Mapping) else "voice_asr")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return VoiceTurnResult("ASR_FAILED", error_code=f"ASR_{type(exc).__name__}")
        finally:
            engine.inject(engine.new_event(EventKind.USER_SPEECH_ENDED.value, {"interaction_id": interaction_id},
                                           source="voice_pipeline", priority=int(Priority.HIGH)))
        transcript = normalized["text"].strip()
        if not transcript:
            return VoiceTurnResult("EMPTY_TRANSCRIPT", error_code="ASR_EMPTY_TEXT")
        assessment = None
        if normalized.get("emotion_is_model_output"):
            assessment = {"emotion": normalized.get("emotion", normalized.get("emotion_label")),
                          "confidence": normalized.get("emotion_confidence"),
                          "source": normalized.get("source")}
        signals = None
        if normalized.get("language") is not None:
            signals = {"kind": "language", "value": str(normalized["language"])[:30]}
        companion_result = await self.companion.chat(transcript, interaction_id=interaction_id,
                                                     emotion_assessment=assessment, voice_signals=signals,
                                                     input_channel="voice")
        if not companion_result.speak_allowed:
            return VoiceTurnResult(companion_result.status.value, transcript=transcript,
                                   companion=companion_result, error_code=companion_result.error_code)
        spoken: list[Mapping[str, Any]] = []
        for segment in companion_result.speech_segments or (companion_result.response_text,):
            # Check generation and stop/mute/quiet before every independently queued bubble.
            if self.companion.engine.current_generation(interaction_id) != companion_result.generation:
                stop = getattr(self.synthesizer, "stop", None)
                if callable(stop):
                    await stop()
                return VoiceTurnResult("STALE", transcript, companion_result, tuple(spoken), "GENERATION_SUPERSEDED")
            blocked = self.companion._blocked_reason()
            if blocked:
                stop = getattr(self.synthesizer, "stop", None)
                if callable(stop):
                    await stop()
                return VoiceTurnResult("SILENCED", transcript, companion_result, tuple(spoken), blocked.upper())
            # Keep bubbles small, but preserve the original words and ordering.
            for offset in range(0, len(segment), self.max_tts_segment_chars):
                chunk = segment[offset:offset + self.max_tts_segment_chars]
                try:
                    speech_result = await self.synthesizer.speak(chunk, voice=voice)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    return VoiceTurnResult("TTS_FAILED", transcript, companion_result, tuple(spoken), f"TTS_{type(exc).__name__}")
                if not isinstance(speech_result, Mapping):
                    return VoiceTurnResult("TTS_FAILED", transcript, companion_result, tuple(spoken), "TTS_INVALID_RESULT")
                spoken.append(dict(speech_result))
                if speech_result.get("status") in {"FAILED", "CANCELLED", "TIMED_OUT"}:
                    return VoiceTurnResult("TTS_FAILED", transcript, companion_result, tuple(spoken), str(speech_result.get("error_code", "TTS_FAILED")))
        return VoiceTurnResult("DEGRADED" if companion_result.degraded else "COMPLETED", transcript,
                               companion_result, tuple(spoken), metadata={"source": normalized.get("source", "voice_asr")})

    async def aclose(self) -> None:
        """Unregister output-control callback and cancel all active sessions."""
        self._unregister_output_interrupt()
        async with self._lock:
            tasks = list(self._active.values())
            for task in tasks:
                if not task.done():
                    task.cancel()
        stop = getattr(self.synthesizer, "stop", None)
        if callable(stop):
            try:
                await stop()
            except Exception:
                pass
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def cancel(self, interaction_id: str = "default") -> bool:
        async with self._lock:
            task = self._active.get(interaction_id)
            if task is None or task.done():
                return False
            task.cancel()
        stop = getattr(self.synthesizer, "stop", None)
        if callable(stop):
            try:
                await stop()
            except Exception:
                pass
        return True
