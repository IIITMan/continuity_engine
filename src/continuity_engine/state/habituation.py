"""Small, persistent habituation for expressive startle only.

It never suppresses a sensor event or changes safety classification. Repeated known
stimuli retain a non-zero expressive response; the first response is full strength.
"""
from __future__ import annotations

import math
from typing import Any, Mapping

_SUPPORTED = {"unexpected_sound", "sudden_appearance"}


class HabituationTracker:
    def __init__(self, *, enabled: bool = True, repeat_scale: float = 0.25, max_event_types: int = 32):
        if not math.isfinite(repeat_scale) or not 0.05 <= repeat_scale <= 1.0:
            raise ValueError("repeat_scale must be in [0.05,1]")
        if isinstance(max_event_types, bool) or not isinstance(max_event_types, int) or not 1 <= max_event_types <= 256:
            raise ValueError("max_event_types must be in [1,256]")
        self.enabled = bool(enabled)
        self.repeat_scale = float(repeat_scale)
        self.max_event_types = max_event_types
        self.counts: dict[str, int] = {}

    def expressive_intensity(self, event_type: str, intensity: float) -> tuple[float, bool]:
        if event_type not in _SUPPORTED:
            raise ValueError("unsupported habituation event type")
        if isinstance(intensity, bool) or not isinstance(intensity, (int, float)) or not math.isfinite(float(intensity)) or not 0 <= intensity <= 1:
            raise ValueError("intensity must be in [0,1]")
        if not self.enabled:
            return float(intensity), False
        if event_type not in self.counts and len(self.counts) >= self.max_event_types:
            # Bounded fail-safe: preserve full expression rather than suppressing a new type.
            return float(intensity), False
        count = self.counts.get(event_type, 0)
        familiar = count > 0
        self.counts[event_type] = min(1_000_000, count + 1)
        return float(intensity) * (self.repeat_scale if familiar else 1.0), familiar

    def snapshot(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "repeat_scale": self.repeat_scale,
                "max_event_types": self.max_event_types, "counts": dict(self.counts)}

    def restore(self, data: Mapping[str, Any]) -> None:
        counts = data.get("counts", {})
        if not isinstance(counts, Mapping) or len(counts) > self.max_event_types:
            raise ValueError("invalid habituation counts")
        checked: dict[str, int] = {}
        for key, value in counts.items():
            if key not in _SUPPORTED or isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000_000:
                raise ValueError("invalid habituation entry")
            checked[key] = value
        scale = float(data.get("repeat_scale", self.repeat_scale))
        if not math.isfinite(scale) or not 0.05 <= scale <= 1.0:
            raise ValueError("invalid habituation scale")
        self.counts = checked
        self.repeat_scale = scale
        self.enabled = bool(data.get("enabled", self.enabled))
