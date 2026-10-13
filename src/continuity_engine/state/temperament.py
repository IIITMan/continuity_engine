"""Bounded temperament drift, separate from transient affect and user style preferences."""
from __future__ import annotations
import math
from typing import Any, Mapping

AXES = ("boldness", "curiosity", "affiliation", "sensitivity", "reactivity")

class Temperament:
    def __init__(self, *, traits: Mapping[str, float] | None = None, baseline: float = .5,
                 max_offset: float = .3, event_step: float = .08, enabled: bool = True,
                 birth_wall: float | None = None):
        if not 0 <= baseline <= 1 or not 0 <= max_offset <= .5 or not 0 <= event_step <= .3: raise ValueError("invalid temperament config")
        self.baseline, self.max_offset, self.event_step, self.enabled = float(baseline), float(max_offset), float(event_step), bool(enabled)
        self.traits = {k: float((traits or {}).get(k, baseline)) for k in AXES}
        if any(not math.isfinite(v) or not 0 <= v <= 1 for v in self.traits.values()): raise ValueError("traits must be in [0,1]")
        self.birth_wall = birth_wall
        self.event_count = 0

    @staticmethod
    def plasticity(age_weeks: float) -> float:
        if not math.isfinite(age_weeks) or age_weeks < 0: raise ValueError("age_weeks must be >= 0")
        if age_weeks < 1: return 1.0
        if age_weeks < 4: return .7
        if age_weeks < 12: return .5
        return .3

    def drift(self, axis: str, direction: float, *, now_wall: float, step: float | None = None) -> float:
        if axis not in self.traits: raise KeyError(f"unknown temperament axis: {axis}")
        if not math.isfinite(direction) or not -1 <= direction <= 1 or not math.isfinite(now_wall): raise ValueError("invalid drift input")
        actual_step = self.event_step if step is None else float(step)
        if not math.isfinite(actual_step) or not 0 <= actual_step <= self.event_step: raise ValueError("step exceeds configured per-event bound")
        if not self.enabled: return self.traits[axis]
        if self.birth_wall is None: self.birth_wall = now_wall
        age_weeks = max(0.0, (now_wall - self.birth_wall) / (7 * 86400))
        delta = direction * actual_step * self.plasticity(age_weeks)
        low, high = self.baseline - self.max_offset, self.baseline + self.max_offset
        self.traits[axis] = min(min(1.0, high), max(max(0.0, low), self.traits[axis] + delta))
        self.event_count += 1
        return self.traits[axis]

    def action_params(self, *, verbosity_base: float = .42, initiative_base: float = .35,
                      warmth_base: float = .68, curiosity_base: float = .66) -> dict[str, float | int]:
        """Pure, bounded expression/behavior projections; never authorize actions."""
        for name, value in (("verbosity_base", verbosity_base), ("initiative_base", initiative_base),
                            ("warmth_base", warmth_base), ("curiosity_base", curiosity_base)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0,1]")
        initiative_factor = .75 + float(initiative_base) * (0.25 / .35)
        affiliation_factor = 1.0 + 2.0 * (self.traits["affiliation"] - self.baseline)
        proactive_scale = max(.5, min(1.5, initiative_factor * affiliation_factor))
        exploration_scale = 1.0 + (float(curiosity_base) - .66) * .1 + (self.traits["curiosity"] - self.baseline) * .2
        return {
            "proactive_scale": proactive_scale,
            "verbosity_chars": int(round(80 + 320 * float(verbosity_base))),
            "warmth": max(0.0, min(1.0, float(warmth_base) + (self.traits["affiliation"] - self.baseline) * .15)),
            "exploration_threshold_scale": max(.9, min(1.1, exploration_scale)),
            "arousal_response_scale": max(.8, min(1.2, 1.0 + (self.traits["reactivity"] - self.baseline) * .4)),
            "startle_expression_scale": max(.75, min(1.25, 1.0 + (self.traits["sensitivity"] - self.baseline) * .5)),
        }

    def snapshot(self) -> dict[str, Any]:
        return {"traits": dict(self.traits), "baseline": self.baseline, "max_offset": self.max_offset,
                "event_step": self.event_step, "enabled": self.enabled, "birth_wall": self.birth_wall,
                "event_count": self.event_count}

    def restore(self, data: Mapping[str, Any]) -> None:
        traits = data.get("traits", {})
        checked = {k: float(traits.get(k, self.baseline)) for k in AXES}
        if any(not math.isfinite(v) or not 0 <= v <= 1 for v in checked.values()): raise ValueError("invalid persisted temperament")
        self.traits = checked; self.birth_wall = data.get("birth_wall"); self.event_count = max(0, int(data.get("event_count", 0)))
