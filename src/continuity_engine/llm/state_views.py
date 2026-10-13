"""Purpose-specific immutable projections; raw EngineState is never serialized to LLM."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from types import MappingProxyType
from typing import Any, Mapping

from ..core.models import EngineState, WorldStatus, thaw, validate_json_compatible
from ..tasks.task_manager import llm_action_argument_schemas


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


class StateViewError(ValueError):
    pass


# Safe, intentionally small default allowlist. Applications may add stable, reviewed
# semantic fact keys at construction time; arbitrary sensor payloads never enter an LLM view.
DEFAULT_LLM_WORLD_KEYS = frozenset({
    "owner_present", "room_activity", "day_period", "weather_summary",
    "known_object_summary", "sound_event_summary",
})

_PURPOSE_FIELDS: dict[str, tuple[str, ...]] = {
    "demand_understanding": ("purpose", "user_input", "request_context", "personality_snapshot", "affect_guidance", "interaction_state_snapshot", "voice_signals"),
    "simple_response": ("purpose", "user_input", "interaction_rules", "current_task", "relevant_memories", "world_summary", "life_context"),
    "complex_dialogue": ("purpose", "user_input", "interaction_rules", "current_task", "relevant_memories", "recent_dialogue", "world_summary", "life_context"),
    "multi_step_plan": ("purpose", "goal", "confirmed_world_facts", "constraints", "allowed_tools", "task_progress", "relevant_memory_refs"),
    "task_plan": (
        "purpose", "goal", "confirmed_world_facts", "constraints", "allowed_action_types",
        "allowed_world_fact_keys", "action_argument_schemas", "validation_feedback",
        "task_progress", "failed_plan", "max_steps",
    ),
    "memory_consolidation": ("purpose", "time_range", "candidate_events"),
}


@dataclass(frozen=True, slots=True)
class StateView:
    purpose: str
    schema_version: str
    _fields: Mapping[str, Any]

    def __post_init__(self) -> None:
        fields = dict(self._fields)
        allowed = set(_PURPOSE_FIELDS.get(self.purpose, ()))
        if set(fields) != allowed:
            raise StateViewError(f"state view fields do not match {self.purpose} allowlist; expected {sorted(allowed)}, got {sorted(fields)}")
        validate_json_compatible(thaw(fields), "state_view")
        object.__setattr__(self, "_fields", _freeze(fields))

    def to_dict(self) -> dict[str, Any]:
        # The allowlist is checked again during serialization, defending against future changes.
        allowed = set(_PURPOSE_FIELDS[self.purpose])
        if set(self._fields) != allowed:
            raise StateViewError("unauthorized field present in state view")
        return {key: thaw(value) for key, value in self._fields.items()}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class StateViewBuilder:
    """Explicit per-purpose projection; never calls dataclasses.asdict on EngineState.

    Only explicitly reviewed semantic world-fact keys can be shared with an LLM.
    """

    def __init__(self, allowed_world_keys: set[str] | frozenset[str] | None = None):
        self.allowed_world_keys = frozenset(DEFAULT_LLM_WORLD_KEYS if allowed_world_keys is None else allowed_world_keys)

    def build(
        self,
        purpose: str,
        state: EngineState,
        *,
        now_mono: float | None = None,
        user_input: str | None = None,
        recent_dialogue: list[dict[str, str]] | None = None,
        relevant_memories: list[dict[str, Any]] | None = None,
        goal: str | None = None,
        allowed_tools: list[str] | None = None,
        allowed_action_types: list[str] | None = None,
        allowed_world_fact_keys: list[str] | None = None,
        validation_feedback: str = "",
        task_progress: dict[str, Any] | None = None,
        failed_plan: dict[str, Any] | None = None,
        max_steps: int | None = None,
        constraints: list[str] | None = None,
        time_range: dict[str, Any] | None = None,
        candidate_events: list[dict[str, Any]] | None = None,
        personality_snapshot: dict[str, Any] | None = None,
        affect_guidance: str | None = None,
        interaction_state_snapshot: dict[str, Any] | None = None,
    ) -> StateView:
        if purpose not in _PURPOSE_FIELDS:
            raise StateViewError(f"unsupported LLM purpose: {purpose}")
        current = state.current_intent
        task = None if current is None else {
            "intent_type": current.intent_type,
            "goal": current.goal,
            "status": current.status.value,
        }
        rules = {
            "quiet_mode": state.quiet_scope,
            "output_muted": bool(getattr(state, "output_muted", False)),
            "user_speaking": state.user_speaking,
            "ordinary_proactive_allowed": state.quiet_scope is None and not bool(getattr(state, "output_muted", False)),
        }
        # The model receives only a bounded, reviewed profile summary; raw conversation
        # and affect history are never injected into the state view.
        if personality_snapshot is not None:
            traits = personality_snapshot.get("traits", {})
            affect = personality_snapshot.get("affect", {})
            rules["personality"] = {
                "traits": {k: float(v) for k, v in traits.items() if k in {"warmth", "curiosity", "playfulness", "directness", "verbosity", "initiative"}},
                "affect": {k: float(v) for k, v in affect.items() if k in {"valence", "arousal", "social_energy"}},
            }
        if affect_guidance:
            rules["expression_guidance"] = affect_guidance[:1200]
        if interaction_state_snapshot is not None:
            # Explicitly project only the reviewed strategy/signal fields. Do not expose
            # internal timestamps, raw dialogue, or arbitrary host metadata.
            strategy = interaction_state_snapshot.get("current_strategy")
            if isinstance(strategy, Mapping):
                rules["current_interaction_strategy"] = {
                    "name": str(strategy.get("name", ""))[:80],
                    "instruction": str(strategy.get("instruction", ""))[:600],
                    "source": str(strategy.get("source", "host"))[:40],
                }
            else:
                rules["current_interaction_strategy"] = None
            signals = interaction_state_snapshot.get("signals", [])
            safe_signals = []
            if isinstance(signals, list):
                for signal in signals[:32]:
                    if not isinstance(signal, Mapping):
                        continue
                    name, value = signal.get("name"), signal.get("value")
                    if not isinstance(name, str) or len(name) > 64:
                        continue
                    if isinstance(value, str):
                        value = value[:100]
                    elif not isinstance(value, (bool, int, float)) or isinstance(value, float) and not math.isfinite(value):
                        continue
                    confidence = signal.get("confidence", 0.0)
                    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(float(confidence)) or not 0 <= float(confidence) <= 1:
                        continue
                    safe_signals.append({"name": name, "value": value, "confidence": float(confidence), "source": str(signal.get("source", "host"))[:40]})
            rules["interaction_state_signals"] = safe_signals
        memories = _sanitize_memories(relevant_memories if relevant_memories is not None else [])

        if purpose == "simple_response":
            fields = {
                "purpose": purpose,
                "user_input": _require_text(user_input, "user_input"),
                "interaction_rules": rules,
                "current_task": task,
                "relevant_memories": memories,
                # Simple replies can use vetted, fresh semantic context such as day
                # period/weather only when the host has actually populated world state.
                "world_summary": self._confirmed_world_summary(state, now_mono),
                "life_context": _sanitize_life_context(state.life_layer_state.get("life_context", [])),
            }
        elif purpose == "complex_dialogue":
            fields = {
                "purpose": purpose,
                "user_input": _require_text(user_input, "user_input"),
                "interaction_rules": rules,
                "current_task": task,
                "relevant_memories": memories,
                "recent_dialogue": _sanitize_dialogue(recent_dialogue if recent_dialogue is not None else []),
                "world_summary": self._confirmed_world_summary(state, now_mono),
                "life_context": _sanitize_life_context(state.life_layer_state.get("life_context", [])),
            }
        elif purpose == "multi_step_plan":
            if not isinstance(goal, str) or not goal.strip() or len(goal) > 1000:
                raise StateViewError("goal must be non-empty text up to 1000 characters")
            clean_constraints = constraints if constraints is not None else []
            clean_tools = allowed_tools if allowed_tools is not None else []
            if not isinstance(clean_constraints, list) or len(clean_constraints) > 50 or any(not isinstance(x, str) for x in clean_constraints):
                raise StateViewError("constraints must be a list of at most 50 strings")
            if not isinstance(clean_tools, list) or len(clean_tools) > 50 or any(not isinstance(x, str) or not x or len(x) > 100 for x in clean_tools):
                raise StateViewError("allowed_tools must be a list of at most 50 tool names")
            fields = {
                "purpose": purpose,
                "goal": goal.strip(),
                "confirmed_world_facts": self._confirmed_world_summary(state, now_mono),
                "constraints": [x[:500] for x in clean_constraints],
                "allowed_tools": sorted(set(clean_tools)),
                "task_progress": _sanitize_task_progress(task_progress if task_progress is not None else {}),
                "relevant_memory_refs": [{"memory_id": m["memory_id"], "source_refs": m["source_refs"]} for m in memories],
            }
        elif purpose == "task_plan":
            if not isinstance(goal, str) or not goal.strip() or len(goal) > 1000:
                raise StateViewError("goal must be non-empty text up to 1000 characters")
            clean_constraints = constraints if constraints is not None else []
            clean_actions = allowed_action_types if allowed_action_types is not None else []
            if not isinstance(clean_constraints, list) or len(clean_constraints) > 50 or any(not isinstance(x, str) for x in clean_constraints):
                raise StateViewError("constraints must be a list of at most 50 strings")
            if not isinstance(clean_actions, list) or len(clean_actions) > 20 or any(not isinstance(x, str) or not x or len(x) > 100 for x in clean_actions):
                raise StateViewError("allowed_action_types must be a list of at most 20 action names")
            requested_world_keys = allowed_world_fact_keys if allowed_world_fact_keys is not None else sorted(self.allowed_world_keys)
            if (not isinstance(requested_world_keys, list) or len(requested_world_keys) > 100
                    or any(not isinstance(x, str) or not x or len(x) > 128 for x in requested_world_keys)):
                raise StateViewError("allowed_world_fact_keys must be a list of at most 100 keys")
            # Callers cannot use the view builder to expand its reviewed world-key allowlist.
            clean_world_keys = sorted(set(requested_world_keys) & self.allowed_world_keys)
            clean_actions = sorted(set(clean_actions))
            if not clean_world_keys:
                clean_actions = [action for action in clean_actions if action != "OBSERVE"]
            if not isinstance(validation_feedback, str) or len(validation_feedback) > 1200:
                raise StateViewError("validation_feedback must be text up to 1200 characters")
            if not isinstance(max_steps, int) or isinstance(max_steps, bool) or not 1 <= max_steps <= 32:
                raise StateViewError("max_steps must be an integer in [1,32]")
            fields = {
                "purpose": purpose,
                "goal": goal.strip(),
                "confirmed_world_facts": self._confirmed_world_summary(state, now_mono),
                "constraints": [x[:500] for x in clean_constraints],
                "allowed_action_types": clean_actions,
                "allowed_world_fact_keys": clean_world_keys,
                "action_argument_schemas": llm_action_argument_schemas(clean_actions, clean_world_keys),
                "validation_feedback": validation_feedback.strip()[:1200],
                "task_progress": _sanitize_task_progress(task_progress if task_progress is not None else {}),
                "failed_plan": _sanitize_failed_plan(failed_plan if failed_plan is not None else {}),
                "max_steps": max_steps,
            }
        else:  # memory_consolidation
            if time_range is None:
                raise StateViewError("time_range is required for memory_consolidation")
            fields = {
                "purpose": purpose,
                "time_range": _sanitize_time_range(time_range),
                "candidate_events": _sanitize_candidate_events(candidate_events if candidate_events is not None else []),
            }
        return StateView(purpose, "1.0", fields)

    def _confirmed_world_summary(self, state: EngineState, now_mono: float | None = None) -> list[dict[str, Any]]:
        rows = []
        if now_mono is not None and (not math.isfinite(now_mono) or now_mono < 0):
            raise StateViewError("now_mono must be finite and >= 0")
        for key, fact in sorted(state.world.items()):
            if key not in self.allowed_world_keys or fact.status not in (WorldStatus.CONFIRMED, WorldStatus.PROBABLE):
                continue
            if now_mono is not None and fact.expires_at_mono <= now_mono:
                continue
            # World summaries expose only scalar, bounded semantic values. Raw nested
            # detector payloads, images, identifiers and arbitrary dictionaries are omitted.
            value = fact.value
            if not isinstance(value, (str, bool, int, float)) or isinstance(value, float) and not math.isfinite(value):
                continue
            if isinstance(value, str):
                value = value[:240]
            rows.append({"key": key, "value": value, "status": fact.status.value,
                         "confidence": fact.confidence, "updated_at_mono": fact.updated_at_mono})
            if len(rows) >= 50:
                break
        return rows


def _sanitize_task_progress(data: dict[str, Any]) -> dict[str, Any]:
    """Allow compact task progress only; do not accept arbitrary raw state blobs."""
    if not isinstance(data, dict):
        raise StateViewError("task_progress must be an object")
    allowed = {"current_step", "completed_steps", "status", "blocked_reason", "percent_complete"}
    extra = set(data) - allowed
    if extra:
        raise StateViewError(f"task_progress contains unapproved fields: {sorted(extra)}")
    clean: dict[str, Any] = {}
    for key in ("current_step", "blocked_reason"):
        if key in data:
            if not isinstance(data[key], str):
                raise StateViewError(f"task_progress.{key} must be text")
            clean[key] = data[key][:300]
    if "completed_steps" in data:
        steps = data["completed_steps"]
        if not isinstance(steps, list) or len(steps) > 30 or any(not isinstance(step, str) for step in steps):
            raise StateViewError("task_progress.completed_steps must be a list of at most 30 strings")
        clean["completed_steps"] = [step[:300] for step in steps]
    if "status" in data:
        if data["status"] not in {"pending", "running", "waiting", "blocked", "completed", "failed"}:
            raise StateViewError("task_progress.status is invalid")
        clean["status"] = data["status"]
    if "percent_complete" in data:
        value = data["percent_complete"]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)) or not 0 <= value <= 1:
            raise StateViewError("task_progress.percent_complete must be finite and in [0,1]")
        clean["percent_complete"] = float(value)
    return clean


def _sanitize_failed_plan(data: dict[str, Any]) -> dict[str, Any]:
    """Expose only minimized, bounded task-failure context to a plan model."""
    allowed = {"failed_step_id", "failed_step_description", "failed_action_type", "error_code", "completed_steps", "remaining_steps"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise StateViewError("failed_plan contains unapproved fields")
    result: dict[str, Any] = {}
    for key in ("failed_step_id", "failed_step_description", "failed_action_type", "error_code"):
        if key in data:
            value = data[key]
            if not isinstance(value, str):
                raise StateViewError(f"failed_plan.{key} must be text")
            result[key] = value[:300]
    for key in ("completed_steps", "remaining_steps"):
        if key not in data:
            continue
        rows = data[key]
        if not isinstance(rows, list) or len(rows) > 32:
            raise StateViewError(f"failed_plan.{key} must be a list of at most 32 items")
        clean_rows = []
        allowed_row_fields = {"step_id", "description", "action_type", "status"}
        for row in rows:
            if not isinstance(row, dict) or set(row) - allowed_row_fields:
                raise StateViewError(f"failed_plan.{key} contains invalid rows")
            clean_row: dict[str, str] = {}
            for field in allowed_row_fields:
                if field in row:
                    value = row[field]
                    if not isinstance(value, str):
                        raise StateViewError(f"failed_plan.{key}.{field} must be text")
                    clean_row[field] = value[:300]
            clean_rows.append(clean_row)
        result[key] = clean_rows
    return result


def _sanitize_time_range(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) - {"start_wall", "end_wall", "label"}:
        raise StateViewError("time_range allows only start_wall, end_wall and optional label")
    start, end = data.get("start_wall"), data.get("end_wall")
    for name, value in (("start_wall", start), ("end_wall", end)):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
            raise StateViewError(f"time_range.{name} must be finite numeric")
    if start > end:
        raise StateViewError("time_range.start_wall cannot be after end_wall")
    result: dict[str, Any] = {"start_wall": float(start), "end_wall": float(end)}
    if "label" in data:
        if not isinstance(data["label"], str):
            raise StateViewError("time_range.label must be text")
        result["label"] = data["label"][:120]
    return result


def _require_text(value: str | None, name: str) -> str:
    if value is None or not value.strip():
        raise StateViewError(f"{name} must be non-empty")
    return value


def _sanitize_life_context(rows: Any) -> list[dict[str, Any]]:
    """Project only bounded structured summaries; never raw events or dialogue."""
    if not isinstance(rows, list):
        return []
    clean = []
    for row in rows[:3]:
        if not isinstance(row, Mapping):
            continue
        day, count, summary = row.get("day"), row.get("entry_count"), row.get("summary")
        if not isinstance(day, str) or not isinstance(summary, str) or not isinstance(count, int) or isinstance(count, bool):
            continue
        clean.append({"day": day[:10], "entry_count": max(0, min(500, count)), "summary": summary[:600]})
    return clean


def _sanitize_memories(memories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(memories, list):
        raise StateViewError("relevant_memories must be a list")
    clean = []
    allowed = {"memory_id", "summary", "source_refs", "kind"}
    for row in memories[:100]:
        if not isinstance(row, dict):
            raise StateViewError("each relevant memory must be an object")
        if set(row) - allowed:
            raise StateViewError(f"memory contains unapproved fields: {sorted(set(row) - allowed)}")
        if not isinstance(row.get("memory_id"), str) or not row["memory_id"]:
            continue
        if not isinstance(row.get("summary"), str) or not row["summary"]:
            continue
        refs = row.get("source_refs", [])
        if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
            raise StateViewError("memory source_refs must be a list of strings")
        kind = row.get("kind", "unknown")
        if not isinstance(kind, str):
            raise StateViewError("memory kind must be text")
        clean.append({
            "memory_id": row["memory_id"][:200],
            "summary": row["summary"][:2000],
            "source_refs": refs[:20],
            "kind": kind[:100],
        })
    return clean[:20]


def _sanitize_dialogue(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    if not isinstance(rows, list):
        raise StateViewError("recent_dialogue must be a list")
    clean = []
    for row in rows[-12:]:
        if not isinstance(row, dict) or set(row) - {"role", "text"}:
            raise StateViewError("dialogue rows may contain only role and text")
        if row.get("role") not in {"user", "assistant"} or not isinstance(row.get("text"), str):
            continue
        clean.append({"role": row["role"], "text": row["text"][:2000]})
    return clean


def _sanitize_candidate_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise StateViewError("candidate_events must be a list")
    clean = []
    allowed = {"event_id", "kind", "timestamp", "summary", "source_refs", "fact_status"}
    for row in rows[:200]:
        if not isinstance(row, dict) or set(row) - allowed:
            raise StateViewError("candidate event contains unapproved fields or is not an object")
        if not isinstance(row.get("event_id"), str) or not row["event_id"]:
            continue
        timestamp = row.get("timestamp")
        if timestamp is not None and (not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool) or not math.isfinite(float(timestamp))):
            raise StateViewError("candidate event timestamp must be finite numeric")
        refs = row.get("source_refs", [])
        if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
            raise StateViewError("candidate event source_refs must be a list of strings")
        kind = row.get("kind", "unknown")
        summary = row.get("summary", "")
        status = row.get("fact_status", "unknown")
        if not all(isinstance(x, str) for x in (kind, summary, status)):
            raise StateViewError("candidate event text fields must be strings")
        clean.append({
            "event_id": row["event_id"][:200], "kind": kind[:100], "timestamp": timestamp,
            "summary": summary[:1000], "source_refs": refs[:20], "fact_status": status[:40],
        })
    return clean
