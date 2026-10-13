"""Stateless view over DriveSystem snapshots; it never owns drive state."""
from __future__ import annotations
from dataclasses import dataclass
import math
from typing import Mapping
from ..core.models import DriveState

@dataclass(frozen=True, slots=True)
class FieldSnapshot:
    values: Mapping[str, float]
    action_waves: tuple[tuple[str, float], ...]
    top1: float
    top2: float
    struggle: float
    sleeping: bool = False

class DriveField:
    """Pure drive-field projection with low-activation and sleep gates."""
    def __init__(self, *, security_wave: str = "safety", low_activation: float = 0.45,
                 sleep_security_threshold: float = 0.45, sleep_struggle_cap: float = 0.30):
        for name, value in (("low_activation", low_activation), ("sleep_security_threshold", sleep_security_threshold), ("sleep_struggle_cap", sleep_struggle_cap)):
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0,1]")
        self.security_wave = security_wave
        self.low_activation = low_activation
        self.sleep_security_threshold = sleep_security_threshold
        self.sleep_struggle_cap = sleep_struggle_cap

    def snapshot(self, drives: Mapping[str, DriveState | float], *, sleeping: bool = False) -> FieldSnapshot:
        values: dict[str, float] = {}
        for name, state in drives.items():
            value = state.value if isinstance(state, DriveState) else float(state)
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"drive {name} must be finite and in [0,1]")
            values[str(name)] = value
        waves = sorted(((k, v) for k, v in values.items() if k != self.security_wave), key=lambda kv: (-kv[1], kv[0]))
        top1 = waves[0][1] if waves else 0.0
        top2 = waves[1][1] if len(waves) > 1 else 0.0
        # Weak waves are not a meaningful conflict even if their values are close.
        activation = min(1.0, min(top1, top2) / self.low_activation) if self.low_activation > 0 and len(waves) > 1 else 0.0
        # If both leading waves are sub-threshold, a close tie is low energy, not conflict.
        struggle = activation * (1.0 - abs(top1 - top2)) if len(waves) > 1 and top2 >= self.low_activation else 0.0
        security = values.get(self.security_wave, 0.0)
        if sleeping and security > self.sleep_security_threshold:
            struggle = min(struggle, self.sleep_struggle_cap)
        return FieldSnapshot(values=dict(values), action_waves=tuple(waves), top1=top1, top2=top2,
                             struggle=max(0.0, min(1.0, struggle)), sleeping=bool(sleeping))
