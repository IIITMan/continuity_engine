"""End-to-end task planning with a deterministic replay LLM and simulated actions.

No cloud LLM, network service, audio device, or physical hardware is used.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from continuity_engine import (
    ConfigManager,
    ContinuityEngine,
    LLMGateway,
    LLMTaskPlanner,
    PlanningStatus,
    ReplayActionExecutor,
    ReplayClock,
    ReplayLLMAdapter,
)


async def main() -> None:
    root = Path(__file__).resolve().parents[1]
    config = ConfigManager.from_yaml(root / "config" / "default.yaml")
    clock = ReplayClock(mono=10.0, wall=1_800_000_000.0)
    executor = ReplayActionExecutor()
    adapter = ReplayLLMAdapter({
        "task_plan": {
            "steps": [
                {"description": "Wait briefly", "action_type": "WAIT", "args": {"duration_s": 1.0}},
                {"description": "Show a success indicator", "action_type": "LIGHT",
                 "args": {"pattern": "success", "duration_s": 2.0}},
            ]
        }
    })
    gateway = LLMGateway(
        adapter,
        timeout_s=float(config.get()["llm"]["default_timeout_s"]),
        max_concurrent=int(config.get()["llm"]["max_concurrent_requests"]),
        context_token_budgets=config.get()["llm"]["context_token_budgets"],
        replay_mode=True,
        action_executor=executor,
    )
    engine = ContinuityEngine(
        config, clock=clock, action_executor=executor, llm_gateway=gateway, replay_mode=True,
    )
    planner = LLMTaskPlanner()
    try:
        result = await planner.plan_goal(
            engine, "Wait briefly, then show a success indicator", request_id="demo-task-plan-001"
        )
        print("planner result:", result.status.value, result.error_code or "OK")
        if result.status != PlanningStatus.QUEUED:
            return

        # One core step accepts the plan and returns its first action proposal.
        step_result = engine.step(dt=0.0)
        print("core:", [item for item in step_result.diagnostics if item.startswith("task_plan:")])

        # Demonstrate the host application's control loop: dispatch proposals, tick the
        # core to consume terminal receipts, and continue until the sequential task ends.
        for _ in range(8):
            for proposal in step_result.proposals:
                decision, action_result = await engine.dispatch_action(proposal)
                print("action:", proposal.action_type, decision.outcome,
                      None if action_result is None else action_result.status.value)
            clock.advance(0.1)
            step_result = engine.step(dt=0.1)
            plan = engine.task_manager.active_plan
            if plan is None or plan.status.value in {"SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"}:
                break

        plan = engine.task_manager.active_plan
        print("final task state:", None if plan is None else plan.status.value)
        print("llm calls:", len(adapter.calls), "(fixture adapter only)")
    finally:
        engine.close()


if __name__ == "__main__":
    asyncio.run(main())
