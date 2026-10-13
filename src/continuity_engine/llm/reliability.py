"""Controlled retry, rate-limit handling, circuit breaking and fast recovery.

The gateway owns *semantic* concerns (purpose views, generation staleness, output
schema validation and bounded schema repair). This module owns *transport* concerns
and is deliberately a separate, opt-in adapter wrapper so that:

* deterministic tests and replay mode are unaffected;
* the retry decision can be made side-effect aware.

Replay rule
-----------
A retry re-sends a request whose outcome may be unknown. That is only acceptable when
re-sending cannot duplicate an external side effect. ``LLMRequest.replay_safe``
(default ``True``, because a chat completion is pure text generation) must be set to
``False`` by any caller whose request could trigger an externally visible effect
(e.g. a future adapter that performs server-side tool/function execution). For those
requests this wrapper never retries and never waits; the failure is surfaced
immediately with a ``retries_suppressed_no_replay`` counter so the host can reconcile
instead of silently replaying.
"""
from __future__ import annotations

import asyncio
import math
import random
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol

from .llm_gateway import LLMAdapter, LLMRequest
from .provider_errors import (
    CircuitOpenError, FailureKind, RETRYABLE_KINDS, classify_provider_failure,
    retry_after_seconds,
)


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded exponential backoff with jitter, honoring server rate-limit hints."""

    max_attempts: int = 3
    base_delay_s: float = 0.5
    max_delay_s: float = 8.0
    jitter_ratio: float = 0.25
    max_total_retry_wait_s: float = 20.0
    respect_retry_after: bool = True
    retry_kinds: frozenset[FailureKind] = field(default_factory=lambda: RETRYABLE_KINDS)

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int) or not 1 <= self.max_attempts <= 8:
            raise ValueError("max_attempts must be an integer in [1,8]")
        for name, value, low, high in (
            ("base_delay_s", self.base_delay_s, 0.0, 60.0),
            ("max_delay_s", self.max_delay_s, 0.0, 600.0),
            ("max_total_retry_wait_s", self.max_total_retry_wait_s, 0.0, 3600.0),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not low <= float(value) <= high:
                raise ValueError(f"{name} must be finite and in [{low},{high}]")
        if isinstance(self.jitter_ratio, bool) or not isinstance(self.jitter_ratio, (int, float)) or not math.isfinite(float(self.jitter_ratio)) or not 0.0 <= float(self.jitter_ratio) <= 1.0:
            raise ValueError("jitter_ratio must be finite and in [0,1]")
        if float(self.base_delay_s) > float(self.max_delay_s):
            raise ValueError("base_delay_s must not exceed max_delay_s")
        if not isinstance(self.retry_kinds, frozenset) or not self.retry_kinds <= set(FailureKind):
            raise ValueError("retry_kinds must be a frozenset of FailureKind")

    def retries(self, kind: FailureKind) -> bool:
        return kind in self.retry_kinds

    def to_dict(self) -> dict[str, Any]:
        return {"max_attempts": self.max_attempts, "base_delay_s": float(self.base_delay_s),
                "max_delay_s": float(self.max_delay_s), "jitter_ratio": float(self.jitter_ratio),
                "max_total_retry_wait_s": float(self.max_total_retry_wait_s),
                "respect_retry_after": bool(self.respect_retry_after),
                "retry_kinds": sorted(k.value for k in self.retry_kinds)}


def backoff_delay(attempt: int, policy: RetryPolicy, *, retry_after_s: float | None = None,
                  rng: random.Random | None = None) -> float:
    """Deterministic base backoff with bounded jitter; no jitter when jitter_ratio is 0."""
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    raw = float(policy.base_delay_s) * (2 ** (attempt - 1))
    delay = min(float(policy.max_delay_s), raw)
    if policy.jitter_ratio > 0:
        source = rng or random
        delay *= 1.0 + source.uniform(-float(policy.jitter_ratio), float(policy.jitter_ratio))
    if policy.respect_retry_after and isinstance(retry_after_s, (int, float)) and not isinstance(retry_after_s, bool):
        if math.isfinite(float(retry_after_s)) and float(retry_after_s) >= 0:
            delay = max(delay, min(float(retry_after_s), float(policy.max_delay_s)))
    return max(0.0, min(float(policy.max_delay_s), delay))


class CircuitBreaker:
    """Consecutive-failure circuit breaker with a single half-open probe.

    It is intentionally conservative: only failures routed through it open the
    circuit, and a single successful probe closes it again.
    """

    def __init__(self, *, failure_threshold: int = 5, recovery_timeout_s: float = 30.0,
                 half_open_max_calls: int = 1, clock=time.monotonic) -> None:
        if isinstance(failure_threshold, bool) or not isinstance(failure_threshold, int) or not 1 <= failure_threshold <= 100:
            raise ValueError("failure_threshold must be an integer in [1,100]")
        if isinstance(recovery_timeout_s, bool) or not isinstance(recovery_timeout_s, (int, float)) or not math.isfinite(float(recovery_timeout_s)) or not 0.05 <= float(recovery_timeout_s) <= 3600:
            raise ValueError("recovery_timeout_s must be finite and in [0.05,3600]")
        if isinstance(half_open_max_calls, bool) or not isinstance(half_open_max_calls, int) or not 1 <= half_open_max_calls <= 10:
            raise ValueError("half_open_max_calls must be an integer in [1,10]")
        self.failure_threshold = failure_threshold
        self.recovery_timeout_s = float(recovery_timeout_s)
        self.half_open_max_calls = half_open_max_calls
        self.clock = clock
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._half_open_inflight = 0
        self._state = CircuitState.CLOSED
        self.opened_events = 0
        self.rejected_while_open = 0

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._state_locked()

    def _state_locked(self) -> CircuitState:
        if self._state is CircuitState.OPEN and self._opened_at is not None:
            if float(self.clock()) - self._opened_at >= self.recovery_timeout_s:
                self._state = CircuitState.HALF_OPEN
                self._half_open_inflight = 0
        return self._state

    def allow(self) -> bool:
        """Return True if a call may proceed; a half-open state permits limited probes."""
        with self._lock:
            state = self._state_locked()
            if state is CircuitState.CLOSED:
                return True
            if state is CircuitState.OPEN:
                self.rejected_while_open += 1
                return False
            if self._half_open_inflight >= self.half_open_max_calls:
                self.rejected_while_open += 1
                return False
            self._half_open_inflight += 1
            return True

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._opened_at = None
            self._half_open_inflight = 0
            self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._state is CircuitState.HALF_OPEN or self._consecutive_failures >= self.failure_threshold:
                if self._state is not CircuitState.OPEN:
                    self.opened_events += 1
                self._state = CircuitState.OPEN
                self._opened_at = float(self.clock())
                self._half_open_inflight = 0

    def reset(self) -> None:
        with self._lock:
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._opened_at = None
            self._half_open_inflight = 0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"state": self._state_locked().value,
                    "consecutive_failures": self._consecutive_failures,
                    "failure_threshold": self.failure_threshold,
                    "recovery_timeout_s": self.recovery_timeout_s,
                    "opened_events": self.opened_events,
                    "rejected_while_open": self.rejected_while_open}


class ReliabilityMetrics:
    """Bounded counters only; never stores prompts, responses, or credentials."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests = 0
        self.attempts = 0
        self.successes = 0
        self.failures = 0
        self.retries = 0
        self.retries_suppressed_no_replay = 0
        self.retry_wait_s = 0.0
        self.rate_limit_waits_s = 0.0
        self.retry_after_observed_s = 0.0
        self.circuit_rejections = 0
        self.failures_by_kind: dict[str, int] = {}
        self.retries_by_kind: dict[str, int] = {}

    def _bump(self, mapping: dict[str, int], key: str) -> None:
        mapping[key] = mapping.get(key, 0) + 1

    def record_attempt(self) -> None:
        with self._lock:
            self.attempts += 1

    def record_request(self) -> None:
        with self._lock:
            self.requests += 1

    def record_success(self) -> None:
        with self._lock:
            self.successes += 1

    def record_failure(self, kind: FailureKind) -> None:
        with self._lock:
            self.failures += 1
            self._bump(self.failures_by_kind, kind.value)

    def record_retry(self, kind: FailureKind, wait_s: float) -> None:
        with self._lock:
            self.retries += 1
            self.retry_wait_s += max(0.0, float(wait_s))
            if kind is FailureKind.RATE_LIMIT:
                self.rate_limit_waits_s += max(0.0, float(wait_s))
            self._bump(self.retries_by_kind, kind.value)

    def record_suppressed_retry(self) -> None:
        with self._lock:
            self.retries_suppressed_no_replay += 1

    def record_retry_after(self, seconds: float) -> None:
        with self._lock:
            self.retry_after_observed_s += max(0.0, float(seconds))

    def record_circuit_rejection(self) -> None:
        with self._lock:
            self.circuit_rejections += 1

    def absorb(self, other: "ReliabilityMetrics") -> None:
        """Merge another metrics object (used to roll up per-drill counters)."""
        with self._lock:
            self.requests += other.requests
            self.attempts += other.attempts
            self.successes += other.successes
            self.failures += other.failures
            self.retries += other.retries
            self.retries_suppressed_no_replay += other.retries_suppressed_no_replay
            self.retry_wait_s += other.retry_wait_s
            self.rate_limit_waits_s += other.rate_limit_waits_s
            self.retry_after_observed_s += other.retry_after_observed_s
            self.circuit_rejections += other.circuit_rejections
            for key, value in other.failures_by_kind.items():
                self._add_count(self.failures_by_kind, key, value)
            for key, value in other.retries_by_kind.items():
                self._add_count(self.retries_by_kind, key, value)

    def _add_count(self, mapping: dict[str, int], key: str, value: int) -> None:
        if value:
            mapping[key] = mapping.get(key, 0) + int(value)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"requests": self.requests, "attempts": self.attempts, "successes": self.successes,
                    "failures": self.failures, "retries": self.retries,
                    "retries_suppressed_no_replay": self.retries_suppressed_no_replay,
                    "retry_wait_s": round(self.retry_wait_s, 6),
                    "rate_limit_waits_s": round(self.rate_limit_waits_s, 6),
                    "retry_after_observed_s": round(self.retry_after_observed_s, 6),
                    "circuit_rejections": self.circuit_rejections,
                    "failures_by_kind": dict(self.failures_by_kind),
                    "retries_by_kind": dict(self.retries_by_kind),
                    "privacy": "counters only; no prompts, responses, or credentials"}


