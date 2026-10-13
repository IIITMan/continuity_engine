"""Deterministic analytic breath oscillator with integrated first-order rate response."""
from __future__ import annotations
import math
from typing import Any, Mapping

class BreathModel:
    def __init__(self, *, base_bpm: float = 12.0, max_bpm: float = 24.0, sleep_bpm: float = 9.0,
                 min_depth: float = .4, max_depth: float = 1.0, arousal_tau_s: float = 4.0,
                 burst_tau_s: float = 20.0, enabled: bool = True):
        values = (base_bpm, max_bpm, sleep_bpm, min_depth, max_depth, arousal_tau_s, burst_tau_s)
        if not all(math.isfinite(v) for v in values) or base_bpm <= 0 or max_bpm < base_bpm or sleep_bpm <= 0 or not 0 <= min_depth <= max_depth <= 1 or arousal_tau_s <= 0 or burst_tau_s <= 0:
            raise ValueError("invalid breath parameters")
        self.base_bpm, self.max_bpm, self.sleep_bpm = base_bpm, max_bpm, sleep_bpm
        self.min_depth, self.max_depth, self.arousal_tau_s, self.burst_tau_s = min_depth, max_depth, arousal_tau_s, burst_tau_s
        self.enabled = bool(enabled); self.phase = 0.0; self.rate_bpm = base_bpm; self.depth = min_depth
        self.burst = 0.0; self._burst_impulse_this_frame = 0.0
        self.sleeping = False; self.wake_until_wall: float | None = None
        self._resume_sleep_after_wake = False

    def shock(self, intensity: float, *, now_wall: float | None = None, wake_hold_s: float = 300.0) -> None:
        if not math.isfinite(intensity) or not 0 <= intensity <= 1: raise ValueError("intensity must be in [0,1]")
        self.burst = max(self.burst, intensity)
        self._burst_impulse_this_frame = max(self._burst_impulse_this_frame, intensity)
        if intensity >= .55 and self.sleeping:
            if now_wall is not None:
                self.wake(now_wall=now_wall, hold_s=wake_hold_s)
            else:
                self.sleeping = False
                self.wake_until_wall = None
                self._resume_sleep_after_wake = True

    def wake(self, *, now_wall: float, hold_s: float = 300.0) -> None:
        if not math.isfinite(now_wall) or not math.isfinite(hold_s) or hold_s < 0: raise ValueError("invalid wake interval")
        was_sleeping = self.sleeping
        self.sleeping = False
        self.wake_until_wall = now_wall + hold_s if was_sleeping else None
        self._resume_sleep_after_wake = was_sleeping

    def set_sleeping(self, sleeping: bool) -> None:
        self.sleeping = bool(sleeping)
        if sleeping:
            self.wake_until_wall = None
            self._resume_sleep_after_wake = False

    def update(self, *, dt: float, arousal: float, now_wall: float | None = None) -> dict[str, float | bool]:
        if not math.isfinite(dt) or dt < 0 or not math.isfinite(arousal) or not 0 <= arousal <= 1: raise ValueError("invalid breath update")
        if now_wall is not None and self.wake_until_wall is not None and now_wall >= self.wake_until_wall:
            self.wake_until_wall = None
            if self._resume_sleep_after_wake:
                self.sleeping = True
            self._resume_sleep_after_wake = False
        if not self.enabled: return self.snapshot()
        goal = self.sleep_bpm if self.sleeping else self.base_bpm + (self.max_bpm - self.base_bpm) * arousal
        goal = min(self.max_bpm, max(self.sleep_bpm if self.sleeping else self.base_bpm, goal))
        old = self.rate_bpm
        decay = math.exp(-dt / self.arousal_tau_s) if dt else 1.0
        # Exact integral of goal + (old-goal)*exp(-t/tau), divided by 60 for cycles.
        integrated_bpm_s = goal * dt + (old - goal) * self.arousal_tau_s * (1.0 - decay)
        self.phase = (self.phase + integrated_bpm_s / 60.0) % 1.0
        self.rate_bpm = goal + (old - goal) * decay
        target_depth = self.max_depth if arousal >= .7 else self.min_depth + (self.max_depth - self.min_depth) * arousal
        if self.sleeping: target_depth = max(.55, target_depth)
        self.depth = target_depth
        decayed_burst = self.burst * (math.exp(-dt / self.burst_tau_s) if dt else 1.0)
        # A shock occurring in this engine tick remains observable in this tick even if
        # the tick integrates a long elapsed interval; decay begins on subsequent ticks.
        self.burst = max(decayed_burst, self._burst_impulse_this_frame)
        self._burst_impulse_this_frame = 0.0
        return self.snapshot()

    def snapshot(self) -> dict[str, float | bool]:
        return {"phase": self.phase, "rate_bpm": self.rate_bpm, "depth": self.depth, "burst": self.burst,
                "waveform": self.depth * math.sin(2 * math.pi * self.phase), "sleeping": self.sleeping,
                "wake_until_wall": self.wake_until_wall, "resume_sleep_after_wake": self._resume_sleep_after_wake}

    def restore(self, data: Mapping[str, Any]) -> None:
        phase, rate, depth, burst = (float(data.get(k, d)) for k, d in (("phase", 0), ("rate_bpm", self.base_bpm), ("depth", self.min_depth), ("burst", 0)))
        if not all(math.isfinite(v) for v in (phase, rate, depth, burst)) or not 0 <= phase < 1 or not self.sleep_bpm <= rate <= self.max_bpm or not self.min_depth <= depth <= self.max_depth or not 0 <= burst <= 1: raise ValueError("invalid breath state")
        self.phase, self.rate_bpm, self.depth, self.burst = phase, rate, depth, burst
        self._burst_impulse_this_frame = 0.0
        self.sleeping = bool(data.get("sleeping", False)); self.wake_until_wall = data.get("wake_until_wall")
        self._resume_sleep_after_wake = bool(data.get("resume_sleep_after_wake", False))
        if self.wake_until_wall is not None and (isinstance(self.wake_until_wall, bool) or not isinstance(self.wake_until_wall, (int, float)) or not math.isfinite(float(self.wake_until_wall))):
            raise ValueError("invalid persisted wake deadline")
