"""Dry-run examples for broad user-request routing (no network or physical providers)."""
from __future__ import annotations

import asyncio
from pathlib import Path

from continuity_engine import (
    ConfigManager,
    ContinuityEngine,
    RequestDispatcher,
    RouteStatus,
    build_request_router_from_config,
    make_engine_control_event_sink,
)


async def main() -> None:
    root = Path(__file__).resolve().parents[1]
    config = ConfigManager.from_yaml(root / "config" / "default.yaml")
    engine = ContinuityEngine(config)  # simulated executor; no physical side effects
    router = build_request_router_from_config(config, llm_available=False, task_planner_available=False)
    dispatcher = RequestDispatcher(
        router.registry,
        control_event_sink=make_engine_control_event_sink(engine),
    )
    context = router.default_context
    examples = (
        "现在几点？",
        "你能看见我吗？",
        "摄像头开着吗？",
        "看看周围",
        "你去卧室看看有没有人",
        "来我这里",
        "你不要打扰我好不好",
        "陪我聊聊天",
        "给我放首歌",
        "帮我计时十分钟",
        "什么是光合作用？",
        "你有什么功能？",
    )

    try:
        for index, text in enumerate(examples, 1):
            decision = router.route(text, context, request_id=f"router-demo-{index}")
            print(f"\n用户: {text}")
            print(f"意图: {decision.intent.name.value} | 路由: {decision.status.value} | 原因: {decision.reason_code}")
            if decision.status in {RouteStatus.READY, RouteStatus.CONTROL_ACTION, RouteStatus.CAPABILITY_QUERY}:
                result = await dispatcher.execute(decision, context)
                print(f"结果: {result.status.value} | {result.message or dict(result.data)}")
                if decision.status == RouteStatus.CONTROL_ACTION:
                    # The request router only enqueues the control event; the host owns stepping.
                    engine.step(dt=0.0)
            else:
                print(f"答复/追问: {decision.question or decision.response_text or '需要宿主处理'}")
                if decision.missing_capabilities:
                    print(f"缺少能力: {', '.join(decision.missing_capabilities)}")
    finally:
        engine.close()


if __name__ == "__main__":
    asyncio.run(main())
