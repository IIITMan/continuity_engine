"""Clock abstractions make simulation and event replay deterministic."""
from __future__ import annotations

from dataclasses import dataclass
import time


class Clock:
    def monotonic(self) -> float:
        raise NotImplementedError

    def wall_time(self) -> float:
        raise NotImplementedError


class SystemClock(Clock):
    def monotonic(self) -> float:
        return time.monotonic()

    def wall_time(self) -> float:
        return time.time()


@dataclass
class ReplayClock(Clock):
    mono: float = 0.0
    wall: float = 1_800_000_000.0

    def monotonic(self) -> float:
        return self.mono

    def wall_time(self) -> float:
        return self.wall

    def advance(self, dt: float) -> None:
        if dt < 0:
            raise ValueError("replay clock cannot move backward")
        self.mono += dt
        self.wall += dt

    def set_wall(self, wall: float) -> None:
        """Wall time can jump in tests; monotonic time remains independent."""
        self.wall = wall
