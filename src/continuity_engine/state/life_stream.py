"""Bounded structured daily life journal; stores no raw dialogue and uses no LLM.

Night consolidation is idempotent per day: retries/restarts must never turn one
night into multiple growth events. Only structured event categories are retained.
"""
from __future__ import annotations

from datetime import date
import hashlib
import random
from typing import Any, Mapping

from ..core.models import validate_json_compatible

_VARIANTS = {
    "reunion": [("今天又见到熟悉的人了。", 0.5), ("今天有一次熟悉的重聚。", 0.3), ("今天记录了一次重聚时刻。", 0.2)],
    "shared_activity": [("今天一起度过了一段时间。", 0.5), ("今天有一段共同活动的记录。", 0.3), ("今天发生了一件值得保留的共同经历。", 0.2)],
    "task_followup": [("今天继续关注一件之前提过的事情。", 0.5), ("今天记录了一项后续进展。", 0.3), ("今天有一项待跟进事情的更新。", 0.2)],
    "novelty": [("今天观察到一些新的环境信息。", 0.5), ("今天有新的环境观察记录。", 0.3), ("今天记录了一次新观察。", 0.2)],
}
_FORBIDDEN_KEYS = {"text", "raw_dialogue", "utterance", "conversation_text", "transcript", "raw_text", "user_message"}


