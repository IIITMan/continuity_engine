from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from continuity_engine import (
    CapabilityRegistry, CapabilityResult, CapabilityResultStatus, CapabilitySpec,
    ContinuityEngine, ConfigManager, IntentName, RequestContext, RequestDispatcher,
    RequestDomain, RequestRouter, RiskLevel, RouteStatus, build_default_registry,
    make_engine_control_event_sink,
)
from continuity_engine.core.models import EventKind


async def ok_provider(call, intent, context):
    return CapabilityResult(
        CapabilityResultStatus.OK, call.capability_id,
        message=f"{call.capability_id} done",
        data={"args": dict(call.arguments), "intent": intent.name.value},
        source="test_provider", observed_at_wall_s=context.now_wall_s,
    )


@pytest.fixture
def registry():
    return build_default_registry(
        llm_available=True,
        task_planner_available=True,
        mobility_enabled=True,
        providers={
            "weather.current": ok_provider,
            "weather.forecast": ok_provider,
            "knowledge.search": ok_provider,
            "vision.observe_user": ok_provider,
            "vision.observe_location": ok_provider,
            "mobility.navigate_to_user": ok_provider,
            "mobility.navigate_to_location": ok_provider,
            "reminder.create": ok_provider,
            "device.control": ok_provider,
        },
    )


def test_time_query_routes_to_builtin_provider_and_uses_timezone(registry):
    router = RequestRouter(registry)
    # 2026-10-11T00:00:00Z -> 09:00 in Tokyo
    from datetime import timezone
    ts = datetime(2026, 10, 11, 0, 0, tzinfo=timezone.utc).timestamp()
    ctx = RequestContext(timezone_name="Asia/Tokyo", now_wall_s=ts)
    decision = router.route("现在几点？", ctx)
    assert decision.intent.name == IntentName.TIME_QUERY
    assert decision.status == RouteStatus.READY

    import asyncio
    result = asyncio.run(RequestDispatcher(registry).execute(decision, ctx))
    assert result.status == CapabilityResultStatus.OK
    assert result.data["time"] == "09:00:00"
    assert result.data["timezone"] == "Asia/Tokyo"


def test_weather_clarifies_location_then_routes(registry):
    router = RequestRouter(registry)
    ctx = RequestContext(conversation_id="weather-chat")
    first = router.route("明天天气怎么样？", ctx, request_id="weather-1")
    assert first.status == RouteStatus.NEEDS_CLARIFICATION
    assert first.missing_slots == ("location",)
    second = router.answer_clarification(first, "西雅图", ctx)
    assert second.status == RouteStatus.READY
    assert second.intent.slots["location"] == "西雅图"
    assert second.calls[0].arguments["location"] == "西雅图"
    assert second.capability_ids == ("weather.forecast",)


def test_weather_not_configured_is_reported_before_asking_location():
    router = RequestRouter(build_default_registry())
    decision = router.route("明天天气怎么样？")
    assert decision.status == RouteStatus.UNSUPPORTED
    assert "weather.forecast" in decision.missing_capabilities
    assert not decision.question


def test_can_you_see_me_requests_a_fresh_visual_observation(registry):
    router = RequestRouter(registry)
    decision = router.route("你能看见我吗？", RequestContext())
    assert decision.status == RouteStatus.READY
    assert decision.intent.name == IntentName.OBSERVE_USER
    assert decision.capability_ids == ("vision.observe_user",)
    assert decision.calls[0].arguments["query"] == "current_visibility_check"


def test_visual_request_without_provider_is_unsupported():
    decision = RequestRouter(build_default_registry()).route("你能看见我吗？")
    assert decision.intent.name == IntentName.OBSERVE_USER
    assert decision.status == RouteStatus.UNSUPPORTED
    assert decision.missing_capabilities == ("vision.observe_user",)


def test_camera_status_query_is_read_only_and_reports_only_known_state(registry):
    router = RequestRouter(registry)
    current = router.route("摄像头开着吗？", RequestContext(world_facts={"camera.active": True}))
    assert current.status == RouteStatus.CAPABILITY_QUERY
    assert current.intent.name == IntentName.CAPABILITY_QUERY
    assert "摄像头已开启" in (current.response_text or "")

    unknown = router.route("摄像头开着吗？", RequestContext())
    assert "无法确认摄像头当前是否开启" in (unknown.response_text or "")


def test_quiet_instruction_is_control_action_without_llm(registry):
    router = RequestRouter(registry)
    decision = router.route("你不要打扰我好不好")
    assert decision.status == RouteStatus.CONTROL_ACTION
    assert decision.control_event_kind == EventKind.USER_QUIET.value
    assert decision.control_payload == {"enabled": True, "scope": "SESSION"}


