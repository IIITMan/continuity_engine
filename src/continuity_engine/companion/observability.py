"""Privacy-preserving companion runtime metrics (no prompts or raw transcripts)."""
from __future__ import annotations

from collections import Counter, deque
import math
import threading
import time
from typing import Any


class CompanionMetrics:
    """Bounded in-process metrics for latency, statuses and error codes only."""
    def __init__(self, *, max_latency_samples: int = 512, clock=time.monotonic):
        if isinstance(max_latency_samples, bool) or not isinstance(max_latency_samples, int) or not 16 <= max_latency_samples <= 10000:
            raise ValueError("max_latency_samples must be in [16,10000]")
        self.clock = clock
        self._lock = threading.Lock()
        self._statuses: Counter[str] = Counter()
        self._errors: Counter[str] = Counter()
        self._channels: Counter[str] = Counter()
        self._latencies = deque(maxlen=max_latency_samples)
        self._context_tokens = deque(maxlen=max_latency_samples)
        self._started_at = float(clock())

    def record(self, *, status: str, latency_s: float, channel: str, error_code: str | None = None,
               context_tokens: Any = None) -> None:
        latency = float(latency_s)
        if not math.isfinite(latency) or latency < 0:
            return
        try:
            token_value = int(context_tokens) if context_tokens is not None and not isinstance(context_tokens, bool) else None
            if token_value is not None and token_value < 0:
                token_value = None
        except (TypeError, ValueError, OverflowError):
            token_value = None
        with self._lock:
            self._statuses[str(status)[:80]] += 1
            self._channels[str(channel)[:30]] += 1
            if error_code:
                # Only stable code tokens, not exception text or inputs.
                code = str(error_code).split(":", 1)[0][:100]
                self._errors[code] += 1
            self._latencies.append(latency)
            if token_value is not None:
                self._context_tokens.append(token_value)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            values = sorted(self._latencies)
            n = len(values)
            context_values = sorted(self._context_tokens)
            cn = len(context_values)
            def cpct(p: float) -> int | None:
                if not cn:
                    return None
                return context_values[min(cn - 1, max(0, int(math.ceil(p * cn)) - 1))]
            def pct(p: float) -> float | None:
                if not n:
                    return None
                return values[min(n - 1, max(0, int(math.ceil(p * n)) - 1))]
            return {
                "uptime_s": max(0.0, float(self.clock()) - self._started_at),
                "conversation_count": sum(self._statuses.values()),
                "statuses": dict(self._statuses), "channels": dict(self._channels),
                "errors": dict(self._errors), "latency_samples": n,
                "latency_p50_s": pct(.50), "latency_p95_s": pct(.95),
                "variable_context_token_samples": cn,
                "variable_context_estimated_tokens_p50": cpct(.50),
                "variable_context_estimated_tokens_p95": cpct(.95),
                "token_estimates_are_approximate": True,
                "privacy": "no raw text, prompts, credentials, or audio stored",
            }

    def reset(self) -> None:
        with self._lock:
            self._statuses.clear(); self._errors.clear(); self._channels.clear(); self._latencies.clear(); self._context_tokens.clear()
            self._started_at = float(self.clock())