class _AsyncCloseable(Protocol):
    async def aclose(self) -> None: ...


class ReliableLLMAdapter:
    """Adapter wrapper adding controlled retry, rate-limit backoff and a circuit breaker.

    It forwards ``generate`` to a delegate adapter and only retries transport-level
    failures. Provider output that fails the schema contract raises ``ProviderResponseError``
    from the delegate and is *not* retried here; the gateway's bounded schema-repair loop
    owns that case.
    """

    is_replay_adapter = False

    def __init__(self, delegate: LLMAdapter, *, policy: RetryPolicy | None = None,
                 breaker: CircuitBreaker | None = None, metrics: ReliabilityMetrics | None = None,
                 clock=time.monotonic, sleep=asyncio.sleep, rng: random.Random | None = None) -> None:
        self.delegate = delegate
        self.policy = policy or RetryPolicy()
        self.breaker = breaker or CircuitBreaker(clock=clock)
        self.metrics = metrics or ReliabilityMetrics()
        self.clock = clock
        self._sleep = sleep
        self._rng = rng

    # -- introspection ---------------------------------------------------------
    @property
    def model(self) -> str | None:
        return getattr(self.delegate, "model", None)

    def snapshot(self) -> dict[str, Any]:
        return {"policy": self.policy.to_dict(), "circuit": self.breaker.snapshot(),
                "metrics": self.metrics.snapshot(),
                "delegate": type(self.delegate).__name__}

    # -- delivery --------------------------------------------------------------
    async def generate(self, request: LLMRequest) -> Mapping[str, Any]:
        replay_safe = bool(getattr(request, "replay_safe", True))
        self.metrics.record_request()
        spent_wait = 0.0
        last_error: BaseException | None = None
        for attempt in range(1, self.policy.max_attempts + 1):
            if not self.breaker.allow():
                self.metrics.record_circuit_rejection()
                raise CircuitOpenError("circuit_open", status_code=None)
            self.metrics.record_attempt()
            try:
                payload = await self.delegate.generate(request)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - classification decides retryability
                kind = classify_provider_failure(exc)
                self.metrics.record_failure(kind)
                if kind not in {FailureKind.INVALID_OUTPUT, FailureKind.REQUEST, FailureKind.AUTH}:
                    self.breaker.record_failure()
                last_error = exc
                if not self.policy.retries(kind):
                    raise
                if not replay_safe:
                    # A possibly side-effecting request must never be silently replayed.
                    self.metrics.record_suppressed_retry()
                    raise
                if attempt >= self.policy.max_attempts:
                    raise
                hint = retry_after_seconds(exc)
                if hint is not None:
                    self.metrics.record_retry_after(hint)
                wait = backoff_delay(attempt, self.policy, retry_after_s=hint, rng=self._rng)
                if spent_wait + wait > float(self.policy.max_total_retry_wait_s):
                    raise
                remaining = float(request.deadline_mono) - float(self.clock())
                if remaining <= 0 or wait >= remaining:
                    # Sleeping would only guarantee a deadline miss; fail fast instead.
                    raise
                self.metrics.record_retry(kind, wait)
                spent_wait += wait
                await self._sleep(wait)
            else:
                self.breaker.record_success()
                self.metrics.record_success()
                return payload
        raise last_error if last_error is not None else CircuitOpenError("circuit_open")

    async def aclose(self) -> None:
        closer = getattr(self.delegate, "aclose", None)
        if closer is not None:
            await closer()


__all__ = [
    "CircuitState", "RetryPolicy", "CircuitBreaker", "ReliabilityMetrics",
    "ReliableLLMAdapter", "backoff_delay",
]
