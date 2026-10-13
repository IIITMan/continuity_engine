"""Run the real-provider acceptance drill against a live OpenAI-compatible endpoint.

This example is an explicit, opt-in live test. It makes real network calls and can
consume provider quota. It does not control actuators (the action executor stays
simulated) and never writes raw prompts or responses into its report.

Volcano Ark (火山方舟 / Doubao) example:

    $env:CONTINUITY_LLM_ENDPOINT = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
    $env:CONTINUITY_LLM_MODEL = "doubao-seed-2-1-pro-260915"
    $env:CONTINUITY_LLM_API_KEY = "<your-key>"          # keep this out of source control
    python examples/real_provider_drill.py --turns 60 --disable-thinking --enable-reliability \
        --out data/drill_reports/ark-drill

Exit code is 0 only when every recorded verdict passed.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from continuity_engine import (  # noqa: E402
    CompanionService, ConfigManager, ContinuityEngine, LLMGateway, LLMRuntime,
    OpenAICompatibleAdapter, SQLiteConversationStore, SQLiteRepository,
)
from continuity_engine.llm.reliability import CircuitBreaker, ReliabilityMetrics, ReliableLLMAdapter, RetryPolicy  # noqa: E402
from continuity_engine.validation.provider_drill import (  # noqa: E402
    DrillConfig, DrillSession, FaultInjectingAdapter, FaultSpec, ProviderDrillRunner,
)


class ArkDrillEnvironment:
    """Concrete DrillEnvironment for a live OpenAI-compatible endpoint."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.data_dir = Path(args.data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.endpoint = args.endpoint or os.environ.get("CONTINUITY_LLM_ENDPOINT", "").strip()
        self.model = args.model or os.environ.get("CONTINUITY_LLM_MODEL", "").strip()
        key_env = args.api_key_env
        self.api_key = args.api_key or (os.environ.get(key_env) if key_env else None)
        if not self.endpoint or not self.model:
            raise SystemExit("set --endpoint/--model or CONTINUITY_LLM_ENDPOINT/CONTINUITY_LLM_MODEL")
        if not self.api_key:
            raise SystemExit(f"no API key found; set --api-key or the {key_env} environment variable")
        self.reliability_enabled = bool(args.enable_reliability)
        self.retry_policy = RetryPolicy(
            max_attempts=args.max_attempts,
            base_delay_s=args.base_delay_s,
            max_delay_s=args.max_delay_s,
            max_total_retry_wait_s=args.max_total_retry_wait_s,
            jitter_ratio=args.jitter_ratio,
        )
        self.circuit = CircuitBreaker(failure_threshold=args.circuit_failure_threshold,
                                      recovery_timeout_s=args.circuit_recovery_timeout_s)
        self.gateway_metrics = ReliabilityMetrics()

    # -- introspection ---------------------------------------------------------
    def describe(self) -> Mapping[str, Any]:
        host = self.endpoint.split("/")[2] if "//" in self.endpoint else self.endpoint
        return {"vendor": "volcano-ark" if "volces.com" in host else "openai-compatible",
                "endpoint_host": host, "endpoint_path": "/" + "/".join(self.endpoint.split("/")[3:]),
                "model": self.model, "api_key_configured": bool(self.api_key),
                "streaming": bool(self.args.stream), "thinking_disabled": bool(self.args.disable_thinking),
                "reliability_layer": self.reliability_enabled,
                "retry_policy": self.retry_policy.to_dict() if self.reliability_enabled else None,
                "gateway_timeout_s": self.args.timeout_s, "max_concurrent": self.args.concurrency,
                "action_executor": "simulated", "transcript_persistence": "enabled (local plaintext SQLite)"}

    # -- adapters --------------------------------------------------------------
    def _build_provider(self) -> OpenAICompatibleAdapter:
        return OpenAICompatibleAdapter(
            endpoint_url=self.endpoint, model=self.model, api_key=self.api_key,
            timeout_s=self.args.timeout_s, temperature=self.args.temperature,
            stream=bool(self.args.stream), disable_thinking=bool(self.args.disable_thinking),
            connect_timeout_s=self.args.connect_timeout_s,
            max_connections=self.args.max_connections,
            max_keepalive_connections=self.args.max_keepalive_connections,
        )

    async def create_fault_adapter(self, *, label: str, faults: Sequence[FaultSpec] = (),
                                   outage: bool = False) -> FaultInjectingAdapter:
        return FaultInjectingAdapter(self._build_provider(), faults=faults, outage=outage)

    async def close_fault_adapter(self, adapter: FaultInjectingAdapter) -> None:
        await adapter.aclose()

    # -- sessions --------------------------------------------------------------
    async def create_session(self, *, label: str) -> DrillSession:
        config = ConfigManager.from_yaml(ROOT / "config" / "default.yaml")
        engine = ContinuityEngine(config)
        memory_repo = SQLiteRepository(self.data_dir / "memories.sqlite3")
        engine.attach_memory_repository(memory_repo)
        store = SQLiteConversationStore(self.data_dir / "recent-dialogue.sqlite3", enabled=True,
                                        max_messages_per_session=64, retention_days=7)
        fault_adapter = FaultInjectingAdapter(self._build_provider())
        adapter: Any = fault_adapter
        reliability = None
        if self.reliability_enabled:
            reliability = ReliableLLMAdapter(fault_adapter, policy=self.retry_policy,
                                             breaker=self.circuit, metrics=self.gateway_metrics)
            adapter = reliability
        gateway = LLMGateway(adapter, timeout_s=self.args.timeout_s, max_concurrent=self.args.concurrency)
        engine.llm_gateway = gateway
        runtime = LLMRuntime(engine, adapter, gateway, reliability)
        companion = CompanionService(engine, session_store=store)
        return DrillSession(label=label, engine=engine, companion=companion, runtime=runtime,
                            fault_adapter=fault_adapter, memory_repository=memory_repo,
                            session_store=store)

    async def close_session(self, session: DrillSession) -> None:
        runtime = session.runtime
        if runtime is not None:
            await runtime.aclose()
        session.close()


