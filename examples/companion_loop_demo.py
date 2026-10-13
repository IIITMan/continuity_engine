"""Offline text companion demo. Uses local disclosed fallback when no LLM is attached."""
import asyncio
from pathlib import Path

from continuity_engine import CompanionService, ConfigManager, ContinuityEngine, ReplayClock, ReplayActionExecutor, SQLiteRepository

async def main() -> None:
    root = Path(__file__).resolve().parents[1]
    config = ConfigManager.from_yaml(root / "config" / "default.yaml")
    # This demo intentionally has no network provider. A production host should use a
    # configured LLMGateway and must not enable replay_mode with a real adapter.
    engine = ContinuityEngine(config, clock=ReplayClock(mono=0, wall=1_800_000_000),
                              action_executor=ReplayActionExecutor(), replay_mode=True)
    memories = SQLiteRepository(":memory:")
    engine.attach_memory_repository(memories)
    companion = CompanionService(engine)
    try:
        for text in ("你好", "我喜欢天文学", "记住我喜欢天文学"):
            result = await companion.chat(text, interaction_id="demo")
            print(f"[{result.status.value}] {result.response_text or result.error_code}")
        print("Approved/persistent memories:", len(memories.list_memories()))
        print("Volatile dialogue messages:", len(companion.history("demo")))
    finally:
        engine.close()
        memories.close()

if __name__ == "__main__":
    asyncio.run(main())
