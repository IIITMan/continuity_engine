"""Core immutable DTOs and enums for Continuity Engine.

No model in this module performs I/O or invokes external services.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum
import math
from types import MappingProxyType
from typing import Any, Mapping

CURRENT_EVENT_SCHEMA = "1.0"


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, set):
        return frozenset(_freeze(v) for v in value)
    return value


def thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, frozenset)):
        return [thaw(v) for v in value]
    if isinstance(value, Enum):
        return value.value
    return value


def validate_json_compatible(value: Any, path: str = "payload") -> None:
    """Reject unsupported objects and non-finite floats before data enters state/logs."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite float")
        return
    if isinstance(value, Mapping):
        for key, inner in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} keys must be strings")
            validate_json_compatible(inner, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for idx, inner in enumerate(value):
            validate_json_compatible(inner, f"{path}[{idx}]")
        return
    if isinstance(value, Enum):
        return
    raise TypeError(f"{path} contains unsupported type {type(value).__name__}")


class Priority(IntEnum):
    LOW = 10
    NORMAL = 50
    HIGH = 80
    CRITICAL = 100


class EventKind(str, Enum):
    SAFETY_STOP = "SAFETY_STOP"
    SAFETY_RESET = "SAFETY_RESET"
    USER_STOP = "USER_STOP"
    USER_CANCEL = "USER_CANCEL"
    USER_MUTE = "USER_MUTE"
    USER_UNMUTE = "USER_UNMUTE"
    ACTION_TERMINAL = "ACTION_TERMINAL"
    USER_MESSAGE = "USER_MESSAGE"
    INTENT_ACTION_READY = "INTENT_ACTION_READY"
    WORLD_OBSERVATION = "WORLD_OBSERVATION"
    DRIVE_IMPULSE = "DRIVE_IMPULSE"
    NOVEL_EVENT = "NOVEL_EVENT"
    TIMER = "TIMER"
    USER_QUIET = "USER_QUIET"
    USER_SPEECH_STARTED = "USER_SPEECH_STARTED"
    USER_SPEECH_ENDED = "USER_SPEECH_ENDED"
    CONFIG_RELOAD = "CONFIG_RELOAD"
    UNKNOWN = "UNKNOWN"


CONTROL_EVENT_KINDS = frozenset({
    EventKind.SAFETY_STOP.value,
    EventKind.SAFETY_RESET.value,
    EventKind.USER_STOP.value,
    EventKind.USER_CANCEL.value,
    EventKind.USER_MUTE.value,
    EventKind.USER_UNMUTE.value,
    EventKind.USER_QUIET.value,
    EventKind.USER_SPEECH_STARTED.value,
    EventKind.USER_SPEECH_ENDED.value,
    EventKind.ACTION_TERMINAL.value,
})
NO_SILENT_DROP_EVENT_KINDS = frozenset({EventKind.USER_MESSAGE.value, EventKind.INTENT_ACTION_READY.value})


@dataclass(frozen=True, slots=True)
class Event:
    event_id: str
    kind: str
    source: str
    timestamp_mono: float
    timestamp_wall: float
    payload: Mapping[str, Any] = field(default_factory=dict)
    event_schema_version: str = CURRENT_EVENT_SCHEMA
    confidence: float | None = None
    priority: int = int(Priority.NORMAL)
    expires_at_mono: float | None = None
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        if not self.event_id:
            raise ValueError("event_id must be non-empty")
        if not self.kind:
            raise ValueError("kind must be non-empty")
        if not self.source:
            raise ValueError("source must be non-empty")
        if self.event_schema_version != CURRENT_EVENT_SCHEMA:
            raise ValueError(f"runtime Event must use current schema {CURRENT_EVENT_SCHEMA}; migrate persisted events explicitly")
        if not isinstance(self.priority, int) or isinstance(self.priority, bool) or not 0 <= self.priority <= 100:
            raise ValueError("priority must be an integer in [0,100]")
        for name, value in (("timestamp_mono", self.timestamp_mono), ("timestamp_wall", self.timestamp_wall)):
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if self.expires_at_mono is not None and not math.isfinite(self.expires_at_mono):
            raise ValueError("expires_at_mono must be finite")
        if self.confidence is not None and (not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1):
            raise ValueError("confidence must be finite and in [0, 1]")
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload must be a mapping")
        validate_json_compatible(self.payload, "event.payload")
        object.__setattr__(self, "payload", _freeze(self.payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "kind": self.kind,
            "source": self.source,
            "timestamp_mono": self.timestamp_mono,
            "timestamp_wall": self.timestamp_wall,
            "payload": thaw(self.payload),
            "event_schema_version": self.event_schema_version,
            "confidence": self.confidence,
            "priority": int(self.priority),
            "expires_at_mono": self.expires_at_mono,
            "correlation_id": self.correlation_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Event":
        version = data.get("event_schema_version")
        if version is None:
            raise ValueError("event_schema_version is required; migrate legacy data explicitly")
        return cls(
            event_id=str(data["event_id"]), kind=str(data["kind"]), source=str(data["source"]),
            timestamp_mono=float(data["timestamp_mono"]), timestamp_wall=float(data["timestamp_wall"]),
            payload=data.get("payload", {}), event_schema_version=str(version),
            confidence=None if data.get("confidence") is None else float(data["confidence"]),
            priority=int(data.get("priority", Priority.NORMAL)),
            expires_at_mono=None if data.get("expires_at_mono") is None else float(data["expires_at_mono"]),
            correlation_id=data.get("correlation_id"),
        )


class WorldStatus(str, Enum):
    CONFIRMED = "CONFIRMED"
    PROBABLE = "PROBABLE"
    UNKNOWN = "UNKNOWN"
    STALE = "STALE"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True, slots=True)
class Evidence:
    key: str
    value: Any
    confidence: float
    source: str
    observed_at_mono: float
    expires_at_mono: float
    event_id: str


@dataclass(frozen=True, slots=True)
class WorldFact:
    key: str
    value: Any
    status: WorldStatus
    confidence: float
    sources: tuple[str, ...]
    updated_at_mono: float
    expires_at_mono: float
    evidence_event_ids: tuple[str, ...] = ()


@dataclass(slots=True)
class DriveState:
    value: float = 0.0
    target: float = 0.0
    urgency: float = 0.0
    updated_at_mono: float = 0.0

    def validate(self) -> None:
        for name, value in (("value", self.value), ("target", self.target), ("urgency", self.urgency)):
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"drive {name} must be finite and in [0,1]")
        if not math.isfinite(self.updated_at_mono):
            raise ValueError("updated_at_mono must be finite")


class IntentStatus(str, Enum):
    PROPOSED = "PROPOSED"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    INTERRUPTED = "INTERRUPTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


TERMINAL_INTENT_STATUSES = frozenset({IntentStatus.SUCCEEDED, IntentStatus.FAILED, IntentStatus.CANCELLED, IntentStatus.EXPIRED})


@dataclass(slots=True)
class Intent:
    intent_id: str
    intent_type: str
    goal: str
    status: IntentStatus
    score: float
    created_at_mono: float
    started_at_mono: float
    reason: str
    action_type: str | None = None
    action_args: dict[str, Any] = field(default_factory=dict)
    deadline_mono: float | None = None
    clarification_rounds: int = 0
    clarification_questions: list[str] = field(default_factory=list)
    generation: int = 0
    cancellation_requested: bool = False
    interruption_policy: str = "REPLAN"
    ordinary_proactive: bool = False
    is_safety_alert: bool = False


@dataclass(frozen=True, slots=True)
class IntentCandidate:
    intent_type: str
    goal: str
    score: float
    reason: str
    action_type: str | None = None
    action_args: Mapping[str, Any] = field(default_factory=dict)
    urgency: float = 0.0
    risk_level: str = "low"
    deadline_mono: float | None = None
    interruption_policy: str = "REPLAN"
    requires_clarification: bool = False
    ordinary_proactive: bool = False
    is_safety_alert: bool = False

    def __post_init__(self) -> None:
        if not self.intent_type or not self.goal:
            raise ValueError("candidate intent_type and goal must be non-empty")
        for name, val in (("score", self.score), ("urgency", self.urgency)):
            if not math.isfinite(val) or not 0.0 <= val <= 1.0:
                raise ValueError(f"{name} must be in [0,1]")
        if self.deadline_mono is not None and not math.isfinite(self.deadline_mono):
            raise ValueError("deadline_mono must be finite")
        if not self.reason or not self.risk_level:
            raise ValueError("candidate reason and risk_level must be non-empty")
        validate_json_compatible(self.action_args, "intent_candidate.action_args")
        object.__setattr__(self, "action_args", _freeze(self.action_args))


class ActionStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ActionProposal:
    action_id: str
    intent_id: str
    action_type: str
    args: Mapping[str, Any]
    created_at_mono: float
    deadline_mono: float | None = None
    ordinary_proactive: bool = False
    is_safety_alert: bool = False
    reason_code: str = "intent_selected"
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if not self.action_id or not self.intent_id or not self.action_type:
            raise ValueError("action proposal requires action_id, intent_id and action_type")
        if not math.isfinite(self.created_at_mono):
            raise ValueError("action proposal created_at_mono must be finite")
        if self.deadline_mono is not None and not math.isfinite(self.deadline_mono):
            raise ValueError("action proposal deadline must be finite")
        validate_json_compatible(self.args, "action_proposal.args")
        object.__setattr__(self, "args", _freeze(self.args))

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id, "intent_id": self.intent_id,
            "action_type": self.action_type, "args": thaw(self.args),
            "created_at_mono": self.created_at_mono, "deadline_mono": self.deadline_mono,
            "ordinary_proactive": self.ordinary_proactive, "is_safety_alert": self.is_safety_alert,
            "reason_code": self.reason_code, "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True, slots=True)
class ActionResult:
    action_id: str
    intent_id: str
    status: ActionStatus
    completed_at_mono: float | None = None
    error_code: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.action_id or not self.intent_id:
            raise ValueError("action result requires action_id and intent_id")
        if not isinstance(self.status, ActionStatus):
            raise ValueError("action result status must be an ActionStatus")
        if self.completed_at_mono is not None and not math.isfinite(self.completed_at_mono):
            raise ValueError("completed_at_mono must be finite")
        validate_json_compatible(self.details, "action_result.details")
        object.__setattr__(self, "details", _freeze(self.details))


@dataclass(slots=True)
class EngineState:
    state_version: int = 0
    config_version: int = 1
    world: dict[str, WorldFact] = field(default_factory=dict)
    drives: dict[str, DriveState] = field(default_factory=dict)
    current_intent: Intent | None = None
    quiet_scope: str | None = None
    quiet_until_wall: float | None = None
    output_muted: bool = False
    user_speaking: bool = False
    safety_stopped: bool = False
    last_user_message: str | None = None
    generation_by_interaction: dict[str, int] = field(default_factory=dict)
    last_reason: str = "initialized"

    def validate(self) -> None:
        if self.state_version < 0 or self.config_version < 1:
            raise ValueError("invalid state/config version")
        for d in self.drives.values():
            d.validate()
        for key, fact in self.world.items():
            if key != fact.key or not isinstance(fact.status, WorldStatus):
                raise ValueError(f"world fact key/status mismatch for {key}")
            if not math.isfinite(fact.confidence) or not 0 <= fact.confidence <= 1:
                raise ValueError(f"world fact {key} confidence must be finite and in [0,1]")
            if not math.isfinite(fact.updated_at_mono) or not math.isfinite(fact.expires_at_mono):
                raise ValueError(f"world fact {key} timestamps must be finite")
            validate_json_compatible(fact.value, f"world.{key}.value")
        if self.quiet_scope not in {None, "TURN", "SESSION", "PERSISTENT"}:
            raise ValueError("quiet_scope must be TURN, SESSION, PERSISTENT or None")
        if self.quiet_until_wall is not None and not math.isfinite(self.quiet_until_wall):
            raise ValueError("quiet_until_wall must be finite")
        if any(not isinstance(k, str) or not isinstance(v, int) or isinstance(v, bool) or v < 0 for k, v in self.generation_by_interaction.items()):
            raise ValueError("generation_by_interaction must map string IDs to non-negative integers")
        if self.current_intent is not None:
            intent = self.current_intent
            if not intent.intent_id or not intent.intent_type or not intent.goal:
                raise ValueError("active intent requires non-empty identity and goal")
            if not isinstance(intent.status, IntentStatus):
                raise ValueError("active intent status must be an IntentStatus")
            if not math.isfinite(intent.score) or not 0.0 <= intent.score <= 1.0:
                raise ValueError("intent score must be finite and in [0,1]")
            for name, value in (("created_at_mono", intent.created_at_mono), ("started_at_mono", intent.started_at_mono)):
                if not math.isfinite(value):
                    raise ValueError(f"intent {name} must be finite")
            if intent.deadline_mono is not None and not math.isfinite(intent.deadline_mono):
                raise ValueError("intent deadline must be finite")
            if not 0 <= intent.clarification_rounds <= 2:
                raise ValueError("clarification_rounds must be in [0,2]")
            if len(intent.clarification_questions) > 2:
                raise ValueError("at most two clarification questions may be retained per intent")
            validate_json_compatible(intent.action_args, "intent.action_args")