def _build_config(args: argparse.Namespace) -> DrillConfig:
    kwargs: dict[str, Any] = {"turns": args.turns, "max_report_turns": args.max_report_turns}
    if args.retry_policy_overrides_drill:
        kwargs["retry_policy"] = RetryPolicy(max_attempts=args.max_attempts, base_delay_s=args.base_delay_s,
                                             max_delay_s=args.max_delay_s,
                                             max_total_retry_wait_s=args.max_total_retry_wait_s,
                                             jitter_ratio=args.jitter_ratio)
    kwargs["circuit_failure_threshold"] = args.circuit_failure_threshold
    kwargs["circuit_recovery_timeout_s"] = args.circuit_recovery_timeout_s
    kwargs["deadline_timeout_s"] = args.deadline_timeout_s
    kwargs["probe_timeout_s"] = args.probe_timeout_s
    kwargs["compat_timeout_s"] = args.probe_timeout_s
    kwargs["compat_attempts"] = args.compat_attempts
    kwargs["expect_thinking_disabled"] = bool(args.disable_thinking)
    kwargs["measure_python_memory"] = not args.no_python_memory
    for name, value in (("run_compatibility", args.compatibility), ("run_conversation", args.conversation),
                        ("run_transport_drills", args.transport_drills),
                        ("run_cancellation", args.cancellation),
                        ("run_outage_recovery", args.outage_recovery), ("run_restart", args.restart)):
        kwargs[name] = value
    return DrillConfig(**kwargs)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live real-provider acceptance drill")
    parser.add_argument("--endpoint", default=None, help="full chat-completions URL")
    parser.add_argument("--model", default=None, help="provider model or endpoint id")
    parser.add_argument("--api-key", default=None, help="API key (prefer --api-key-env)")
    parser.add_argument("--api-key-env", default="CONTINUITY_LLM_API_KEY")
    parser.add_argument("--turns", type=int, default=60, help="conversation turns (50-100 recommended)")
    parser.add_argument("--timeout-s", type=float, default=45.0, help="per-request gateway timeout")
    parser.add_argument("--connect-timeout-s", type=float, default=10.0)
    parser.add_argument("--probe-timeout-s", type=float, default=60.0)
    parser.add_argument("--deadline-timeout-s", type=float, default=1.0)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--max-connections", type=int, default=20)
    parser.add_argument("--max-keepalive-connections", type=int, default=10)
    parser.add_argument("--stream", action="store_true", help="use SSE streaming and aggregate deltas")
    parser.add_argument("--disable-thinking", action="store_true",
                        help="send thinking={'type':'disabled'} (Doubao/Ark)")
    parser.add_argument("--enable-reliability", action="store_true",
                        help="wrap the provider with controlled retry + circuit breaking")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--base-delay-s", type=float, default=0.4)
    parser.add_argument("--max-delay-s", type=float, default=4.0)
    parser.add_argument("--max-total-retry-wait-s", type=float, default=10.0)
    parser.add_argument("--jitter-ratio", type=float, default=0.25)
    parser.add_argument("--retry-policy-overrides-drill", action="store_true",
                        help="use the transport retry settings for the drill too")
    parser.add_argument("--circuit-failure-threshold", type=int, default=3)
    parser.add_argument("--circuit-recovery-timeout-s", type=float, default=2.0)
    parser.add_argument("--max-report-turns", type=int, default=200)
    parser.add_argument("--compat-attempts", type=int, default=3,
                        help="independent samples per output-contract probe")
    parser.add_argument("--no-python-memory", dest="no_python_memory", action="store_true",
                        help="do not enable tracemalloc-based Python memory sampling")
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--out", default=str(ROOT / "data" / "drill_reports" / "provider-drill"))
    parser.add_argument("--no-compatibility", dest="compatibility", action="store_false", default=True)
    parser.add_argument("--no-conversation", dest="conversation", action="store_false", default=True)
    parser.add_argument("--no-transport-drills", dest="transport_drills", action="store_false", default=True)
    parser.add_argument("--no-cancellation", dest="cancellation", action="store_false", default=True)
    parser.add_argument("--no-outage-recovery", dest="outage_recovery", action="store_false", default=True)
    parser.add_argument("--no-restart", dest="restart", action="store_false", default=True)
    return parser.parse_args(argv)


async def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    environment = ArkDrillEnvironment(args)
    config = _build_config(args)
    runner = ProviderDrillRunner(environment, config=config)
    print(f"Drill target: {environment.endpoint} model={environment.model}")
    print(f"turns={args.turns} stream={args.stream} thinking_disabled={args.disable_thinking} "
          f"reliability={args.enable_reliability}")
    print("This run makes real provider calls and may consume quota. No actuators are controlled.")
    report = await runner.run()
    paths = report.write(Path(args.out))
    print()
    print("== Verdicts ==")
    for name, ok in report.verdicts.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print()
    conv = report.conversation or {}
    if conv:
        print(f"conversation: turns={conv.get('turn_count')} completed={conv.get('completed')} "
              f"degraded={conv.get('degraded')} failed={conv.get('failed')} "
              f"error_rate={conv.get('error_rate')}")
        print(f"latency: p50={conv.get('latency_p50_s')}s p95={conv.get('latency_p95_s')}s "
              f"max={conv.get('latency_max_s')}s")
    print(f"report: {paths['markdown']}")
    print(f"report: {paths['json']}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
