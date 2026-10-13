"""Evidence-gated attachment signals; no inference from silence or stale presence."""
from __future__ import annotations
from dataclasses import dataclass, asdict
import math
from typing import Any, Mapping
from ..core.models import WorldFact, WorldStatus

@dataclass(frozen=True, slots=True)
class ReunionResponse:
    category: str
    mood_delta: float
    trust_gain: float
    attachment_impulse: float
    reason_code: str

class Attachment:
    def __init__(self, *, trust: float = 20.0, imprint_days: int = 14, separation_k: float = 0.0009,
                 separation_cap: float = 0.8, brief_s: float = 1800.0, long_away_s: float = 14400.0,
                 confidence_min: float = 0.85, enabled: bool = True):
        if not 0 <= trust <= 100: raise ValueError("trust must be in [0,100]")
        if imprint_days < 0 or separation_k < 0 or not math.isfinite(separation_k) or not 0 <= separation_cap <= 1: raise ValueError("invalid attachment parameters")
        if brief_s <= 0 or long_away_s <= brief_s or not 0 <= confidence_min <= 1: raise ValueError("invalid reunion thresholds")
        self.trust = float(trust); self.imprint_days = int(imprint_days); self.separation_k = float(separation_k)
        self.separation_cap = float(separation_cap); self.brief_s = float(brief_s); self.long_away_s = float(long_away_s)
        self.confidence_min = float(confidence_min); self.enabled = bool(enabled)
        self.separation_s = 0.0; self.confirmed_absent = False; self.last_presence_status = "UNKNOWN"
        self.last_reunion_wall: float | None = None; self.imprint_started_wall: float | None = None
        self.history: list[dict[str, Any]] = []

    @property
    def separation_level(self) -> float:
        if not self.confirmed_absent: return 0.0
        minutes = self.separation_s / 60.0
        return min(self.separation_k * minutes * minutes, self.separation_cap)

    def observe_presence(self, fact: WorldFact | None, *, dt: float, now_wall: float) -> ReunionResponse | None:
        if not self.enabled: return None
        if not math.isfinite(dt) or dt < 0 or not math.isfinite(now_wall): raise ValueError("invalid time")
        if fact is None or fact.status != WorldStatus.CONFIRMED or fact.confidence < self.confidence_min or not isinstance(fact.value, bool):
            self.last_presence_status = "UNKNOWN_OR_UNCONFIRMED"
            return None
        self.last_presence_status = "CONFIRMED_PRESENT" if fact.value else "CONFIRMED_ABSENT"
        if fact.value is False:
            # Only a confirmed absence advances separation. Silence/stale/unknown does not.
            self.confirmed_absent = True; self.separation_s += dt
            return None
        if not self.confirmed_absent:
            return None
        away = self.separation_s
        category = "brief" if away < self.brief_s else "away" if away < self.long_away_s else "long_away"
        factor = {"brief": .25, "away": .6, "long_away": 1.0}[category]
        age_days = 999.0 if self.imprint_started_wall is None else max(0.0, (now_wall - self.imprint_started_wall) / 86400.0)
        trust_gain = factor * (3.0 if age_days <= self.imprint_days else 1.0)
        response = ReunionResponse(category, mood_delta=.08 * factor, trust_gain=trust_gain,
                                  attachment_impulse=.15 * factor, reason_code=f"confirmed_reunion_{category}")
        self.trust = min(100.0, self.trust + trust_gain)
        self.history.append({**asdict(response), "separation_s": away, "timestamp_wall": now_wall})
        self.separation_s = 0.0; self.confirmed_absent = False; self.last_reunion_wall = now_wall
        if self.imprint_started_wall is None: self.imprint_started_wall = now_wall
        return response

    def record_shared_experience(self, kind: str, *, delta: float = 0.5, now_wall: float) -> None:
        if kind not in {"positive_shared_activity", "user_explicit_trust_feedback", "successful_followup"}:
            raise ValueError("unsupported attachment evidence kind")
        if not math.isfinite(delta) or not 0 <= delta <= 2 or not math.isfinite(now_wall): raise ValueError("invalid trust delta/time")
        age_days = 999.0 if self.imprint_started_wall is None else max(0.0, (now_wall - self.imprint_started_wall) / 86400.0)
        gain = delta * (3.0 if age_days <= self.imprint_days else 1.0)
        self.trust = min(100.0, self.trust + gain)
        self.history.append({"kind": kind, "trust_gain": gain, "timestamp_wall": now_wall})
        if self.imprint_started_wall is None: self.imprint_started_wall = now_wall

    def snapshot(self) -> dict[str, Any]:
        return {"trust": self.trust, "imprint_days": self.imprint_days, "separation_k": self.separation_k,
                "separation_cap": self.separation_cap, "separation_s": self.separation_s,
                "confirmed_absent": self.confirmed_absent, "last_presence_status": self.last_presence_status,
                "last_reunion_wall": self.last_reunion_wall, "imprint_started_wall": self.imprint_started_wall,
                "history": self.history[-100:]}

    def restore(self, data: Mapping[str, Any]) -> None:
        trust = float(data.get("trust", self.trust)); sep = float(data.get("separation_s", 0.0))
        if not math.isfinite(trust) or not 0 <= trust <= 100 or not math.isfinite(sep) or sep < 0: raise ValueError("invalid persisted attachment state")
        self.trust, self.separation_s = trust, sep
        self.confirmed_absent = bool(data.get("confirmed_absent", False))
        self.last_presence_status = str(data.get("last_presence_status", "UNKNOWN"))
        self.last_reunion_wall = data.get("last_reunion_wall"); self.imprint_started_wall = data.get("imprint_started_wall")
        self.history = list(data.get("history", []))[-100:]
