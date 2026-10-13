"""Interactive text companion using an explicitly configured real LLM provider.

This example uses the simulated action executor: it does not control real actuators.
Set CONTINUITY_LLM_ENDPOINT and CONTINUITY_LLM_MODEL; keep the API key in your secret
store or environment. Press Ctrl+C to stop.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import time
from pathlib import Path

from continuity_engine import (
    CircuitBreaker, CompanionService, ConfigManager, ContinuityEngine, LLMRuntime,
    ReplayActionExecutor, RetryPolicy, SQLiteConversationStore, SQLiteRepository, FernetTranscriptCipher,
)

_HINTS = (
    "试试这些：",
    "  随便聊点什么，比如“我今天有点累”",
    "  记住一件事：“请记住：我的猫叫雪球”，稍后问“我的猫叫什么名字？”",
    "  打断它：对话过程中输入“停止”",
    "  结束：输入“退出”",
)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider-config", default=None, help="optional local YAML provider config")
    parser.add_argument("--persist-recent-dialogue", action="store_true",
                        help="explicitly persist recent chat context in local SQLite (plaintext unless encrypted mode is also enabled)")
    parser.add_argument("--encrypt-transcripts", action="store_true",
                        help="encrypt transcript contents at rest using CONTINUITY_TRANSCRIPT_FERNET_KEY")
    parser.add_argument("--no-reliability", dest="reliability", action="store_false", default=True,
                        help="disable the transport retry/circuit-breaker layer (on by default for a remote provider)")
    args = parser.parse_args()
    if args.encrypt_transcripts and not args.persist_recent_dialogue:
        parser.error("--encrypt-transcripts requires --persist-recent-dialogue")
    transcript_cipher = None
    if args.encrypt_transcripts:
        key = os.environ.get("CONTINUITY_TRANSCRIPT_FERNET_KEY")
        if not key:
            parser.error("set CONTINUITY_TRANSCRIPT_FERNET_KEY using a protected secret store")
        transcript_cipher = FernetTranscriptCipher(key)
    root = Path(__file__).resolve().parents[1]
    config = ConfigManager.from_yaml(root / "config" / "default.yaml")
    data_dir = root / "data"
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    # This demonstration keeps action execution simulated. A production host must
    # construct its reviewed executor and separate emergency-stop path explicitly.
    engine = ContinuityEngine(config, action_executor=ReplayActionExecutor(), replay_mode=False)
    memory_repo = SQLiteRepository(data_dir / "memories.sqlite3")
    engine.attach_memory_repository(memory_repo)
    session_store = SQLiteConversationStore(data_dir / "recent-dialogue.sqlite3", enabled=True,
                                            max_messages_per_session=64, retention_days=7,
                                            cipher=transcript_cipher) if args.persist_recent_dialogue else None
    runtime = None
    try:
        runtime = LLMRuntime.attach(
            engine, config_file=args.provider_config,
            env_prefix=None if args.provider_config else "CONTINUITY_LLM_",
            retry_policy=RetryPolicy(max_attempts=3, base_delay_s=0.4, max_delay_s=4.0) if args.reliability else None,
            circuit_breaker=CircuitBreaker(failure_threshold=5, recovery_timeout_s=30.0) if args.reliability else None,
        )
        companion = CompanionService(engine, session_store=session_store)
        adapter = runtime.delegate
        print("=" * 68)
        print("Continuity Engine 文本陪伴（真实供应商）")
        print(f"  端点      : {getattr(adapter, 'endpoint_url', 'unknown')}")
        print(f"  模型      : {getattr(adapter, 'model', 'unknown')}")
        print(f"  思考模式  : {'已关闭' if getattr(adapter, 'disable_thinking', False) else '默认（未显式关闭）'}")
        print(f"  可靠性层  : {'开（受控重试 + 熔断）' if args.reliability else '关'}")
        print(f"  执行器    : 模拟（不控制任何真实执行器）")
        if session_store is None:
            print("  近期对话  : 不持久化（默认）")
        elif transcript_cipher is not None:
            print("  近期对话  : 持久化，内容已加密落盘")
        else:
            print("  近期对话  : 持久化，明文（建议加 --encrypt-transcripts）")
        print("=" * 68)
        for line in _HINTS:
            print(line)
        print("输入“退出”结束。")
        print()
        while True:
            try:
                text = input("你> ").strip()
            except EOFError:
                break
            if text.casefold() in {"退出", "exit", "quit"}:
                break
            if not text:
                continue
            started = time.monotonic()
            result = await companion.chat(text, interaction_id="console-owner")
            elapsed = max(0.0, time.monotonic() - started)
            print(f"机器人 [{result.status.value}]> {result.response_text or result.error_code or ''}")
            print(f"  ({elapsed:.2f}s"
                  + (f"，记忆 {result.metadata['memory_count']} 条" if result.metadata.get("memory_count") else "")
                  + (f"，估算上下文 {result.metadata['variable_context_estimated_tokens']} token"
                     if result.metadata.get("variable_context_estimated_tokens") else "")
                  + ")\n")
        print("Metrics:", companion.metrics.snapshot())
        if runtime.reliability_snapshot() is not None:
            print("Reliability:", runtime.reliability_snapshot()["metrics"])
    finally:
        if runtime is not None:
            await runtime.aclose()
        engine.close()
        memory_repo.close()
        if session_store is not None:
            session_store.close()


if __name__ == "__main__":
    asyncio.run(main())
