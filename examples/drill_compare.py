"""Compare two provider-drill reports and print the metrics that matter.

    python examples/drill_compare.py BASE.json CANDIDATE.json

Both files must be produced by examples/real_provider_drill.py. Only statuses,
timings, counters and token counts are read; no prompt or response text is used.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping


def _pct_change(before: float | None, after: float | None) -> str:
    if before is None or after is None or before == 0:
        return "n/a"
    delta = (after - before) / before * 100.0
    return f"{delta:+.1f}%"


def _load(path: str) -> Mapping[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, Mapping):
        raise SystemExit(f"{path} is not a drill report")
    return data


def _row(label: str, before: Any, after: Any, unit: str = "", lower_is_better: bool = True) -> dict[str, Any]:
    b = float(before) if isinstance(before, (int, float)) else None
    a = float(after) if isinstance(after, (int, float)) else None
    change = _pct_change(b, a)
    if b is None or a is None or b == 0:
        direction = "n/a"
    else:
        better = (a < b) if lower_is_better else (a > b)
        direction = "better" if better else ("worse" if a != b else "same")
    return {"metric": label, "baseline": before, "candidate": after, "unit": unit,
            "change": change, "direction": direction}


def compare(baseline: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    b_conv, c_conv = baseline.get("conversation") or {}, candidate.get("conversation") or {}
    b_tok, c_tok = baseline.get("token_usage") or {}, candidate.get("token_usage") or {}
    b_resp = int(b_tok.get("responses") or 0)
    c_resp = int(c_tok.get("responses") or 0)
    b_fail = ((baseline.get("reliability") or {}).get("session_gateway") or {}).get("metrics", {})
    c_fail = ((candidate.get("reliability") or {}).get("session_gateway") or {}).get("metrics", {})
    b_lat, c_lat = b_conv.get("latency_stats") or {}, c_conv.get("latency_stats") or {}

    rows = [
        _row("input tokens (prompt_tokens, total)", b_tok.get("prompt_tokens"), c_tok.get("prompt_tokens"), "tok"),
        _row("input tokens per response", (b_tok.get("prompt_tokens") / b_resp) if b_resp else None,
             (c_tok.get("prompt_tokens") / c_resp) if c_resp else None, "tok"),
        _row("output tokens (completion_tokens, total)", b_tok.get("completion_tokens"), c_tok.get("completion_tokens"), "tok"),
        _row("output tokens per response", (b_tok.get("completion_tokens") / b_resp) if b_resp else None,
             (c_tok.get("completion_tokens") / c_resp) if c_resp else None, "tok"),
        _row("total tokens", b_tok.get("total_tokens"), c_tok.get("total_tokens"), "tok"),
        _row("reasoning tokens (thinking-off check)", b_tok.get("reasoning_tokens"), c_tok.get("reasoning_tokens"), "tok"),
        _row("turn latency P50", b_lat.get("p50_s"), c_lat.get("p50_s"), "s"),
        _row("turn latency P95", b_lat.get("p95_s"), c_lat.get("p95_s"), "s"),
        _row("turn latency mean", b_lat.get("mean_s"), c_lat.get("mean_s"), "s"),
        _row("turn latency max", b_lat.get("max_s"), c_lat.get("max_s"), "s"),
        _row("conversation hard failures", b_conv.get("failed"), c_conv.get("failed"), "turns"),
        _row("conversation disclosed degradations", b_conv.get("degraded"), c_conv.get("degraded"), "turns"),
        _row("conversation error rate", b_conv.get("error_rate"), c_conv.get("error_rate"), "ratio"),
        _row("provider transport failures (session)", b_fail.get("failures"), c_fail.get("failures"), "attempts"),
        _row("provider transport retries (session)", b_fail.get("retries"), c_fail.get("retries"), "attempts"),
        _row("circuit rejections (session)", b_fail.get("circuit_rejections"), c_fail.get("circuit_rejections"), "calls"),
        _row("wall duration", baseline.get("duration_s"), candidate.get("duration_s"), "s"),
    ]
    qualitative = [
        {"metric": "memory recall correct (turn 5 probe)", "baseline": b_conv.get("memory_recall_ok"),
         "candidate": c_conv.get("memory_recall_ok"), "unit": "bool", "change": "n/a", "direction": "n/a"},
        {"metric": "memory recall probe attempts", "baseline": next(
            (t.get("attempts") for t in b_conv.get("per_turn", []) if t.get("label") == "memory_recall"), None),
         "candidate": next((t.get("attempts") for t in c_conv.get("per_turn", []) if t.get("label") == "memory_recall"), None),
         "unit": "tries", "change": "n/a", "direction": "n/a"},
        {"metric": "context continuation correct (turn 13 probe)", "baseline": b_conv.get("context_continuation_ok"),
         "candidate": c_conv.get("context_continuation_ok"), "unit": "bool", "change": "n/a", "direction": "n/a"},
        {"metric": "context continuation probe attempts", "baseline": next(
            (t.get("attempts") for t in b_conv.get("per_turn", []) if t.get("label") == "context_continuation"), None),
         "candidate": next((t.get("attempts") for t in c_conv.get("per_turn", []) if t.get("label") == "context_continuation"), None),
         "unit": "tries", "change": "n/a", "direction": "n/a"},
        {"metric": "no fabrication without live data", "baseline": b_conv.get("no_fabrication_ok"),
         "candidate": c_conv.get("no_fabrication_ok"), "unit": "bool", "change": "n/a", "direction": "n/a"},
        {"metric": "recall after restart", "baseline": (baseline.get("restart") or {}).get("recall_after_restart_ok"),
         "candidate": (candidate.get("restart") or {}).get("recall_after_restart_ok"), "unit": "bool",
         "change": "n/a", "direction": "n/a"},
        {"metric": "all verdicts passed", "baseline": baseline.get("passed"), "candidate": candidate.get("passed"),
         "unit": "bool", "change": "n/a", "direction": "n/a"},
    ]
    return {"rows": rows, "qualitative": qualitative,
            "baseline": {"run_id": baseline.get("run_id"), "duration_s": baseline.get("duration_s"),
                         "turns": b_conv.get("turn_count"), "responses": b_resp},
            "candidate": {"run_id": candidate.get("run_id"), "duration_s": candidate.get("duration_s"),
                          "turns": c_conv.get("turn_count"), "responses": c_resp}}


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    result = compare(_load(argv[1]), _load(argv[2]))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
