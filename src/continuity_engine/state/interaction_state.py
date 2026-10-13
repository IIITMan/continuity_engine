"""Bounded conversational state without an independent short-term-goal system.

The host decides whether to set or clear a current interaction strategy. LLMs may
interpret signals, but signals never automatically activate a strategy. Raw dialogue
is not stored here.
"""
from __future__ import annotations

import math
import re
import threading
import time
from typing import Any, Mapping

_SIGNAL_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SOURCES = {"engine_decision", "explicit_user_preference", "host"}
_MAX_SIGNALS = 32
_MAX_TTL_S = 86_400.0


class InteractionStateError(ValueError):
    """Raised when interaction-state input exceeds its reviewed contract."""


class InteractionState:
    """Thread-safe, in-memory interaction signals and one current strategy.

    This is deliberately not a goal manager: there are no candidate goals, priority
    comparisons, goal instances, or goal lifecycle transitions. The host owns policy
    decisions and explicitly changes the current strategy when appropriate.
    """

    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.RLock()
        self._signals: dict[str, dict[str, Any]] = {}
        self._strategy: dict[str, Any] | None = None
        self._version = 0

    @staticmethod
    def _text(value: Any, name: str, limit: int) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise InteractionStateError(f"{name} must be non-empty text up to {limit} characters")
        if any(ord(ch) < 32 and ch not in "\t\n\r" for ch in value):
            raise InteractionStateError(f"{name} contains control characters")
        return " ".join(value.split())

    @staticmethod
    def _ttl(value: float | None, *, optional: bool = False) -> float | None:
        if value is None and optional:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not 1 <= float(value) <= _MAX_TTL_S:
            raise InteractionStateError(f"ttl_s must be between 1 and {_MAX_TTL_S:g} seconds")
        return float(value)

    def observe_signal(
        self, name: str, value: str | bool | int | float, *, confidence: float = 1.0,
        source: str = "host", ttl_s: float = 300.0,
    ) -> dict[str, Any]:
        """Record a bounded, non-verbatim signal; same-name observations replace old ones."""
        if not isinstance(name, str) or not _SIGNAL_NAME.fullmatch(name):
            raise InteractionStateError("signal name must match [a-z][a-z0-9_.-]{0,63}")
        if isinstance(value, str):
            value = self._text(value, "signal value", 100)
        elif isinstance(value, bool):
            pass
        elif isinstance(value, int):
            if abs(value) > 1_000_000_000:
                raise InteractionStateError("integer signal value is out of range")
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise InteractionStateError("signal value must be finite")
        else:
            raise InteractionStateError("signal value must be a string, boolean, integer, or finite number")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(float(confidence)) or not 0 <= float(confidence) <= 1:
            raise InteractionStateError("confidence must be finite and in [0,1]")
        if source not in _SOURCES:
            raise InteractionStateError(f"source must be one of {sorted(_SOURCES)}")
        ttl = self._ttl(ttl_s)
        now = self._clock()
        with self._lock:
            self._expire_locked(now)
            if name not in self._signals and len(self._signals) >= _MAX_SIGNALS:
                # Evict the oldest observation rather than growing without bound.
                oldest = min(self._signals, key=lambda key: self._signals[key]["observed_at_mono"])
                del self._signals[oldest]
            self._signals[name] = {
                "name": name, "value": value, "confidence": float(confidence),
                "source": source, "observed_at_mono": now, "expires_at_mono": now + ttl,
            }
            self._version += 1
            return self._public_signal(self._signals[name], now)

    def clear_signal(self, name: str) -> bool:
        with self._lock:
            removed = self._signals.pop(name, None) is not None
            if removed:
                self._version += 1
            return removed

    def set_strategy(
        self, name: str, instruction: str, *, source: str = "engine_decision",
        ttl_s: float | None = None, reason_code: str = "updated_by_engine",
    ) -> dict[str, Any]:
        """Set the approved current interaction strategy; no priority competition occurs."""
        clean_name = self._text(name, "strategy name", 80)
        clean_instruction = self._text(instruction, "strategy instruction", 600)
        if source not in _SOURCES:
            raise InteractionStateError(f"source must be one of {sorted(_SOURCES)}")
        ttl = self._ttl(ttl_s, optional=True)
        clean_reason = self._text(reason_code, "reason_code", 80)
        now = self._clock()
        with self._lock:
            self._strategy = {
                "name": clean_name, "instruction": clean_instruction, "source": source,
                "reason_code": clean_reason, "updated_at_mono": now,
                "expires_at_mono": None if ttl is None else now + ttl,
            }
            self._version += 1
            return self._public_strategy(now) or {}

    def clear_strategy(self, *, reason_code: str = "no_longer_applicable") -> bool:
        """Clear the current strategy explicitly; a cleared instance is not retained."""
        self._text(reason_code, "reason_code", 80)
        with self._lock:
            if self._strategy is None:
                return False
            self._strategy = None
            self._version += 1
            return True

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            changed = self._expire_locked(now)
            if changed:
                self._version += 1
            signals = [self._public_signal(item, now) for item in self._signals.values()]
            signals.sort(key=lambda item: item["name"])
            return {
                "schema_version": 1,
                "version": self._version,
                "current_strategy": self._public_strategy(now),
                "signals": signals,
                "raw_dialogue_stored": False,
            }

    def _expire_locked(self, now: float) -> bool:
        expired = [key for key, value in self._signals.items() if value["expires_at_mono"] <= now]
        for key in expired:
            del self._signals[key]
        strategy_expired = self._strategy is not None and self._strategy["expires_at_mono"] is not None and self._strategy["expires_at_mono"] <= now
        if strategy_expired:
            self._strategy = None
        return bool(expired) or strategy_expired

    @staticmethod
    def _public_signal(item: Mapping[str, Any], now: float) -> dict[str, Any]:
        return {
            "name": item["name"], "value": item["value"], "confidence": item["confidence"],
            "source": item["source"], "age_s": round(max(0.0, now - item["observed_at_mono"]), 3),
        }

    def _public_strategy(self, now: float) -> dict[str, Any] | None:
        item = self._strategy
        if item is None:
            return None
        return {
            "name": item["name"], "instruction": item["instruction"], "source": item["source"],
            "reason_code": item["reason_code"],
            "age_s": round(max(0.0, now - item["updated_at_mono"]), 3),
            "expires_in_s": None if item["expires_at_mono"] is None else round(max(0.0, item["expires_at_mono"] - now), 3),
        }
