"""Safe end-to-end demo: deterministic replay, no real LLM/network/hardware."""
from __future__ import annotations

import asyncio
from pathlib import Path

from continuity_engine import (
    ConfigManager,
    ContinuityEngine,
    EventKind,
    ReplayActionExecutor,
    ReplayClock,
)


async def main() -> None:
    root = Path(__file__).resolve().parents[1]
    config = ConfigManager.from_yaml(root / "config" / "default.yaml")
    clock = ReplayClock(mono=0.0, wall=1_800_000_000.0)
    executor = ReplayActionExecutor()
    engine = ContinuityEngine(
        config,
        clock=clock,
        action_executor=executor,
        replay_mode=True,
    )
    try:
        event = engine.new_event(
            EventKind.USER_MESSAGE.value,
            {"text": "你好，能听到我吗？"},
            source="demo_console",
        )
        engine.inject(event)
        first = engine.step(dt=0.0)
        print("step 1:", first.diagnostics)
        print("proposals:", [p.to_dict() for p in first.proposals])

        for proposal in first.proposals:
            decision, result = await engine.dispatch_action(proposal)
            print("safety:", decision.outcome, decision.code)
            print("action result:", result)

        clock.advance(0.1)
        second = engine.step(dt=0.1)
        print("step 2 diagnostics:", second.diagnostics)
        print("current intent:", second.state.current_intent)
        print("action starts:", len(executor.started))
    finally:
        engine.close()


if __name__ == "__main__":
    asyncio.run(main())
