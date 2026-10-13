"""Typed provider failures and a conservative failure taxonomy.

This module deliberately contains no HTTP client dependency: adapters raise these
types, and the reliability layer classifies both these types and generic transport
exceptions (``httpx``, ``asyncio.TimeoutError``, ``OSError``) into a small taxonomy
that drives retry/circuit decisions.

Error messages and attributes never contain credentials, prompts, or response text.
"""
from __future__ import annotations

from enum import Enum
from typing import Any


class FailureKind(str, Enum):
    """Coarse failure classes used for retry and circuit-breaker policy."""

    CONNECT = "CONNECT"            # no bytes reached the provider
    TIMEOUT = "TIMEOUT"            # outcome unknown: the provider may have processed it
    RATE_LIMIT = "RATE_LIMIT"      # 429 / provider quota signal
    SERVER = "SERVER"              # 5xx / provider-side transient fault
    AUTH = "AUTH"                  # 401/403: bad key, permanently failing until fixed
    REQUEST = "REQUEST"            # other 4xx: malformed or rejected request
    INVALID_OUTPUT = "INVALID_OUTPUT"  # provider answered, but the body failed the contract
    CANCELLED = "CANCELLED"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    UNKNOWN = "UNKNOWN"


#: Kinds that a controlled-retry policy may retry when the request is replay-safe.
RETRYABLE_KINDS: frozenset[FailureKind] = frozenset({
    FailureKind.CONNECT, FailureKind.TIMEOUT, FailureKind.RATE_LIMIT, FailureKind.SERVER,
})


class ProviderTransportError(RuntimeError):
    """Base class for typed provider transport failures (no secret payload text)."""

    kind: FailureKind = FailureKind.UNKNOWN

    def __init__(self, code: str, *, retry_after_s: float | None = None,
                 status_code: int | None = None) -> None:
        super().__init__(code)
        self.code = str(code)[:120]
        self.retry_after_s = retry_after_s
        self.status_code = status_code


class ProviderRateLimitError(ProviderTransportError):
    kind = FailureKind.RATE_LIMIT


class ProviderServerError(ProviderTransportError):
    kind = FailureKind.SERVER


class ProviderAuthError(ProviderTransportError):
    kind = FailureKind.AUTH


class ProviderRequestError(ProviderTransportError):
    kind = FailureKind.REQUEST


class ProviderConnectError(ProviderTransportError):
    kind = FailureKind.CONNECT


class ProviderTimeoutError(ProviderTransportError):
    """A transport read/write timeout whose outcome is unknown.

    It is explicitly *not* the same as a cancellation or a deadline enforced before
    the request was sent; the reliability layer treats it as "unknown outcome" and
    therefore only retries it when the request is declared replay-safe.
    """

    kind = FailureKind.TIMEOUT


class CircuitOpenError(ProviderTransportError):
    """Raised when the circuit breaker is open and the call is refused fast."""

    kind = FailureKind.CIRCUIT_OPEN


def _status_of(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


#: Timeout-shaped exceptions: the deadline elapsed with an unknown outcome.
_TIMEOUT_NAMES = frozenset({"ReadTimeout", "WriteTimeout", "ConnectTimeout", "PoolTimeout", "TimeoutException"})
#: Connection/stream-shaped exceptions: the transport failed at or below the socket
#: layer, so a bounded retry is safe and useful (a truncated read is recoverable).
_CONNECT_NAMES = frozenset({
    "ConnectError", "ConnectTransportError", "NetworkError", "RemoteProtocolError", "LocalProtocolError",
    "ReadError", "WriteError", "CloseError", "TransportError", "ProtocolError",
    "ConnectionError", "ConnectionResetError", "ConnectionAbortedError", "ConnectionRefusedError",
    "ConnectionDone", "BrokenPipeError", "IncompleteReadError",
})


def classify_provider_failure(exc: BaseException) -> FailureKind:
    """Map an exception to a failure kind without inspecting prompt/response text."""
    if isinstance(exc, ProviderTransportError):
        return exc.kind
    name = type(exc).__name__
    if isinstance(exc, TimeoutError) or name in _TIMEOUT_NAMES:
        return FailureKind.TIMEOUT
    status = _status_of(exc)
    if status == 429:
        return FailureKind.RATE_LIMIT
    if status in {401, 403}:
        return FailureKind.AUTH
    if isinstance(status, int) and status >= 500:
        return FailureKind.SERVER
    if isinstance(status, int) and status >= 400:
        return FailureKind.REQUEST
    if name in _CONNECT_NAMES:
        return FailureKind.CONNECT
    if isinstance(exc, OSError):
        return FailureKind.CONNECT
    if name in {"HTTPStatusError", "HTTPError"}:
        return FailureKind.UNKNOWN
    return FailureKind.UNKNOWN


def retry_after_seconds(exc: BaseException) -> float | None:
    """Best-effort Retry-After extraction from a typed error or an HTTP response."""
    value = getattr(exc, "retry_after_s", None)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    return parse_retry_after_header(headers.get("retry-after"))


def parse_retry_after_header(raw: Any) -> float | None:
    """Parse a Retry-After header value; only bounded numeric seconds are accepted."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        return None
    if seconds != seconds or seconds < 0 or seconds > 600:
        return None
    return seconds


__all__ = [
    "FailureKind", "RETRYABLE_KINDS", "ProviderTransportError", "ProviderRateLimitError",
    "ProviderServerError", "ProviderAuthError", "ProviderRequestError", "ProviderConnectError",
    "ProviderTimeoutError", "CircuitOpenError", "classify_provider_failure",
    "retry_after_seconds", "parse_retry_after_header",
]
