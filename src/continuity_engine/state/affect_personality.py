"""Bounded affect and personality learning for Continuity Engine.

Affect is transient and never authorizes actions. Personality changes only from
explicit, bounded preference feedback; raw dialogue is never persisted.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Mapping


_DEFAULT_TRAITS = {
    "warmth": 0.68,
    "curiosity": 0.66,
    "playfulness": 0.38,
    "directness": 0.62,
    "verbosity": 0.42,
    "initiative": 0.35,
}
_TRAIT_LABELS = {
    "warmth": "温暖亲切",
    "curiosity": "好奇但不盘问",
    "playfulness": "适度幽默",
    "directness": "直接清楚",
    "verbosity": "表达详略",
    "initiative": "主动程度",
}
_POSITIVE = ("谢谢", "太好了", "真棒", "喜欢", "开心", "哈哈", "有帮助", "不错")
_NEGATIVE = ("烦死", "难过", "糟糕", "生气", "失望", "别烦", "没用")


class AffectPersonality:
    """Small deterministic profile manager with atomic local persistence."""

    def __init__(self, *, enabled: bool = True, persistence_path: str | Path | None = "data/personality_state.json",
                 traits: Mapping[str, float] | None = None, affect: Mapping[str, float] | None = None):
        self.enabled = bool(enabled)
        self.persistence_path = Path(persistence_path) if persistence_path else None
        self._lock = threading.RLock()
        self.traits = dict(_DEFAULT_TRAITS)
        if traits:
            for key, value in traits.items():
                if key in self.traits:
                    self.traits[key] = self._unit(value, f"traits.{key}")
        self.affect = {"valence": 0.0, "arousal": 0.18, "social_energy": 0.55}
        if affect:
            for key in self.affect:
                if key in affect:
                    self.affect[key] = self._unit(affect[key], f"affect.{key}") if key != "valence" else self._signed(affect[key], f"affect.{key}")
        self.observations = 0
        self.updated_at = time.time()
        if self.persistence_path and self.persistence_path.exists():
            self._load()

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any]) -> "AffectPersonality":
        section = config.get("affect_personality", {}) if isinstance(config, Mapping) else {}
        if not isinstance(section, Mapping):
            section = {}
        return cls(enabled=section.get("enabled", True),
                   persistence_path=section.get("persistence_path", "data/personality_state.json"),
                   traits=section.get("initial_traits"), affect=section.get("initial_affect"))

    @staticmethod
    def _unit(value: Any, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"{name} must be finite and in [0,1]")
        return float(value)

    @staticmethod
    def _signed(value: Any, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not -1 <= value <= 1:
            raise ValueError(f"{name} must be finite and in [-1,1]")
        return float(value)

    def observe_user_message(self, text: str) -> None:
        """Update only transient affect from a message; never store the text."""
        if not self.enabled or not isinstance(text, str) or not text.strip():
            return
        sample = text.strip()[:2000]
        with self._lock:
            # Slowly relax toward neutral before applying a small evidence update.
            self.affect["valence"] *= 0.92
            self.affect["arousal"] = 0.18 + (self.affect["arousal"] - 0.18) * 0.86
            self.affect["social_energy"] = 0.55 + (self.affect["social_energy"] - 0.55) * 0.96
            if any(word in sample for word in _POSITIVE):
                self.affect["valence"] = min(1.0, self.affect["valence"] + 0.12)
                self.affect["social_energy"] = min(1.0, self.affect["social_energy"] + 0.04)
            if any(word in sample for word in _NEGATIVE):
                self.affect["valence"] = max(-1.0, self.affect["valence"] - 0.12)
                self.affect["arousal"] = min(1.0, self.affect["arousal"] + 0.08)
            if "!" in sample or "！" in sample or "？" in sample or "?" in sample:
                self.affect["arousal"] = min(1.0, self.affect["arousal"] + 0.025)
            self.observations += 1
            self.updated_at = time.time()
        # Clear style requests (e.g. “简洁点”) count as explicit feedback; general
        # mood or sentiment never changes the stable personality profile.
        self.record_preference_feedback(sample)

    def apply_transient_pulse(self, *, valence_delta: float = 0.0, arousal_delta: float = 0.0) -> bool:
        """Apply a small bounded event pulse without changing stable traits or persistence."""
        if (isinstance(valence_delta, bool) or not isinstance(valence_delta, (int, float))
                or not math.isfinite(float(valence_delta)) or abs(float(valence_delta)) > 0.25
                or isinstance(arousal_delta, bool) or not isinstance(arousal_delta, (int, float))
                or not math.isfinite(float(arousal_delta)) or abs(float(arousal_delta)) > 0.25):
            raise ValueError("transient affect pulse exceeds bounds")
        if not self.enabled:
            return False
        with self._lock:
            self.affect["valence"] = max(-1.0, min(1.0, self.affect["valence"] + float(valence_delta)))
            self.affect["arousal"] = max(0.0, min(1.0, self.affect["arousal"] + float(arousal_delta)))
            self.updated_at = time.time()
        return True

    def observe_emotion_assessment(self, assessment: Mapping[str, Any]) -> bool:
        """Apply a confidence-gated, transient estimate; it never changes stable traits.

        The model assessment is treated as uncertain evidence, not a fact about the user.
        Raw text/evidence is neither persisted nor copied into the personality profile.
        """
        if not self.enabled or not isinstance(assessment, Mapping):
            return False
        label = assessment.get("emotion")
        confidence = assessment.get("confidence")
        if label is None or confidence is None:
            return False
        if (not isinstance(label, str) or len(label) > 80 or isinstance(confidence, bool)
                or not isinstance(confidence, (int, float)) or not math.isfinite(float(confidence))
                or not 0.60 <= float(confidence) <= 1.0):
            return False
        emotion = label.strip().casefold().replace(" ", "_")
        # Only a small, explicit vocabulary is allowed to affect expression guidance.
        positive = {"joy", "happy", "happiness", "gratitude", "excited", "excitement", "开心", "喜悦", "感激", "兴奋"}
        negative = {"sad", "sadness", "anger", "angry", "fear", "anxiety", "anxious", "frustrated",
                    "frustration", "disappointed", "disappointment", "overwhelmed", "难过", "悲伤", "生气",
                    "愤怒", "害怕", "焦虑", "挫败", "失望", "压力大"}
        fatigue = {"tired", "fatigue", "fatigued", "exhausted", "疲惫", "累", "困"}
        calm = {"calm", "relaxed", "平静", "放松"}
        with self._lock:
            # Slowly relax old affect first so one estimate cannot dominate indefinitely.
            self.affect["valence"] *= 0.94
            self.affect["arousal"] = 0.18 + (self.affect["arousal"] - 0.18) * 0.90
            self.affect["social_energy"] = 0.55 + (self.affect["social_energy"] - 0.55) * 0.98
            strength = min(1.0, max(0.0, (float(confidence) - 0.60) / 0.40))
            if emotion in positive:
                self.affect["valence"] = min(1.0, self.affect["valence"] + 0.12 * strength)
                self.affect["social_energy"] = min(1.0, self.affect["social_energy"] + 0.03 * strength)
                self.affect["arousal"] = min(1.0, self.affect["arousal"] + 0.035 * strength)
            elif emotion in negative:
                self.affect["valence"] = max(-1.0, self.affect["valence"] - 0.12 * strength)
                self.affect["arousal"] = min(1.0, self.affect["arousal"] + 0.055 * strength)
            elif emotion in fatigue:
                self.affect["arousal"] = max(0.0, self.affect["arousal"] - 0.06 * strength)
                self.affect["social_energy"] = max(0.0, self.affect["social_energy"] - 0.04 * strength)
            elif emotion in calm:
                self.affect["arousal"] = max(0.0, self.affect["arousal"] - 0.05 * strength)
            else:
                return False
            self.observations += 1
            self.updated_at = time.time()
        return True

    def record_preference_feedback(self, feedback: str) -> dict[str, Any]:
        """Learn a small amount only from clear, explicit style preferences."""
        if not isinstance(feedback, str) or not feedback.strip() or len(feedback) > 1000:
            raise ValueError("feedback must contain 1..1000 characters")
        if not self.enabled:
            return self.snapshot()
        text = feedback.strip().lower()
        rules = (
            (("简洁点", "简短点", "少说一点", "别啰嗦", "更简洁"), "verbosity", -0.05),
            (("详细点", "展开说", "多解释", "说详细", "更详细"), "verbosity", 0.05),
            (("幽默点", "多开玩笑", "有趣一点", "活泼一点"), "playfulness", 0.05),
            (("严肃一点", "少开玩笑", "别开玩笑"), "playfulness", -0.05),
            (("热情一点", "亲切一点", "温柔一点"), "warmth", 0.05),
            (("冷静一点", "别太热情", "直接一点", "别客套"), "warmth", -0.05),
            (("多问问我", "多好奇一点"), "curiosity", 0.05),
            (("别总问我", "少问问题"), "curiosity", -0.05),
            (("主动一点", "可以主动提醒我"), "initiative", 0.05),
            (("别主动打扰", "不要主动找我"), "initiative", -0.05),
        )
        changed = False
        with self._lock:
            for phrases, trait, delta in rules:
                if any(phrase in text for phrase in phrases):
                    self.traits[trait] = min(1.0, max(0.0, self.traits[trait] + delta))
                    changed = True
                    break
            if changed:
                self.updated_at = time.time()
                self._save()
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "traits": dict(self.traits),
                "trait_labels": dict(_TRAIT_LABELS),
                "affect": dict(self.affect),
                "observations": self.observations,
                "updated_at": self.updated_at,
                "learning_policy": "explicit_style_feedback_only; bounded_step_0.05; no_raw_dialogue_storage",
            }

    def expression_guidance(self) -> str:
        if not self.enabled:
            return "Use a neutral, concise and respectful style."
        snap = self.snapshot()
        t, a = snap["traits"], snap["affect"]
        humor = "可以偶尔轻松幽默" if t["playfulness"] >= 0.55 else "不要刻意讲笑话"
        length = "优先简短回答" if t["verbosity"] < 0.35 else "按问题复杂度决定回答长度" if t["verbosity"] < 0.65 else "解释充分，但避免重复"
        warmth = "语气温暖自然" if t["warmth"] >= 0.55 else "语气平静克制"
        curiosity = "必要时提出一个有帮助的追问" if t["curiosity"] >= 0.6 else "避免不必要的追问"
        directness = "先给结论，再补充理由" if t["directness"] >= 0.6 else "先简短铺垫，再给出建议"
        initiative = "可以提出低打扰的下一步建议，但不能擅自执行" if t["initiative"] >= 0.6 else "除非用户提出请求，否则不要主动扩展任务"
        affect = "保持平静、体贴" if a["valence"] < -0.25 else "自然、轻快但不过度兴奋" if a["valence"] > 0.25 else "保持自然稳定"
        return (f"你具有稳定的机器人个性：{warmth}；{length}；{humor}；{curiosity}；{directness}；{initiative}。"
                f"当前表达状态：{affect}。风格只影响表达，不得改变事实标准、安全策略、权限、静默指令或动作许可。"
                "不要声称自己有真实的人类情绪、身体感受或未经证实的记忆。")

    def _load(self) -> None:
        try:
            data = json.loads(self.persistence_path.read_text(encoding="utf-8"))
            traits = data.get("traits", {})
            for key in self.traits:
                if key in traits:
                    self.traits[key] = self._unit(traits[key], f"traits.{key}")
            self.observations = max(0, int(data.get("observations", 0)))
            self.updated_at = float(data.get("updated_at", time.time()))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            # Corrupt profile fails back to conservative defaults, never blocks startup.
            self.traits = dict(_DEFAULT_TRAITS)
            self.observations = 0

    def _save(self) -> None:
        if self.persistence_path is None:
            return
        path = self.persistence_path
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"schema_version": 1, "traits": self.traits, "observations": self.observations,
                "updated_at": self.updated_at}
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