def _reject_raw_dialogue(value: Any, path: str = "life_stream") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).casefold() in _FORBIDDEN_KEYS:
                raise ValueError(f"raw dialogue is prohibited in {path}.{key}")
            _reject_raw_dialogue(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_raw_dialogue(child, f"{path}[{index}]")


class LifeStream:
    def __init__(self, *, max_entries_per_day: int = 500, retention_days: int = 7,
                 enabled: bool = True, seed: str = "continuity"):
        if not 1 <= max_entries_per_day <= 10000 or not 1 <= retention_days <= 90:
            raise ValueError("invalid life stream limits")
        self.max_entries_per_day = max_entries_per_day
        self.retention_days = retention_days
        self.enabled = bool(enabled)
        self.seed = str(seed)
        self.daily: dict[str, list[dict[str, Any]]] = {}
        self.consolidated: dict[str, dict[str, Any]] = {}
        self.counted_days: set[str] = set()
        self.depth_days: set[str] = set()
        self.nights = 0
        self.self_depth = 0

    def record(self, *, day: str, kind: str, summary_key: str, source_id: str,
               timestamp_wall: float, metadata: Mapping[str, Any] | None = None) -> bool:
        if not self.enabled:
            return False
        if kind not in _VARIANTS:
            raise ValueError("unsupported structured life event kind")
        if not isinstance(day, str):
            raise TypeError("day must be ISO date string")
        date.fromisoformat(day)
        if not summary_key or len(summary_key) > 80 or not source_id or len(source_id) > 160:
            raise ValueError("summary_key/source_id invalid")
        safe_metadata = dict(metadata or {})
        _reject_raw_dialogue(safe_metadata, "life_stream.metadata")
        row = {"kind": kind, "summary_key": summary_key, "source_id": source_id,
               "timestamp_wall": float(timestamp_wall), "metadata": safe_metadata}
        validate_json_compatible(row, "life_stream.entry")
        bucket = self.daily.setdefault(day, [])
        if any(x["source_id"] == source_id and x["kind"] == kind for x in bucket):
            return False
        if len(bucket) >= self.max_entries_per_day:
            return False
        bucket.append(row)
        # A new event after a prior consolidation invalidates the summary for that day;
        # the next consolidation recomputes it, without double-counting the night.
        self.consolidated.pop(day, None)
        return True

    def consolidate_night(self, day: str) -> dict[str, Any]:
        date.fromisoformat(day)
        if day in self.consolidated:
            return dict(self.consolidated[day])
        rows = list(self.daily.get(day, []))
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["kind"]] = counts.get(row["kind"], 0) + 1
        parts: list[str] = []
        for kind in sorted(counts):
            seed_bytes = hashlib.sha256(f"{self.seed}:{day}:{kind}:{counts[kind]}".encode()).digest()
            rng = random.Random(int.from_bytes(seed_bytes[:8], "big"))
            variants = _VARIANTS[kind]
            parts.append(rng.choices([v for v, _ in variants], weights=[w for _, w in variants], k=1)[0])
        result = {"day": day, "entry_count": len(rows), "counts": counts,
                  "summary": " ".join(parts), "source_ids": sorted({r["source_id"] for r in rows}),
                  "consolidated": True}
        self.consolidated[day] = result
        if day not in self.counted_days:
            self.counted_days.add(day)
            self.nights += 1
        if rows and day not in self.depth_days:
            self.depth_days.add(day)
            self.self_depth = min(3650, self.self_depth + 1)
        return dict(result)

    def recent_context(self, *, before_day: str, limit: int = 3) -> list[dict[str, Any]]:
        date.fromisoformat(before_day)
        if limit < 0:
            raise ValueError("limit must be >= 0")
        keys = sorted((k for k in self.daily if k < before_day), reverse=True)[:self.retention_days]
        result = []
        for key in keys:
            rows = self.daily[key]
            if not rows:
                continue
            saved = self.consolidated.get(key)
            result.append({
                "day": key,
                "entry_count": len(rows),
                "kinds": sorted({r["kind"] for r in rows}),
                "summary": str(saved.get("summary", "")) if saved else self.consolidate_preview(key),
            })
        return result[:limit]

    def consolidate_preview(self, day: str) -> str:
        rows = self.daily.get(day, [])
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["kind"]] = counts.get(row["kind"], 0) + 1
        # Stable preview has no state mutation and no original conversation content.
        return " ".join(_VARIANTS[k][0][0] for k in sorted(counts))

    def prune(self, today: str) -> None:
        today_date = date.fromisoformat(today)
        keep_from = today_date.toordinal() - self.retention_days + 1
        self.daily = {d: rows for d, rows in self.daily.items() if date.fromisoformat(d).toordinal() >= keep_from}
        self.consolidated = {d: row for d, row in self.consolidated.items()
                             if date.fromisoformat(d).toordinal() >= keep_from}
        self.counted_days = {d for d in self.counted_days if date.fromisoformat(d).toordinal() >= keep_from}
        self.depth_days = {d for d in self.depth_days if date.fromisoformat(d).toordinal() >= keep_from}

    def snapshot(self) -> dict[str, Any]:
        result = {"daily": self.daily, "consolidated": self.consolidated,
                  "counted_days": sorted(self.counted_days), "depth_days": sorted(self.depth_days),
                  "nights": self.nights, "self_depth": self.self_depth}
        _reject_raw_dialogue(result)
        validate_json_compatible(result, "life_stream.snapshot")
        return result

    def restore(self, data: Mapping[str, Any]) -> None:
        if not isinstance(data, Mapping):
            raise ValueError("invalid life stream state")
        daily = data.get("daily", {})
        consolidated = data.get("consolidated", {})
        if not isinstance(daily, Mapping) or not isinstance(consolidated, Mapping):
            raise ValueError("invalid life stream state")
        checked: dict[str, list[dict[str, Any]]] = {}
        for day, entries in daily.items():
            date.fromisoformat(str(day))
            if not isinstance(entries, list) or len(entries) > self.max_entries_per_day:
                raise ValueError("invalid daily journal bucket")
            for entry in entries:
                if not isinstance(entry, Mapping) or "source_id" not in entry or "kind" not in entry:
                    raise ValueError("invalid life stream entry")
                _reject_raw_dialogue(entry)
                if entry.get("kind") not in _VARIANTS:
                    raise ValueError("invalid life stream event kind")
                validate_json_compatible(entry, "life_stream.entry")
            checked[str(day)] = [dict(x) for x in entries]
        saved: dict[str, dict[str, Any]] = {}
        for day, item in consolidated.items():
            date.fromisoformat(str(day))
            if not isinstance(item, Mapping) or item.get("day") != str(day) or not isinstance(item.get("summary"), str):
                raise ValueError("invalid consolidated day")
            _reject_raw_dialogue(item)
            validate_json_compatible(item, "life_stream.consolidated")
            saved[str(day)] = dict(item)
        counted = data.get("counted_days", list(saved))
        depth_days = data.get("depth_days", [d for d, rows in checked.items() if rows and d in saved])
        if not isinstance(counted, list) or not isinstance(depth_days, list):
            raise ValueError("invalid consolidated day counters")
        counted_checked = {str(d) for d in counted}
        depth_checked = {str(d) for d in depth_days}
        for key in counted_checked | depth_checked:
            date.fromisoformat(key)
        if not set(saved).issubset(counted_checked):
            raise ValueError("consolidated days missing idempotency markers")
        self.daily = checked
        self.consolidated = saved
        self.counted_days = counted_checked
        self.depth_days = depth_checked
        self.nights = max(0, int(data.get("nights", 0)))
        self.self_depth = min(3650, max(0, int(data.get("self_depth", 0))))
