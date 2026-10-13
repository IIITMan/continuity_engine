"""Standalone provider latency probe: N sequential requests with thinking disabled.

This is a measurement tool, not an acceptance drill. It deliberately does **not** enable
the reliability layer, so a failure is reported instead of being retried away: the number
you see is the raw model + network latency for one request.

The request body is built through the engine's own state-view builder for the
`simple_response` purpose, so the prompt shape matches production dialogue rather than a
toy prompt.

    $env:CONTINUITY_LLM_ENDPOINT = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
    $env:CONTINUITY_LLM_MODEL    = "doubao-seed-2-1-pro-260915"
    $env:CONTINUITY_LLM_API_KEY  = "<your-key>"
    python examples/provider_latency_probe.py --repeats 10 --disable-thinking

No prompt or response text is printed or written; only timings and token counts.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from continuity_engine import (  # noqa: E402
    ConfigManager, ContinuityEngine, LLMRequest, OpenAICompatibleAdapter,
)

PROMPT = "用一句话说明你现在可以做什么。"


def _percentile(values: Sequence[float], p: float) -> float | None:
    data = sorted(v for v in values if isinstance(v, (int, float)) and math.isfinite(float(v)))
    if not data:
        return None
    return data[min(len(data) - 1, max(0, int(math.ceil(p * len(data))) - 1))]


async def _one_request(adapter: OpenAICompatibleAdapter, context_json: str, seq: int) -> dict:
    now = time.monotonic()
    request = LLMRequest(
        request_id=f"latency-{seq}", interaction_id="latency-probe", intent_id=None,
        generation=1, purpose="simple_response", created_at_mono=now,
        deadline_mono=now + 120.0, context_json=context_json, output_token_budget=256,
    )
    row: dict = {"seq": seq, "ok": False, "latency_s": None, "error": None,
                 "prompt_tokens": None, "completion_tokens": None, "reasoning_tokens": None,
                 "response_chars": 0, "output_tokens_per_s": None}
    # Clear the adapter's diagnostic slot so a failed request cannot be attributed the
    # previous request's token counts.
    adapter.last_usage = None
    started = time.monotonic()
    try:
        payload = await adapter.generate(request)
        row["latency_s"] = max(0.0, time.monotonic() - started)
        row["ok"] = isinstance(payload, dict) and isinstance(payload.get("response_text"), str)
        row["response_chars"] = len(str(payload.get("response_text") or ""))
    except Exception as exc:
        row["latency_s"] = max(0.0, time.monotonic() - started)
        row["error"] = type(exc).__name__
    usage = adapter.usage_snapshot().get("last")
    if isinstance(usage, dict) and row["ok"]:
        row["prompt_tokens"] = usage.get("prompt_tokens")
        row["completion_tokens"] = usage.get("completion_tokens")
        row["reasoning_tokens"] = usage.get("reasoning_tokens")
    if row["completion_tokens"] and row["latency_s"]:
        row["output_tokens_per_s"] = round(float(row["completion_tokens"]) / float(row["latency_s"]), 2)
    return row


async def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure single-request provider latency")
    parser.add_argument("--endpoint", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--api-key-env", default="CONTINUITY_LLM_API_KEY")
    parser.add_argument("--repeats", type=int, default=10, help="measured requests (default 10)")
    parser.add_argument("--warmup", type=int, default=1, help="uncounted warm-up requests")
    parser.add_argument("--timeout-s", type=float, default=120.0)
    parser.add_argument("--connect-timeout-s", type=float, default=10.0)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--disable-thinking", action="store_true")
    parser.add_argument("--out", default=None, help="optional JSON output path")
    args = parser.parse_args(argv)

    endpoint = args.endpoint or os.environ.get("CONTINUITY_LLM_ENDPOINT", "").strip()
    model = args.model or os.environ.get("CONTINUITY_LLM_MODEL", "").strip()
    api_key = args.api_key or os.environ.get(args.api_key_env)
    if not endpoint or not model:
        raise SystemExit("set --endpoint/--model or CONTINUITY_LLM_ENDPOINT/CONTINUITY_LLM_MODEL")
    if not api_key:
        raise SystemExit(f"no API key found (--api-key or {args.api_key_env})")
    if args.repeats < 1 or args.warmup < 0:
        raise SystemExit("--repeats must be >= 1 and --warmup >= 0")

    config = ConfigManager.from_yaml(ROOT / "config" / "default.yaml")
    engine = ContinuityEngine(config)
    adapter = OpenAICompatibleAdapter(
        endpoint_url=endpoint, model=model, api_key=api_key, timeout_s=args.timeout_s,
        connect_timeout_s=args.connect_timeout_s, temperature=args.temperature,
        stream=bool(args.stream), disable_thinking=bool(args.disable_thinking),
    )
    view = engine.state_view_builder.build("simple_response", engine.state, user_input=PROMPT)
    context_json = view.to_json()

    header = (f"endpoint={endpoint}\nmodel={model} repeats={args.repeats} warmup={args.warmup} "
              f"thinking_disabled={args.disable_thinking} stream={args.stream}")
    print(header)
    print("Latency is measured end-to-end around one request; no retry layer is enabled.")
    print()

    rows: list[dict] = []
    try:
        for index in range(args.warmup):
            warm = await _one_request(adapter, context_json, -(index + 1))
            print(f"warmup {index + 1}: ok={warm['ok']} latency={warm['latency_s']:.3f}s "
                  f"error={warm['error']}")
        print()
        print(f"{'#':>3} {'ok':>5} {'latency_s':>10} {'prompt':>7} {'out':>5} {'reason':>7} "
              f"{'tok/s':>7} {'chars':>6}  error")
        for seq in range(1, args.repeats + 1):
            row = await _one_request(adapter, context_json, seq)
            rows.append(row)
            print(f"{row['seq']:>3} {str(row['ok']):>5} {row['latency_s']:>10.3f} "
                  f"{str(row['prompt_tokens']):>7} {str(row['completion_tokens']):>5} "
                  f"{str(row['reasoning_tokens']):>7} {str(row['output_tokens_per_s']):>7} "
                  f"{row['response_chars']:>6}  {row['error'] or ''}")
        print()
        ok = [r for r in rows if r["ok"]]
        latencies = [float(r["latency_s"]) for r in ok]
        errors = [r for r in rows if not r["ok"]]
        summary: dict = {
            "model": model,
            "thinking_disabled": bool(args.disable_thinking),
            "stream": bool(args.stream),
            "requested": args.repeats, "succeeded": len(ok), "failed": len(errors),
            "error_rate": round(len(errors) / len(rows), 4) if rows else 0.0,
        }
        if latencies:
            summary["latency_s"] = {
                "min": round(min(latencies), 4),
                "p50": round(_percentile(latencies, 0.50) or 0.0, 4),
                "mean": round(statistics.fmean(latencies), 4),
                "p90": round(_percentile(latencies, 0.90) or 0.0, 4),
                "p95": round(_percentile(latencies, 0.95) or 0.0, 4),
                "max": round(max(latencies), 4),
                "stdev": round(statistics.stdev(latencies), 4) if len(latencies) > 1 else 0.0,
            }
            completion = [int(r["completion_tokens"]) for r in ok if r["completion_tokens"]]
            reasoning = [int(r["reasoning_tokens"]) for r in ok if r["reasoning_tokens"] is not None]
            if completion:
                summary["completion_tokens"] = {
                    "total": sum(completion), "min": min(completion),
                    "mean": round(statistics.fmean(completion), 2), "max": max(completion)}
                summary["output_tokens_per_s"] = round(sum(completion) / sum(latencies), 2)
            if reasoning:
                summary["reasoning_tokens"] = {"total": sum(reasoning), "max": max(reasoning)}
        print("summary:")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print()
        print("per-request:")
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        if args.out:
            path = Path(args.out)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"summary": summary, "requests": rows}, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            print(f"\nwritten: {path}")
    finally:
        await adapter.aclose()
        engine.close()
    return 0 if all(r["ok"] for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
