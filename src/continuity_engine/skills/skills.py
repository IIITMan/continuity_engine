"""Runtime-pluggable skills with explicit permissions and execution feedback."""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol

from ..core.models import validate_json_compatible

_SKILL_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,95}$")


class SkillStatus(str, Enum):
    OK = "OK"
    DENIED = "DENIED"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED = "FAILED"
    BUSY = "BUSY"


@dataclass(frozen=True, slots=True)
class SkillSpec:
    skill_id: str
    description: str
    required_permissions: tuple[str, ...] = ()
    enabled: bool = True
    max_concurrency: int = 1
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _SKILL_ID.fullmatch(self.skill_id):
            raise ValueError("skill_id must be a stable lowercase identifier")
        if not self.description.strip() or len(self.description) > 500:
            raise ValueError("description must contain 1..500 characters")
        if isinstance(self.max_concurrency, bool) or not isinstance(self.max_concurrency, int) or self.max_concurrency < 1:
            raise ValueError("max_concurrency must be an integer >= 1")
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be boolean")
        if any(not p or len(p) > 100 for p in self.required_permissions):
            raise ValueError("invalid required permission")
        validate_json_compatible(dict(self.metadata), "skill.metadata")


@dataclass(frozen=True, slots=True)
class SkillCall:
    skill_id: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    granted_permissions: frozenset[str] = frozenset()
    timeout_s: float = 30.0

    def __post_init__(self) -> None:
        validate_json_compatible(dict(self.arguments), "skill_call.arguments")
        if (not self.request_id or isinstance(self.timeout_s, bool)
                or not isinstance(self.timeout_s, (int, float)) or not 0 < self.timeout_s <= 3600):
            raise ValueError("request_id and timeout_s (0, 3600] are required")


@dataclass(frozen=True, slots=True)
class SkillResult:
    status: SkillStatus
    skill_id: str
    request_id: str
    data: Mapping[str, Any] = field(default_factory=dict)
    message: str | None = None
    error_code: str | None = None
    started_at_mono: float | None = None
    finished_at_mono: float | None = None

    def __post_init__(self) -> None:
        validate_json_compatible(dict(self.data), "skill_result.data")


class Skill(Protocol):
    async def execute(self, call: SkillCall) -> SkillResult | Mapping[str, Any]: ...


@dataclass
class _SkillEntry:
    spec: SkillSpec
    implementation: Skill
    semaphore: asyncio.Semaphore
    active: int = 0
    removing: bool = False


class SkillRegistry:
    """Host-owned registry. Discovery does not grant permission; active skills cannot unload."""

    def __init__(self, *, feedback_limit: int = 200):
        if feedback_limit < 1:
            raise ValueError("feedback_limit must be >= 1")
        self._entries: dict[str, _SkillEntry] = {}
        self._lock = asyncio.Lock()
        self._feedback: list[SkillResult] = []
        self._feedback_limit = feedback_limit

    async def register(self, spec: SkillSpec, implementation: Skill, *, replace_existing: bool = False) -> None:
        if not callable(getattr(implementation, "execute", None)):
            raise TypeError("skill implementation must provide execute(call)")
        async with self._lock:
            old = self._entries.get(spec.skill_id)
            if old is not None:
                if not replace_existing:
                    raise ValueError(f"skill already registered: {spec.skill_id}")
                if old.active:
                    raise RuntimeError("cannot replace a skill while executions are active")
            self._entries[spec.skill_id] = _SkillEntry(spec, implementation, asyncio.Semaphore(spec.max_concurrency))

    async def set_enabled(self, skill_id: str, enabled: bool) -> None:
        async with self._lock:
            entry = self._entries.get(skill_id)
            if entry is None:
                raise KeyError(skill_id)
            if entry.active and not enabled:
                raise RuntimeError("cannot disable a skill while executions are active")
            entry.spec = SkillSpec(entry.spec.skill_id, entry.spec.description, entry.spec.required_permissions,
                                   bool(enabled), entry.spec.max_concurrency, entry.spec.metadata)

    async def unregister(self, skill_id: str) -> bool:
        async with self._lock:
            entry = self._entries.get(skill_id)
            if entry is None:
                return False
            if entry.active:
                raise RuntimeError("cannot unload a skill while executions are active")
            del self._entries[skill_id]
            return True

    async def describe(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [{"skill_id": e.spec.skill_id, "description": e.spec.description,
                     "enabled": e.spec.enabled, "required_permissions": list(e.spec.required_permissions),
                     "active_executions": e.active, "metadata": dict(e.spec.metadata)}
                    for e in sorted(self._entries.values(), key=lambda x: x.spec.skill_id)]

    async def dispatch(self, call: SkillCall) -> SkillResult:
        started = time.monotonic()
        async with self._lock:
            entry = self._entries.get(call.skill_id)
            if entry is None or not entry.spec.enabled or entry.removing:
                result = SkillResult(SkillStatus.UNAVAILABLE, call.skill_id, call.request_id,
                                     error_code="SKILL_UNAVAILABLE", started_at_mono=started,
                                     finished_at_mono=time.monotonic())
                self._record(result)
                return result
            missing = set(entry.spec.required_permissions) - set(call.granted_permissions)
            if missing:
                result = SkillResult(SkillStatus.DENIED, call.skill_id, call.request_id,
                                     message="Required permission was not granted.", error_code="PERMISSION_DENIED",
                                     started_at_mono=started, finished_at_mono=time.monotonic())
                self._record(result)
                return result
            entry.active += 1
        try:
            async with entry.semaphore:
                value = await asyncio.wait_for(entry.implementation.execute(call), timeout=call.timeout_s)
            if isinstance(value, SkillResult):
                result = SkillResult(value.status, call.skill_id, call.request_id, value.data, value.message,
                                     value.error_code, started, time.monotonic())
            elif isinstance(value, Mapping):
                validate_json_compatible(dict(value), "skill.execute.result")
                result = SkillResult(SkillStatus.OK, call.skill_id, call.request_id, dict(value),
                                     started_at_mono=started, finished_at_mono=time.monotonic())
            else:
                result = SkillResult(SkillStatus.FAILED, call.skill_id, call.request_id,
                                     error_code="INVALID_SKILL_RESULT", started_at_mono=started,
                                     finished_at_mono=time.monotonic())
        except asyncio.TimeoutError:
            result = SkillResult(SkillStatus.FAILED, call.skill_id, call.request_id,
                                 error_code="SKILL_TIMEOUT", started_at_mono=started, finished_at_mono=time.monotonic())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result = SkillResult(SkillStatus.FAILED, call.skill_id, call.request_id,
                                 error_code=type(exc).__name__, started_at_mono=started, finished_at_mono=time.monotonic())
        finally:
            async with self._lock:
                entry.active = max(0, entry.active - 1)
        self._record(result)
        return result

    def _record(self, result: SkillResult) -> None:
        self._feedback.append(result)
        if len(self._feedback) > self._feedback_limit:
            del self._feedback[:len(self._feedback) - self._feedback_limit]

    def feedback_snapshot(self) -> list[SkillResult]:
        return list(self._feedback)
