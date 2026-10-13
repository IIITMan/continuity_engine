"""Optional voice adapter contracts; importing the core never imports Sherpa-ONNX."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


@dataclass(frozen=True, slots=True)
class VoiceSignal:
    """A bounded observation from a voice adapter, not a verified fact."""
    source: str
    kind: str
    value: str | bool | float | int | None = None
    confidence: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source or len(self.source) > 100 or not self.kind or len(self.kind) > 100:
            raise ValueError("source and kind are required and bounded")
        if self.confidence is not None and (isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)) or not 0 <= self.confidence <= 1):
            raise ValueError("confidence must be in [0,1]")


class KeywordSpotter(Protocol):
    async def detect(self, audio: Any) -> bool: ...


class VoiceActivityDetector(Protocol):
    async def detect_speech(self, audio: Any) -> bool: ...


class SpeechRecognizer(Protocol):
    async def transcribe(self, audio: Any) -> Mapping[str, Any]: ...


class TextToSpeech(Protocol):
    async def speak(self, text: str, *, voice: str | None = None) -> Mapping[str, Any]: ...


def normalize_asr_result(result: Mapping[str, Any], *, source: str = "sensevoice_asr") -> dict[str, Any]:
    """Keep only recognized text and explicitly supplied emotion-related fields.

    The adapter/model determines exact labels. Unknown keys are not guessed or promoted.
    """
    if not isinstance(result, Mapping):
        raise TypeError("ASR result must be a mapping")
    text = result.get("text")
    if not isinstance(text, str):
        raise ValueError("ASR result must include string 'text'")
    out: dict[str, Any] = {"text": text[:12000], "source": source}
    for key in ("emotion", "emotion_label", "emotion_confidence", "language", "timestamps"):
        if key in result:
            value = result[key]
            if isinstance(value, (str, int, float, bool, list, dict)) or value is None:
                out[key] = value
    out["emotion_is_model_output"] = any(k in out for k in ("emotion", "emotion_label", "emotion_confidence"))
    return out