def test_persistent_quiet_and_unquiet(registry):
    router = RequestRouter(registry)
    on = router.route("以后都不要主动打扰我")
    off = router.route("解除安静模式")
    assert on.control_payload["scope"] == "PERSISTENT"
    assert off.control_payload == {"enabled": False}


def test_location_composite_requires_mobility_and_vision(registry):
    router = RequestRouter(registry)
    ctx = RequestContext(allowed_locations=("卧室",))
    decision = router.route("你去卧室看看有没有人", ctx, request_id="room-check-1")
    assert decision.status == RouteStatus.NEEDS_CONFIRMATION
    confirmed = router.route_confirmed(decision, ctx, True)
    assert confirmed.status == RouteStatus.READY
    assert [c.capability_id for c in confirmed.calls] == ["mobility.navigate_to_location", "vision.observe_location"]


def test_private_room_check_stops_after_confirmation_declined(registry):
    router = RequestRouter(registry)
    ctx = RequestContext(allowed_locations=("卧室",))
    decision = router.route("去卧室看看有没有人")
    # A provider may be present, but unconfigured navigation maps fail closed.
    assert decision.status == RouteStatus.BLOCKED
    assert decision.reason_code == "LOCATION_MAP_NOT_CONFIGURED"
    decision = router.route("去卧室看看有没有人", ctx, request_id="room-check-2")
    assert decision.status == RouteStatus.NEEDS_CONFIRMATION
    denied = router.route_confirmed(decision, ctx, False)
    assert denied.status == RouteStatus.BLOCKED
    assert denied.reason_code == "USER_DECLINED"


def test_move_to_user_fails_closed_without_localization(registry):
    router = RequestRouter(registry)
    decision = router.route("来我这里")
    assert decision.status == RouteStatus.BLOCKED
    assert decision.reason_code == "USER_LOCATION_UNKNOWN"


def test_movement_capability_disabled_by_default():
    router = RequestRouter(build_default_registry())
    decision = router.route("来我这里")
    assert decision.status == RouteStatus.UNSUPPORTED
    assert decision.missing_capabilities == ("mobility.navigate_to_user",)


def test_observe_location_requires_allowlisted_map(registry):
    router = RequestRouter(registry)
    decision = router.route("看看卧室有没有人", RequestContext())
    assert decision.status == RouteStatus.BLOCKED
    assert decision.reason_code == "LOCATION_MAP_NOT_CONFIGURED"


def test_observe_user_routes_to_vision_provider(registry):
    router = RequestRouter(registry)
    decision = router.route("看一下我")
    assert decision.status == RouteStatus.READY
    assert decision.capability_ids == ("vision.observe_user",)


def test_current_info_requires_search_not_model_guess(registry):
    router = RequestRouter(registry)
    decision = router.route("查一下最新的政策")
    assert decision.status == RouteStatus.READY
    assert decision.capability_ids == ("knowledge.search",)


def test_basic_knowledge_prefers_retrieval_when_search_provider_is_wired(registry):
    router = RequestRouter(registry)
    decision = router.route("什么是光合作用？")
    assert decision.status == RouteStatus.READY
    assert decision.capability_ids == ("knowledge.search",)


def test_small_talk_delegates_to_llm(registry):
    router = RequestRouter(registry)
    decision = router.route("陪我聊聊天")
    assert decision.status == RouteStatus.DELEGATE_LLM
    assert decision.intent.name == IntentName.SMALL_TALK


def test_reminder_clarifies_time_then_calls_provider(registry):
    router = RequestRouter(registry)
    first = router.route("提醒我多喝水", request_id="remind-1")
    assert first.status == RouteStatus.NEEDS_CLARIFICATION
    assert first.missing_slots == ("time",)
    done = router.answer_clarification(first, "下午三点", RequestContext())
    assert done.status == RouteStatus.READY
    assert done.calls[0].arguments["time"] == "下午三点"


def test_clarification_limit_prevents_endless_questioning(registry):
    router = RequestRouter(registry, clarification_max_rounds=2)
    first = router.route("查天气", request_id="weather-limited")
    # Weather provider exists but location omitted; first clarification asks location.
    assert first.status == RouteStatus.NEEDS_CLARIFICATION
    second = router.answer_clarification(first, "", RequestContext())
    assert second.status == RouteStatus.NEEDS_CLARIFICATION
    third = router.answer_clarification(second, "", RequestContext())
    assert third.reason_code == "CLARIFICATION_LIMIT_REACHED"
    assert router.answer_clarification(third, "东京", RequestContext()).status == RouteStatus.INVALID


def test_device_control_unavailable_is_not_fabricated(registry):
    registry_no_device = build_default_registry(llm_available=True)
    decision = RequestRouter(registry_no_device).route("打开空调")
    assert decision.status == RouteStatus.UNSUPPORTED
    assert "device.control" in decision.missing_capabilities


