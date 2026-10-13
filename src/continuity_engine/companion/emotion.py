"""Conservative, volatile emotion continuity for companion dialogue.

This module tracks an *estimate of the user's expressed affect*, not the robot's
subjective feelings or a clinical assessment. It stores no raw utterances.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
import time
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class EmotionEstimate:
    label: str | None
    valence: float
    arousal: float
    confidence: float
    source: str
    observed_at: float
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label, "valence": self.valence, "arousal": self.arousal,
            "confidence": self.confidence, "source": self.source,
            "observed_at": self.observed_at, "evidence": list(self.evidence),
        }


class EmotionContinuity:
    """Small, deterministic and bounded affect estimate with analytic decay.

    Unknown and ambiguous input keeps the previous estimate and lets it decay; the
    classifier does not infer negative emotion from silence or absence of messages.
    """
    POSITIVE = ("开心", "高兴", "太好了", "很棒", "成功了", "谢谢你", "感激", "兴奋", "期待", "哈哈")
    NEGATIVE = ("难过", "伤心", "委屈", "失望", "焦虑", "压力大", "很烦", "生气", "害怕", "沮丧")
    FATIGUE = ("累了", "好累", "疲惫", "困了", "精疲力尽", "没精神")
    CALM = ("平静", "放松", "安心", "踏实")
    NO_ADVICE = ("不用建议", "不需要建议", "别给建议", "只想说说", "听我说就好", "先听我说", "不用帮我解决")
    BREVITY = ("简短点", "简单说", "别太长", "简洁一点")

    def __init__(self, *, decay_tau_s: float = 900.0, confidence_floor: float = 0.05,
                 clock=time.time):
        if not math.isfinite(decay_tau_s) or decay_tau_s <= 0:
            raise ValueError("decay_tau_s must be finite and positive")
        if not 0 <= confidence_floor <= 1:
            raise ValueError("confidence_floor must be in [0,1]")
        self.decay_tau_s = float(decay_tau_s)
        self.confidence_floor = float(confidence_floor)
        self._clock = clock
        now = float(clock())
        self._estimate = EmotionEstimate(None, 0.0, 0.18, 0.0, "unknown", now, ())
        self._last_update = now
        self._strategy = "balanced"

    @staticmethod
    def _unit(value: Any, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError(f"{name} must be a finite number")
        return min(1.0, max(0.0, float(value)))

    def observe(self, text: str, *, model_assessment: Mapping[str, Any] | None = None,
                now: float | None = None, source: str = "text") -> EmotionEstimate:
        if not isinstance(text, str) or len(text) > 12000:
            raise ValueError("text must be a string up to 12000 characters")
        now = float(self._clock() if now is None else now)
        if not math.isfinite(now):
            raise ValueError("now must be finite")
        self._decay(now)
        self._strategy = self._choose_strategy(text)
        candidate = self._from_model(model_assessment, now, source) if model_assessment else None
        if candidate is None:
            candidate = self._from_text(text, now, source)
        # Don't overwrite useful context with a low-confidence/unknown guess.
        if candidate is not None and candidate.confidence >= 0.45:
            self._estimate = candidate
            self._last_update = now
        return self.current(now=now)

    def _from_model(self, assessment: Mapping[str, Any] | None, now: float, source: str) -> EmotionEstimate | None:
        if not isinstance(assessment, Mapping):
            return None
        label = assessment.get("emotion", assessment.get("emotion_label"))
        confidence = assessment.get("confidence", assessment.get("emotion_confidence"))
        evidence = assessment.get("evidence", [])
        if not isinstance(label, str) or not label.strip() or len(label) > 80:
            return None
        try:
            confidence = self._unit(confidence, "confidence")
        except ValueError:
            return None
        if confidence < 0.60:
            return None
        valence, arousal = self._map_label(label.casefold())
        if valence is None:
            return None
        safe_evidence = tuple(x[:100] for x in evidence[:4] if isinstance(x, str)) if isinstance(evidence, list) else ()
        return EmotionEstimate(label.strip().casefold()[:80], valence, arousal, confidence,
                               str(assessment.get("source", source))[:80], now, safe_evidence)

    def _from_text(self, text: str, now: float, source: str) -> EmotionEstimate | None:
        # Avoid classifying obvious negations such as "我不开心" as positive.
        lower = text.casefold()
        matches: list[tuple[str, float, float, float]] = []
        for words, label, valence, arousal, confidence in (
            (self.POSITIVE, "positive", .45, .55, .62),
            (self.NEGATIVE, "negative", -.45, .68, .66),
            (self.FATIGUE, "fatigue", -.12, .12, .62),
            (self.CALM, "calm", .18, .18, .58),
        ):
            matched = next((w for w in words if w.casefold() in lower), None)
            if matched:
                if any(neg + matched in lower for neg in ("不", "没", "并不", "不太")):
                    continue
                matches.append((label, valence, arousal, confidence))
        if not matches:
            return None
        # Conflicting lexical signals are ambiguous; reduce confidence rather than
        # force a single mood label.
        label, valence, arousal, confidence = matches[0]
        if len({x[0] for x in matches}) > 1:
            confidence *= 0.65
        if confidence < 0.45:
            return None
        return EmotionEstimate(label, valence, arousal, confidence, source, now, ())

    @staticmethod
    def _map_label(label: str) -> tuple[float | None, float]:
        if any(x in label for x in ("joy", "happy", "gratitude", "excited", "positive", "开心", "喜悦", "感激", "兴奋")):
            return .45, .58
        if any(x in label for x in ("sad", "anger", "angry", "fear", "anxiety", "frustrat", "disappoint", "negative", "难过", "悲伤", "生气", "愤怒", "害怕", "焦虑", "挫败", "失望")):
            return -.45, .68
        if any(x in label for x in ("tired", "fatigue", "exhaust", "疲惫", "累", "困")):
            return -.12, .12
        if any(x in label for x in ("calm", "relaxed", "平静", "放松")):
            return .18, .18
        return None, 0.18

    def _decay(self, now: float) -> None:
        dt = max(0.0, now - self._last_update)
        factor = math.exp(-dt / self.decay_tau_s)
        old = self._estimate
        confidence = old.confidence * factor
        self._estimate = EmotionEstimate(
            old.label if confidence >= self.confidence_floor else None,
            old.valence * factor, 0.18 + (old.arousal - 0.18) * factor,
            confidence, old.source if confidence >= self.confidence_floor else "unknown",
            old.observed_at, old.evidence if confidence >= self.confidence_floor else (),
        )
        self._last_update = now

    def current(self, *, now: float | None = None) -> EmotionEstimate:
        now = float(self._clock() if now is None else now)
        self._decay(now)
        return self._estimate

    def strategy(self) -> str:
        return self._strategy

    def guidance(self) -> str:
        est = self.current()
        strategy = self._strategy
        instructions = {
            "listen_first": "用户明确表示只想倾诉或暂时不需要建议。先回应并倾听，不要立刻列解决步骤；最多问一个温和、开放的问题。",
            "concise": "用户明确偏好简短表达，优先用一两句回应。",
            "gentle_acknowledgment": "用户可能表达了较强情绪。温和承接其表达，不把情绪推断说成事实，不急着说教。",
            "balanced": "自然回应当前内容；不强行分析情绪，也不必每次都追问。",
        }
        mood = "情绪线索不明确"
        if est.confidence >= .6 and est.label:
            mood = f"用户表达出的情绪线索可能与 {est.label} 有关（置信度 {est.confidence:.2f}，仅为估计）"
        return f"{instructions[strategy]} {mood}。不要声称拥有真实的人类情绪；沉默不能解释为冷落、难过或孤独。"

    def snapshot(self) -> dict[str, Any]:
        return {"estimate": self.current().to_dict(), "strategy": self._strategy,
                "persistence": "volatile; no raw text or inferred mood is persisted"}

    @classmethod
    def _choose_strategy(cls, text: str) -> str:
        lower = text.casefold()
        if any(p.casefold() in lower for p in cls.NO_ADVICE):
            return "listen_first"
        if any(p.casefold() in lower for p in cls.BREVITY):
            return "concise"
        if any(p.casefold() in lower for p in cls.NEGATIVE + cls.FATIGUE):
            return "gentle_acknowledgment"
        return "balanced"
