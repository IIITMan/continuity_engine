"""Demonstrate bounded sequential tasks and a host-provided replanning response.

Uses only replay clock / replay action executor. No LLM, network, or hardware is called.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from continuity_engine import (
    ActionStatus, ConfigManager, ContinuityEngine, EventKind,
    ReplayActionExecutor, ReplayClock,
)


async def main() -> None:
    root = Path(__file__).resolve().parents[1]
    config = ConfigManager.from_yaml(root / "config" / "default.yaml")
    clock = ReplayClock(mono=0.0, wall=1_800_000_000.0)
    # Force the first speech action to fail so the engine can demonstrate its replan
    # request. A production host would send that minimized request to its LLM gateway.
    executor = ReplayActionExecutor(result_by_action_type={"SPEAK": ActionStatus.FAILED})
    engine = ContinuityEngine(config, clock=clock, action_executor=executor, replay_mode=True)
    try:
        initial_plan = {
            "task_id": "demo-task-1",
            "goal": "Greet then observe the room",
            "priority": 0.88,
            "steps": [
                {"step_id": "greet", "action_type": "SPEAK", "args": {"text": "你好。"}},
                {"step_id": "observe", "action_type": "OBSERVE", "args": {"key": "room_activity"}},
            ],
        }
        engine.inject(engine.new_event(EventKind.TASK_PLAN_SUBMITTED.value, initial_plan,
                                       source="demo_planner", priority=90))
        chosen = engine.step(dt=0.0)
        for proposal in chosen.proposals:
            print("dispatch initial step:", proposal.action_type, proposal.reason_code)
            await engine.dispatch_action(proposal)

        clock.advance(0.1)
        failed = engine.step(dt=0.1)
        request = failed.replan_request
        print("replan request:", request)
        if request is None:
            print("No replan required.")
            return

        # A real integration may call the LLM here. We use a deterministic fallback plan
        # to demonstrate request correlation, strict validation, and replacement semantics.
        replacement = {
            "task_id": "demo-task-2",
            "replaces_task_id": request["failed_task_id"],
            "goal": "Skip failed greeting and observe the room",
            "priority": 0.88,
            "steps": [
                {"step_id": "observe-only", "action_type": "OBSERVE", "args": {"key": "room_activity"}},
            ],
        }
        engine.inject(engine.new_event(EventKind.TASK_PLAN_SUBMITTED.value, replacement,
                                       source="demo_replanner", priority=90))
        replanned = engine.step(dt=0.0)
        print("replacement accepted:", engine.task_manager.active_plan.task_id)
        for proposal in replanned.proposals:
            print("dispatch replacement step:", proposal.action_type, proposal.reason_code)
            await engine.dispatch_action(proposal)
        clock.advance(0.1)
        completed = engine.step(dt=0.1)
        print("task state:", completed.state.task_state["active_plan"]["status"])
    finally:
        engine.close()


if __name__ == "__main__":
    asyncio.run(main())