def test_general_task_delegates_to_task_planner_only_if_enabled(registry):
    decision = RequestRouter(registry).route("帮我安排一个简单任务")
    assert decision.status == RouteStatus.DELEGATE_TASK_PLANNER
    unavailable = RequestRouter(build_default_registry()).route("帮我安排一个简单任务")
    assert unavailable.status == RouteStatus.UNSUPPORTED


def test_composite_dispatch_is_sequential_and_stops_after_failure():
    order = []
    async def move(call, intent, context):
        order.append("move")
        return CapabilityResult(CapabilityResultStatus.OK, call.capability_id, data={"arrived": True})
    async def observe(call, intent, context):
        order.append("observe")
        return CapabilityResult(CapabilityResultStatus.FAILED, call.capability_id, error_code="CAMERA_FAIL")
    reg = build_default_registry(mobility_enabled=True, providers={
        "mobility.navigate_to_location": move,
        "vision.observe_location": observe,
    })
    # Re-register plan has only movement and vision active; a bedroom is explicitly mapped.
    router = RequestRouter(reg)
    decision = router.route("去书房看看有没有人", RequestContext(allowed_locations=("书房",)))
    assert decision.status == RouteStatus.READY
    import asyncio
    result = asyncio.run(RequestDispatcher(
        reg, pre_dispatch_guard=lambda call, intent, context: None
    ).execute(decision, RequestContext(allowed_locations=("书房",))))
    assert result.status == CapabilityResultStatus.FAILED
    assert order == ["move", "observe"]
    assert result.data["steps"][-1]["error_code"] == "CAMERA_FAIL"


def test_engine_control_sink_injects_but_does_not_step_engine():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    config = ConfigManager.from_yaml(root / "config" / "default.yaml")
    engine = ContinuityEngine(config)
    sink = __import__("continuity_engine").make_engine_control_event_sink(engine)
    assert sink(EventKind.USER_QUIET.value, {"enabled": True, "scope": "SESSION"})
    assert engine.state.quiet_scope is None  # event is queued; host scheduler owns step()
    engine.step(dt=0.0)
    assert engine.state.quiet_scope == "SESSION"
    engine.close()


def test_empty_and_oversize_request_are_rejected():
    router = RequestRouter(build_default_registry())
    assert router.route("   ").status == RouteStatus.INVALID
    assert router.route("x" * 10001).reason_code == "REQUEST_TOO_LARGE"


def test_english_high_confidence_control_and_visual_capability_phrases(registry):
    router = RequestRouter(registry)
    assert router.route("Don't bother me, please").status == RouteStatus.CONTROL_ACTION
    see_me = router.route("Can you see me?")
    assert see_me.intent.name == IntentName.OBSERVE_USER
    assert see_me.status == RouteStatus.READY  # fixture registry wires the camera provider
    assert router.route("What time is it?").status == RouteStatus.READY


def test_config_builder_respects_product_hardware_flags():
    from pathlib import Path
    from continuity_engine import build_request_router_from_config
    root = Path(__file__).resolve().parents[1]
    config = ConfigManager.from_yaml(root / "config" / "default.yaml")
    router = build_request_router_from_config(
        config,
        providers={"vision.observe_user": ok_provider, "mobility.navigate_to_user": ok_provider,
                   "weather.current": ok_provider},
        llm_available=True,
    )
    assert not router.registry.is_available("vision.observe_user")  # camera disabled in product profile
    assert not router.registry.is_available("mobility.navigate_to_user")  # static desktop profile
    assert not router.registry.is_available("weather.current")  # provider feature is not enabled in config


def test_capability_list_does_not_claim_unavailable_vision(registry):
    router = RequestRouter(build_default_registry(llm_available=True))
    decision = router.route("你有什么功能？")
    assert decision.status == RouteStatus.CAPABILITY_QUERY
    assert "视觉观察" not in (decision.response_text or "")
    assert "日常闲聊" in (decision.response_text or "")


def test_english_compound_request_is_classified_but_not_faked_when_disabled():
    router = RequestRouter(build_default_registry())
    decision = router.route("Go to the bedroom and see if anyone is there.")
    assert decision.intent.name == IntentName.COMPOSITE_OBSERVE_LOCATION
    assert decision.status == RouteStatus.UNSUPPORTED


def test_english_weather_location_extraction():
    reg = build_default_registry(providers={"weather.forecast": ok_provider})
    # Config is not involved here: directly injected provider is an explicit opt-in.
    decision = RequestRouter(reg).route("weather forecast for Boston tomorrow")
    assert decision.intent.name == IntentName.WEATHER_QUERY
    assert decision.intent.slots["period"] == "forecast"
    assert decision.intent.slots["location"] == "Boston"
