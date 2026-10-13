from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from continuity_engine import (
    CapabilityCall,
    CapabilityResultStatus,
    ClassifiedIntent,
    ConfigManager,
    IntentName,
    RequestContext,
    RequestDispatcher,
    RequestDomain,
    RequestRouter,
    RouteStatus,
    ReminderScheduler,
    OpenMeteoWeatherProvider,
    WikipediaKnowledgeProvider,
    build_default_registry,
    build_request_router_from_config,
    build_v09_provider_bundle,
    parse_reminder_due_at,
)


class FakeDelivery:
    def __init__(self):
        self.delivered = []
        self.closed = False
        self.delivered_event = None

    async def deliver(self, reminder):
        self.delivered.append(reminder.to_dict())
        if self.delivered_event is not None:
            self.delivered_event.set()
        return {"ok": True, "backend": "test"}

    async def health_check(self):
        return True

    async def aclose(self):
        self.closed = True


def _intent(name: IntentName, raw: str, slots=None):
    return ClassifiedIntent(name, RequestDomain.INFORMATION, raw, raw, slots or {})


def test_parse_relative_reminder_time_and_horizon():
    now = datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc).timestamp()
    due = parse_reminder_due_at("一分钟后", timezone_name="Asia/Tokyo", now_wall_s=now)
    assert due == now + 60
    assert parse_reminder_due_at("半小时后", timezone_name="Asia/Tokyo", now_wall_s=now) == now + 1800
    with pytest.raises(ValueError, match="30 天以内"):
        parse_reminder_due_at("31天后", timezone_name="Asia/Tokyo", now_wall_s=now)


