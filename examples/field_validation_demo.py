"""Non-destructive simulated preflight; it does not contact a provider or touch hardware."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from continuity_engine import (
    CompanionService, ConfigManager, ContinuityEngine, FieldValidationRunner,
    LLMGateway, ReplayActionExecutor, ReplayClock, ReplayLLMAdapter, SQLiteRepository,
)


async def main() -> None:
    root = Path(__file__).resolve().parents[1]
    config = ConfigManager.from_yaml(root / "config" / "default.yaml")
    adapter = ReplayLLMAdapter({"simple_response": {"response_text": "这是一个离线预检示例。"}})
    gateway = LLMGateway(adapter, context_token_budgets=config.get()["llm"]["context_token_budgets"])
    engine = ContinuityEngine(config, clock=ReplayClock(mono=1, wall=1_800_000_000),
                              action_executor=ReplayActionExecutor(), llm_gateway=gateway, replay_mode=True)
    repository = SQLiteRepository(":memory:")
    engine.attach_memory_repository(repository)
    companion = CompanionService(engine)
    try:
        report = await FieldValidationRunner(engine, companion=companion).run(field_mode=False)
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    finally:
        engine.close(); repository.close()


if __name__ == "__main__":
    asyncio.run(main())
