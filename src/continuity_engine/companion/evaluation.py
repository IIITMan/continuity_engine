"""Scenario-based companion behavior evaluation with privacy-safe reports."""
from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
import uuid


@dataclass(frozen=True, slots=True)
class EvaluationTurn:
    user_input: str
    required_any: tuple[str, ...] = ()
    forbidden_any: tuple[str, ...] = ()
    allowed_statuses: tuple[str, ...] = ("COMPLETED", "DEGRADED")

    def __post_init__(self) -> None:
        if not isinstance(self.user_input, str) or not self.user_input.strip() or len(self.user_input) > 12000:
            raise ValueError("user_input must contain 1..12000 characters")
        if len(self.required_any) > 20 or len(self.forbidden_any) > 20:
            raise ValueError("each expectation list is limited to 20 entries")
        if any(not isinstance(v, str) or len(v) > 200 for v in (*self.required_any, *self.forbidden_any)):
            raise ValueError("expectations must be bounded strings")
        if not self.allowed_statuses or any(not isinstance(v, str) or len(v) > 80 for v in self.allowed_statuses):
            raise ValueError("allowed_statuses must be non-empty strings")


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    name: str
    turns: tuple[EvaluationTurn, ...]
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 100:
            raise ValueError("case name must contain 1..100 characters")
        if not self.turns or len(self.turns) > 100:
            raise ValueError("case must contain 1..100 turns")
        if len(self.description) > 500:
            raise ValueError("description must be <= 500 characters")


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    schema_version: str
    run_id: str
    started_at: float
    completed_at: float
    cases: tuple[Mapping[str, Any], ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def turn_count(self) -> int:
        return sum(int(case.get("turn_count", 0)) for case in self.cases)

    @property
    def pass_rate(self) -> float:
        turns = [turn for case in self.cases for turn in case.get("turns", [])]
        return sum(1 for turn in turns if turn.get("passed")) / len(turns) if turns else 0.0

    def to_dict(self) -> dict[str, Any]:
        turns = [turn for case in self.cases for turn in case.get("turns", [])]
        latencies = sorted(float(turn["latency_s"]) for turn in turns if isinstance(turn.get("latency_s"), (int, float)) and math.isfinite(float(turn["latency_s"])))
        def pct(p: float) -> float | None:
            if not latencies:
                return None
            return latencies[min(len(latencies)-1, max(0, int(math.ceil(p * len(latencies))) - 1))]
        return {"schema_version": self.schema_version, "run_id": self.run_id,
                "started_at": self.started_at, "completed_at": self.completed_at,
                "duration_s": max(0.0, self.completed_at-self.started_at),
                "case_count": len(self.cases), "turn_count": self.turn_count,
                "pass_rate": self.pass_rate, "latency_p50_s": pct(.50), "latency_p95_s": pct(.95),
                "cases": [dict(c) for c in self.cases], "metadata": dict(self.metadata),
                "privacy": "report contains status, timings, and boolean expectation results only; no raw prompts or responses"}


class CompanionEvaluationRunner:
    """Run an authored suite against a disposable or explicitly designated service.

    This is a smoke/behavior evaluation harness, not a claim of objective emotional
    quality. Tests should specify observable expected behavior instead of private states.
    """
    def __init__(self, companion: Any, *, clock=time.time, monotonic=time.monotonic):
        self.companion = companion
        self.clock = clock
        self.monotonic = monotonic

    async def run(self, cases: Sequence[EvaluationCase], *, run_id: str | None = None) -> EvaluationReport:
        if not cases or len(cases) > 100:
            raise ValueError("cases must contain 1..100 evaluation cases")
        names = [case.name for case in cases]
        if len(set(names)) != len(names):
            raise ValueError("case names must be unique")
        run_id = run_id or str(uuid.uuid4())
        started_wall = float(self.clock())
        started_mono = float(self.monotonic())
        results: list[dict[str, Any]] = []
        session_prefix = "evaluation:" + run_id[:120] + ":"
        for case in cases:
            session_id = session_prefix + case.name[:100]
            turn_results: list[dict[str, Any]] = []
            for index, turn in enumerate(case.turns):
                start = float(self.monotonic())
                try:
                    outcome = await self.companion.chat(turn.user_input, interaction_id=session_id)
                    elapsed = max(0.0, float(self.monotonic()) - start)
                    response = str(getattr(outcome, "response_text", ""))
                    status = str(getattr(getattr(outcome, "status", "FAILED"), "value", getattr(outcome, "status", "FAILED")))
                    error_code = getattr(outcome, "error_code", None)
                    requirements_ok = not turn.required_any or any(token in response for token in turn.required_any)
                    forbidden_ok = not any(token in response for token in turn.forbidden_any)
                    status_ok = status in turn.allowed_statuses
                    passed = bool(status_ok and requirements_ok and forbidden_ok)
                    turn_results.append({"index": index, "status": status, "passed": passed,
                                         "status_ok": status_ok, "requirements_ok": requirements_ok,
                                         "forbidden_ok": forbidden_ok, "response_chars": len(response),
                                         "latency_s": round(elapsed, 6),
                                         "error_code": str(error_code)[:100] if error_code else None})
                except Exception as exc:
                    elapsed = max(0.0, float(self.monotonic()) - start)
                    turn_results.append({"index": index, "status": "EXCEPTION", "passed": False,
                                         "status_ok": False, "requirements_ok": False, "forbidden_ok": True,
                                         "response_chars": 0, "latency_s": round(elapsed, 6),
                                         "error_code": type(exc).__name__})
            passed_turns = sum(1 for row in turn_results if row["passed"])
            results.append({"name": case.name,
                            "turn_count": len(turn_results), "passed_turns": passed_turns,
                            "pass_rate": passed_turns / len(turn_results), "passed": passed_turns == len(turn_results),
                            "turns": turn_results})
            # Clean only volatile memory. Any persistent session store's history is not
            # used by the next case because every case has a unique run/session ID.
            clear = getattr(self.companion, "clear_session", None)
            if callable(clear):
                clear(session_id)
        completed_wall = float(self.clock())
        _duration = max(0.0, float(self.monotonic()) - started_mono)
        return EvaluationReport("1.0", run_id, started_wall, completed_wall, tuple(results),
                                {"mode": "live_service", "cases_are_authored_expectations": True,
                                 "duration_monotonic_s": round(_duration, 6),
                                 "provider": "not inferred; inspect deployment configuration separately"})