def test_parse_absolute_chinese_reminder_time():
    now = datetime(2026, 10, 11, 0, 0, tzinfo=timezone.utc).timestamp()  # Tokyo 09:00
    due = parse_reminder_due_at("明天下午三点", timezone_name="Asia/Tokyo", now_wall_s=now)
    expected = datetime(2026, 10, 12, 15, 0, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Tokyo")).timestamp()
    assert due == expected


def test_router_extracts_relative_reminder_task_and_time():
    router = RequestRouter(build_default_registry(providers={"reminder.create": _no_op_provider}))
    decision = router.route("一分钟后提醒我吃饭")
    assert decision.intent.name == IntentName.REMINDER_CREATE
    assert decision.status == RouteStatus.READY
    assert decision.calls[0].arguments == {"text": "吃饭", "time": "一分钟后"}


async def _no_op_provider(call, intent, context):
    from continuity_engine import CapabilityResult
    return CapabilityResult(CapabilityResultStatus.OK, call.capability_id)


@pytest.mark.asyncio
async def test_reminder_service_persists_and_delivers_due_reminder(tmp_path):
    now = [datetime(2026, 10, 11, 0, 0, tzinfo=timezone.utc).timestamp()]
    delivery = FakeDelivery()
    service = ReminderScheduler(
        tmp_path / "reminders.sqlite3", delivery=delivery,
        clock=lambda: now[0], poll_interval_s=0.05,
    )
    await service.start()
    try:
        registry = build_default_registry(providers={"reminder.create": service})
        router = RequestRouter(registry)
        context = RequestContext(timezone_name="Asia/Tokyo", now_wall_s=now[0])
        decision = router.route("一分钟后提醒我吃饭", context)
        assert decision.status == RouteStatus.READY
        result = await RequestDispatcher(router.registry).execute(decision, context)
        assert result.status == CapabilityResultStatus.OK
        reminder_id = result.data["reminder_id"]
        assert result.data["text"] == "吃饭"
        assert result.data["status"] == "PENDING"
        assert (await service.get(reminder_id))["status"] == "PENDING"

        now[0] += 61
        assert await service.run_due_once() == 1
        record = await service.get(reminder_id)
        assert record["status"] == "FIRED"
        assert len(delivery.delivered) == 1
        assert delivery.delivered[0]["text"] == "吃饭"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_reminder_restart_marks_inflight_unknown_not_duplicate(tmp_path):
    delivery = FakeDelivery()
    db = tmp_path / "reminders.sqlite3"
    service = ReminderScheduler(db, delivery=delivery)
    await service.start()
    try:
        assert service._db is not None
        service._db.execute(
            "INSERT INTO reminders(reminder_id,text,due_at_wall_s,timezone_name,status,created_at_wall_s) VALUES(?,?,?,?,?,?)",
            ("crash-row", "检查结果", time.time() - 1, "Asia/Tokyo", "FIRING", time.time() - 2),
        )
    finally:
        await service.close()

    recovered = ReminderScheduler(db, delivery=FakeDelivery())
    await recovered.start()
    try:
        row = await recovered.get("crash-row")
        assert row["status"] == "UNKNOWN"
        assert row["error_code"] == "PROCESS_RESTART_DURING_DELIVERY"
        assert await recovered.run_due_once() == 0
    finally:
        await recovered.close()


def _weather_transport(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/v1/search"):
        return httpx.Response(200, json={"results": [{"name": "Tokyo", "admin1": "Tokyo", "country": "Japan", "latitude": 35.68, "longitude": 139.69}]})
    if request.url.path.endswith("/v1/forecast"):
        forecast = {
            "timezone": "Asia/Tokyo",
            "daily": {"time": ["2026-10-11", "2026-10-12", "2026-10-13"],
                      "weather_code": [1, 61, 3], "temperature_2m_max": [23, 20, 18],
                      "temperature_2m_min": [16, 15, 14], "precipitation_probability_max": [10, 70, 20],
                      "sunrise": ["05:40", "05:41", "05:42"], "sunset": ["17:10", "17:09", "17:08"]},
            "hourly": {"time": ["2026-10-11T15:00", "2026-10-11T20:00"],
                       "temperature_2m": [22, 18], "precipitation_probability": [10, 20], "weather_code": [1, 2]},
        }
        if request.url.params.get("current") and not request.url.params.get("daily"):
            return httpx.Response(200, json={
                "timezone": "Asia/Tokyo",
                "current": {"time": "2026-10-11T18:00", "temperature_2m": 22.1, "relative_humidity_2m": 55,
                            "apparent_temperature": 21.2, "is_day": 0, "precipitation": 0.0,
                            "weather_code": 2, "wind_speed_10m": 9.3},
            })
        forecast["current"] = {"time": "2026-10-11T18:00", "temperature_2m": 22.1, "relative_humidity_2m": 55,
                               "apparent_temperature": 21.2, "is_day": 0, "precipitation": 0.0,
                               "weather_code": 2, "wind_speed_10m": 9.3}
        return httpx.Response(200, json=forecast)
    return httpx.Response(404, json={"error": "unexpected route"})


@pytest.mark.asyncio
async def test_open_meteo_current_weather_uses_real_provider_contract():
    client = httpx.AsyncClient(transport=httpx.MockTransport(_weather_transport))
    provider = OpenMeteoWeatherProvider(client=client)
    try:
        call = CapabilityCall("weather.current", {"location": "东京", "period": "current"})
        result = await provider(call, _intent(IntentName.WEATHER_QUERY, "东京天气"), RequestContext())
        assert result.status == CapabilityResultStatus.OK
        assert result.data["temperature_c"] == 22.1
        assert result.data["condition_zh"] == "局部多云"
        assert result.source == "Open-Meteo"
        assert result.data["source_url"].startswith("https://")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_open_meteo_forecast_selects_tomorrow():
    client = httpx.AsyncClient(transport=httpx.MockTransport(_weather_transport))
    provider = OpenMeteoWeatherProvider(client=client)
    try:
        call = CapabilityCall("weather.forecast", {"location": "东京", "period": "forecast", "forecast_day": "tomorrow", "query": "明天东京天气"})
        result = await provider(call, _intent(IntentName.WEATHER_QUERY, "明天东京天气"), RequestContext())
        assert result.status == CapabilityResultStatus.OK
        assert result.data["date"] == "2026-10-12"
        assert result.data["temperature_max_c"] == 20
        assert result.data["precipitation_probability_max_pct"] == 70
    finally:
        await client.aclose()


def _wiki_transport(request: httpx.Request) -> httpx.Response:
    params = dict(request.url.params)
    if params.get("list") == "search":
        return httpx.Response(200, json={"query": {"search": [{"title": "光合作用", "snippet": "植物把光能转化为化学能"}]}})
    if params.get("prop") == "extracts|info":
        return httpx.Response(200, json={"query": {"pages": [{"title": "光合作用", "extract": "光合作用是植物、藻类和某些细菌利用光能合成有机物的过程。", "fullurl": "https://zh.wikipedia.org/wiki/光合作用"}]}})
    return httpx.Response(400, json={"error": "unexpected route"})


@pytest.mark.asyncio
async def test_wikipedia_knowledge_search_returns_source_link_and_extract():
    client = httpx.AsyncClient(transport=httpx.MockTransport(_wiki_transport))
    provider = WikipediaKnowledgeProvider(client=client)
    try:
        call = CapabilityCall("knowledge.search", {"query": "什么是光合作用"})
        result = await provider(call, _intent(IntentName.KNOWLEDGE_QUERY, "什么是光合作用"), RequestContext())
        assert result.status == CapabilityResultStatus.OK
        assert result.data["results"][0]["title"] == "光合作用"
        assert "光能" in result.data["results"][0]["extract"]
        assert result.data["results"][0]["url"].startswith("https://zh.wikipedia.org/")
        # Same query hits the provider cache and stays source-backed.
        cached = await provider(call, _intent(IntentName.KNOWLEDGE_QUERY, "什么是光合作用"), RequestContext())
        assert cached.data["cached"] is True
    finally:
        await client.aclose()


def test_knowledge_query_prefers_search_when_provider_is_wired():
    async def ok_provider(call, intent, context):
        from continuity_engine import CapabilityResult
        return CapabilityResult(CapabilityResultStatus.OK, call.capability_id, message="retrieved")
    registry = build_default_registry(llm_available=True, providers={"knowledge.search": ok_provider})
    decision = RequestRouter(registry).route("什么是光合作用？")
    assert decision.status == RouteStatus.READY
    assert decision.capability_ids == ("knowledge.search",)
    assert decision.calls[0].arguments["query"] == "什么是光合作用？"


@pytest.mark.asyncio
async def test_provider_bundle_network_services_are_off_until_explicitly_enabled(tmp_path):
    config = ConfigManager.from_yaml(Path(__file__).resolve().parents[1] / "config" / "default.yaml").get()
    config["capabilities"]["weather_enabled"] = False
    config["capabilities"]["knowledge_search_enabled"] = False
    config["capabilities"]["reminders_enabled"] = False
    bundle = build_v09_provider_bundle(config, reminder_database_path=tmp_path / "no-db.sqlite3")
    try:
        assert "weather.current" not in bundle.providers
        assert "weather.forecast" not in bundle.providers
        assert "knowledge.search" not in bundle.providers
        assert "reminder.create" not in bundle.providers
    finally:
        await bundle.aclose()


@pytest.mark.asyncio
async def test_reminder_background_worker_delivers_without_manual_poll(tmp_path):
    delivery = FakeDelivery()
    delivery.delivered_event = asyncio.Event()
    service = ReminderScheduler(
        tmp_path / "background-reminders.sqlite3", delivery=delivery, poll_interval_s=0.05,
    )
    await service.start()
    try:
        config = ConfigManager.from_yaml(Path(__file__).resolve().parents[1] / "config" / "default.yaml").get()
        config["capabilities"]["reminders_enabled"] = True
        router = build_request_router_from_config(config, providers={
            "reminder.create": service, "reminder.list": service, "reminder.cancel": service,
        })
        context = RequestContext(timezone_name="Asia/Tokyo", now_wall_s=time.time())
        decision = router.route("2秒后提醒我喝水", context)
        result = await RequestDispatcher(router.registry).execute(decision, context)
        assert result.status == CapabilityResultStatus.OK
        reminder_id = result.data["reminder_id"]

        # The scheduler worker, not the test, waits until due and delivers.
        await asyncio.wait_for(delivery.delivered_event.wait(), timeout=5.0)
        stored = await service.get(reminder_id)
        assert stored["status"] == "FIRED"
        assert len(delivery.delivered) == 1
        assert delivery.delivered[0]["text"] == "喝水"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_provider_bundle_routes_reminder_create_list_and_cancel(tmp_path):
    config = ConfigManager.from_yaml(Path(__file__).resolve().parents[1] / "config" / "default.yaml").get()
    config["capabilities"]["reminders_enabled"] = True
    config["capabilities"]["weather_enabled"] = False
    config["capabilities"]["knowledge_search_enabled"] = False
    delivery = FakeDelivery()
    bundle = build_v09_provider_bundle(
        config, reminder_database_path=tmp_path / "bundle-reminders.sqlite3", reminder_delivery=delivery,
    )
    await bundle.start()
    try:
        router = build_request_router_from_config(config, providers=bundle.providers, llm_available=False)
        dispatcher = RequestDispatcher(router.registry)
        context = RequestContext(timezone_name="Asia/Tokyo", now_wall_s=time.time())

        created_decision = router.route("十分钟后提醒我吃饭", context)
        created = await dispatcher.execute(created_decision, context)
        assert created.status == CapabilityResultStatus.OK
        reminder_id = created.data["reminder_id"]

        listed_decision = router.route("我有哪些提醒", context)
        listed = await dispatcher.execute(listed_decision, context)
        assert listed.status == CapabilityResultStatus.OK
        assert listed.data["count"] == 1
        assert listed.data["reminders"][0]["reminder_id"] == reminder_id

        cancelled_decision = router.route("取消吃饭提醒", context)
        cancelled = await dispatcher.execute(cancelled_decision, context)
        assert cancelled.status == CapabilityResultStatus.OK
        assert cancelled.data["status"] == "CANCELLED"
        assert (await bundle.providers["reminder.create"].get(reminder_id))["status"] == "CANCELLED"

        listed_again = await dispatcher.execute(router.route("我有哪些提醒", context), context)
        assert listed_again.status == CapabilityResultStatus.OK
        assert listed_again.data["count"] == 0
    finally:
        await bundle.aclose()
        assert delivery.closed


@pytest.mark.asyncio
async def test_provider_bundle_routes_weather_and_knowledge_with_mock_http():
    config = ConfigManager.from_yaml(Path(__file__).resolve().parents[1] / "config" / "default.yaml").get()
    config["capabilities"]["reminders_enabled"] = False
    config["capabilities"]["weather_enabled"] = True
    config["capabilities"]["knowledge_search_enabled"] = True

    def combined_transport(request: httpx.Request) -> httpx.Response:
        if request.url.host in {"geocoding-api.open-meteo.com", "api.open-meteo.com"}:
            return _weather_transport(request)
        if request.url.host in {"zh.wikipedia.org", "en.wikipedia.org"}:
            # Keep the fixture source-backed and deterministic for integration tests.
            params = dict(request.url.params)
            if params.get("list") == "search":
                query = str(params.get("srsearch", ""))
                title = "珠穆朗玛峰" if "珠穆朗玛峰" in query else "光合作用"
                snippet = "珠穆朗玛峰海拔为可靠资料记载的高度。" if title == "珠穆朗玛峰" else "植物利用光能合成有机物。"
                return httpx.Response(200, json={"query": {"search": [{"title": title, "snippet": snippet}]}})
            if params.get("prop") == "extracts|info":
                page = params.get("titles", "光合作用")
                title = "珠穆朗玛峰" if "珠穆朗玛峰" in str(page) else "光合作用"
                extract = "珠穆朗玛峰的高度应以注明日期和测量口径的正式来源为准。" if title == "珠穆朗玛峰" else "光合作用是生物利用光能合成有机物的过程。"
                return httpx.Response(200, json={"query": {"pages": [{"title": title, "extract": extract, "fullurl": f"https://zh.wikipedia.org/wiki/{title}"}]}})
        return httpx.Response(404, json={"error": "unexpected route"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(combined_transport))
    bundle = build_v09_provider_bundle(config, http_client=client)
    await bundle.start()
    try:
        router = build_request_router_from_config(config, providers=bundle.providers, llm_available=False)
        dispatcher = RequestDispatcher(router.registry)
        context = RequestContext(timezone_name="Asia/Tokyo", default_location="Tokyo")

        weather_decision = router.route("今天天气怎么样", context)
        assert weather_decision.status == RouteStatus.READY
        weather = await dispatcher.execute(weather_decision, context)
        assert weather.status == CapabilityResultStatus.OK
        assert weather.source == "Open-Meteo"
        assert weather.data["location"].startswith("Tokyo")

        knowledge_decision = router.route("珠穆朗玛峰有多高", context)
        assert knowledge_decision.status == RouteStatus.READY
        assert knowledge_decision.capability_ids == ("knowledge.search",)
        knowledge = await dispatcher.execute(knowledge_decision, context)
        assert knowledge.status == CapabilityResultStatus.OK
        assert knowledge.data["results"][0]["title"] == "珠穆朗玛峰"
        assert knowledge.data["results"][0]["url"].startswith("https://zh.wikipedia.org/")
    finally:
        await bundle.aclose()
        await client.aclose()


def test_disabled_network_capabilities_remain_fail_closed_at_router():
    config = ConfigManager.from_yaml(Path(__file__).resolve().parents[1] / "config" / "default.yaml").get()
    config["capabilities"]["weather_enabled"] = False
    config["capabilities"]["knowledge_search_enabled"] = False
    router = build_request_router_from_config(config, providers={}, llm_available=False)
    assert router.route("东京今天天气怎么样").status == RouteStatus.UNSUPPORTED
    assert router.route("搜一下最新的月球探测新闻").status == RouteStatus.UNSUPPORTED

@pytest.mark.asyncio
async def test_reminder_capability_fails_closed_when_notification_backend_is_unhealthy(tmp_path):
    class UnhealthyDelivery(FakeDelivery):
        async def health_check(self):
            return False

    config = ConfigManager.from_yaml(Path(__file__).resolve().parents[1] / "config" / "default.yaml").get()
    config["capabilities"]["reminders_enabled"] = True
    delivery = UnhealthyDelivery()
    bundle = build_v09_provider_bundle(
        config, reminder_database_path=tmp_path / "unavailable-reminders.sqlite3", reminder_delivery=delivery,
    )
    await bundle.start()
    try:
        assert "reminder.create" in bundle.start_errors
        router = build_request_router_from_config(config, providers=bundle.providers)
        decision = router.route("一分钟后提醒我吃饭", RequestContext())
        assert decision.status == RouteStatus.UNSUPPORTED
        assert "reminder.create" in decision.missing_capabilities
    finally:
        await bundle.aclose()
        assert delivery.closed
