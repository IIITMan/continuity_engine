"""Interactive v0.9 reminder/weather/knowledge demo.

Reminders use a local persistent SQLite scheduler and native desktop notifications.
Weather and Wikipedia require explicit --enable-weather / --enable-knowledge flags,
which opt into external HTTP requests. Tests never use these live endpoints.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from continuity_engine import (
    ConfigManager,
    RequestContext,
    RequestDispatcher,
    RouteStatus,
    build_request_router_from_config,
    build_v09_provider_bundle,
)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/default.yaml")
    parser.add_argument("--reminder-db", default=None)
    parser.add_argument("--enable-weather", action="store_true", help="allow external Open-Meteo HTTP requests")
    parser.add_argument("--enable-knowledge", action="store_true", help="allow external Wikipedia HTTP requests")
    args = parser.parse_args()

    config = ConfigManager.from_yaml(args.config).get()
    if args.enable_weather:
        config.setdefault("capabilities", {})["weather_enabled"] = True
    if args.enable_knowledge:
        config.setdefault("capabilities", {})["knowledge_search_enabled"] = True

    bundle = build_v09_provider_bundle(config, reminder_database_path=args.reminder_db)
    await bundle.start()
    for capability_id, error in bundle.start_errors.items():
        print(f"[能力未启动] {capability_id}: {error}（不会假装执行提醒）")
    if args.enable_weather or args.enable_knowledge:
        print("[网络已显式启用] 天气/百科请求会向配置的公共 API 发送查询词和地点。")
    else:
        print("[网络关闭] 要启用天气或百科检索，请显式传入 --enable-weather / --enable-knowledge。")

    router = build_request_router_from_config(config, providers=bundle.providers, llm_available=False)
    dispatcher = RequestDispatcher(router.registry)
    context = RequestContext(
        timezone_name=str(config.get("request_routing", {}).get("default_timezone", "Asia/Tokyo")),
        default_location=config.get("request_routing", {}).get("default_location"),
        protected_locations=tuple(config.get("request_routing", {}).get("protected_locations", ("卧室", "浴室", "卫生间", "bathroom"))),
    )
    pending = None
    print("输入请求；输入 q 退出。提醒会由后台 scheduler 在到期时显示系统通知。")
    try:
        while True:
            text = await asyncio.to_thread(input, "你> ")
            if text.strip().lower() in {"q", "quit", "exit", "退出"}:
                break
            if pending is not None and pending.status == RouteStatus.NEEDS_CLARIFICATION:
                decision = router.answer_clarification(pending, text, context)
            else:
                decision = router.route(text, context)
            if decision.status == RouteStatus.NEEDS_CLARIFICATION:
                pending = decision
                print("机器人> " + (decision.question or decision.response_text or "请补充必要信息。"))
                continue
            pending = None
            result = await dispatcher.execute(decision, context)
            print("机器人> " + (result.message or f"{result.status.value}: {dict(result.data)}"))
            if result.status.value == "OK" and result.data:
                # For source-backed lookup/reminder creation, surface the small result metadata.
                if result.capability_id in {"weather.current", "weather.forecast", "knowledge.search", "reminder.create"}:
                    if result.capability_id == "knowledge.search":
                        for entry in result.data.get("results", [])[:3]:
                            if isinstance(entry, dict):
                                print(f"  来源：{entry.get('title')} — {entry.get('url')}")
                    elif result.capability_id == "reminder.create":
                        print(f"  reminder_id={result.data.get('reminder_id')} status={result.data.get('status')}")
    finally:
        await bundle.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
