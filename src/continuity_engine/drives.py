"""Normalized drive dynamics with explicit event impulses and bounded updates."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping

from .models import DriveState, Event


@dataclass(frozen=True, slots=True)
class DriveConfig:
    tau_s: float
    initial_value: float = 0.0
    initial_target: float = 0.0
    event_gain: float = 0.1

    def __post_init__(self) -> None:
        if not math.isfinite(self.tau_s) or self.tau_s <= 0:
            raise ValueError("tau_s must be finite and > 0")
        for name, value in (("initial_value", self.initial_value), ("initial_target", self.initial_target), ("event_gain", self.event_gain)):
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{name} must be finite and in [0,1]")


class DriveSystem:
    """Updates drives using a closed-form first-order response, stable for large dt."""

    def __init__(self, configs: Mapping[str, DriveConfig], now_mono: float = 0.0):
        self.configs = dict(configs)
        self.states: dict[str, DriveState] = {
            name: DriveState(cfg.initial_value, cfg.initial_target, 0.0, now_mono)
            for name, cfg in self.configs.items()
        }

    def update(self, dt: float, now_mono: float) -> dict[str, DriveState]:
        if not math.isfinite(dt) or dt < 0:
            raise ValueError("dt must be finite and >= 0")
        if not math.isfinite(now_mono):
            raise ValueError("now_mono must be finite")
        for name, state in self.states.items():
            cfg = self.configs[name]
            fraction = -math.expm1(-dt / cfg.tau_s) if dt else 0.0
            state.value = _clamp01(state.value + (state.target - state.value) * fraction)
            state.updated_at_mono = now_mono
            state.validate()
        return self.snapshot()

    def set_target(self, drive: str, target: float, urgency: float | None = None) -> None:
        if drive not in self.states:
            raise KeyError(f"unknown drive: {drive}")
        if not math.isfinite(target) or not 0 <= target <= 1:
            raise ValueError("target must be finite and in [0,1]")
        state = self.states[drive]
        state.target = target
        if urgency is not None:
            if not math.isfinite(urgency) or not 0 <= urgency <= 1:
                raise ValueError("urgency must be finite and in [0,1]")
            state.urgency = urgency

    def impulse(self, drive: str, delta: float, urgency_delta: float = 0.0) -> None:
        if drive not in self.states:
            raise KeyError(f"unknown drive: {drive}")
        if not math.isfinite(delta) or not math.isfinite(urgency_delta):
            raise ValueError("drive impulses must be finite")
        state = self.states[drive]
        # Event impulse changes the current level, not its long-term target;
        # the state then returns toward the target according to tau_s.
        state.value = _clamp01(state.value + delta)
        state.urgency = _clamp01(state.urgency + urgency_delta)
        state.validate()

    def apply_event(self, event: Event) -> None:
        if event.kind == "DRIVE_IMPULSE":
            drive = str(event.payload["drive"])
            delta = float(event.payload.get("delta", 0.0))
            urgency_delta = float(event.payload.get("urgency_delta", 0.0))
            self.impulse(drive, delta, urgency_delta)
        elif event.kind == "NOVEL_EVENT":
            self.impulse("exploration", 0.15 * (event.confidence if event.confidence is not None else 1.0), 0.05)

    def snapshot(self) -> dict[str, DriveState]:
        return {name: DriveState(s.value, s.target, s.urgency, s.updated_at_mono) for name, s in self.states.items()}


def _clamp01(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("non-finite drive result")
    return min(1.0, max(0.0, value))
