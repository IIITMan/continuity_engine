from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from continuity_engine import (
    CapabilityResult,
    CapabilityResultStatus,
    ConfigManager,
    IntentName,
    RequestContext,
    RequestDispatcher,
    RequestRouter,
    RouteStatus,
    build_default_registry,
    build_request_router_from_config,
)


async def ok_provider(call, intent, context):
    return CapabilityResult(
        CapabilityResultStatus.OK,
        call.capability_id,
        message=f"{call.capability_id} executed",
        data={"arguments": dict(call.arguments), "intent": intent.name.value},
        source="test_fixture",
        observed_at_wall_s=context.now_wall_s,
    )


def test_scene_observation_does_not_misclassify_as_user_observation():
    router = RequestRouter(build_default_registry(providers={"vision.observe_scene": ok_provider}))
    decision = router.route("看看我周围")
    assert decision.intent.name == IntentName.OBSERVE_SCENE
    assert decision.capability_ids == ("vision.observe_scene",)


def test_generic_room_observation_routes_to_current_scene():
    router = RequestRouter(build_default_registry(providers={"vision.observe_scene": ok_provider}))
    decision = router.route("看看房间里有没有人")
    assert decision.intent.name == IntentName.OBSERVE_SCENE


def test_named_room_observation_is_not_mistaken_for_current_scene():
    router = RequestRouter(build_default_registry(providers={"vision.observe_location": ok_provider}))
    decision = router.route("看看卧室有没有人", RequestContext(allowed_locations=("卧室",)))
    assert decision.intent.name == IntentName.OBSERVE_LOCATION
    assert decision.intent.slots["location"] == "卧室"


def test_question_about_device_capability_never_toggles_device():
    router = RequestRouter(build_default_registry(providers={"device.control": ok_provider}))
    decision = router.route("你能开灯吗？")
    assert decision.status == RouteStatus.CAPABILITY_QUERY
    assert decision.intent.name == IntentName.CAPABILITY_QUERY
    assert decision.capability_ids == ()
    assert decision.control_event_kind is None


def test_media_control_is_unsupported_until_provider_is_wired():
    decision = RequestRouter(build_default_registry()).route("给我放首歌")
    assert decision.intent.name == IntentName.MEDIA_CONTROL
    assert decision.status == RouteStatus.UNSUPPORTED
    assert decision.missing_capabilities == ("media.control",)


def test_media_control_executes_only_through_registered_provider():
    registry = build_default_registry(providers={"media.control": ok_provider})
    router = RequestRouter(registry)
    context = RequestContext()
    decision = router.route("给我放首歌", context)
    assert decision.status == RouteStatus.READY
    assert decision.calls[0].capability_id == "media.control"
    result = asyncio.run(RequestDispatcher(registry).execute(decision, context))
    assert result.status == CapabilityResultStatus.OK
    assert result.data["arguments"]["command"] == "给我放首歌"


def test_timer_duration_is_normalized_to_seconds():
    registry = build_default_registry(providers={"timer.create": ok_provider})
    router = RequestRouter(registry)
    context = RequestContext()
    decision = router.route("帮我计时十分钟", context)
    assert decision.intent.name == IntentName.TIMER_CREATE
    assert decision.status == RouteStatus.READY
    assert decision.calls[0].arguments["duration_s"] == 600
    result = asyncio.run(RequestDispatcher(registry).execute(decision, context))
    assert result.status == CapabilityResultStatus.OK


def test_timer_missing_duration_clarifies_instead_of_guessing():
    registry = build_default_registry(providers={"timer.create": ok_provider})
    decision = RequestRouter(registry).route("开始计时", request_id="timer-needs-duration")
    assert decision.status == RouteStatus.NEEDS_CLARIFICATION
    assert decision.missing_slots == ("duration_s",)


def test_duration_mentioned_in_normal_question_does_not_create_timer():
    router = RequestRouter(build_default_registry(llm_available=True))
    decision = router.route("过去十分钟发生了什么？")
    assert decision.intent.name == IntentName.SMALL_TALK or decision.intent.name == IntentName.KNOWLEDGE_QUERY
    assert decision.intent.name != IntentName.TIMER_CREATE


def test_unimplemented_imperative_is_clarified_not_faked_as_conversation():
    decision = RequestRouter(build_default_registry(llm_available=True)).route("拍一张照片")
    assert decision.status == RouteStatus.NEEDS_CLARIFICATION
    assert decision.intent.name == IntentName.UNKNOWN


def test_request_routing_can_be_explicitly_disabled():
    project_root = Path(__file__).resolve().parents[1]
    data = ConfigManager.from_yaml(project_root / "config" / "default.yaml").get()
    data["request_routing"]["enabled"] = False
    with pytest.raises(ValueError, match="request_routing.enabled is false"):
        build_request_router_from_config(data)


def test_timer_clarification_parses_followup_duration():
    registry = build_default_registry(providers={"timer.create": ok_provider})
    router = RequestRouter(registry)
    first = router.route("开始计时", request_id="timer-clarify")
    followup = router.answer_clarification(first, "十五分钟", RequestContext())
    assert followup.status == RouteStatus.READY
    assert followup.calls[0].arguments["duration_s"] == 900


def test_capability_query_is_distinct_from_explicit_weather_request():
    registry = build_default_registry(providers={"weather.forecast": ok_provider})
    router = RequestRouter(registry)
    capability = router.route("你能查天气吗")
    request = router.route("你能查一下明天东京的天气吗")
    assert capability.status == RouteStatus.CAPABILITY_QUERY
    assert capability.intent.name == IntentName.CAPABILITY_QUERY
    assert request.intent.name == IntentName.WEATHER_QUERY
    assert request.status == RouteStatus.READY
    assert request.intent.slots["location"] == "东京"


def test_polite_device_request_with_explicit_request_marker_routes_to_action():
    registry = build_default_registry(providers={"device.control": ok_provider})
    decision = RequestRouter(registry).route("你能帮我开灯吗")
    assert decision.intent.name == IntentName.DEVICE_CONTROL
    assert decision.status == RouteStatus.READY


def test_high_risk_capability_fails_closed_without_live_pre_dispatch_guard():
    calls = []

    async def observe(call, intent, context):
        calls.append(call.capability_id)
        return CapabilityResult(CapabilityResultStatus.OK, call.capability_id)

    registry = build_default_registry(providers={"vision.observe_location": observe})
    router = RequestRouter(registry)
    context = RequestContext(allowed_locations=("书房",))
    decision = router.route("看看书房有没有人", context)
    assert decision.status == RouteStatus.READY
    result = asyncio.run(RequestDispatcher(registry).execute(decision, context))
    assert result.status == CapabilityResultStatus.FAILED
    assert result.error_code == "LIVE_PRE_DISPATCH_GUARD_REQUIRED"
    assert calls == []


def test_live_pre_dispatch_guard_is_rechecked_for_every_composite_step():
    calls = []

    async def move(call, intent, context):
        calls.append(call.capability_id)
        return CapabilityResult(CapabilityResultStatus.OK, call.capability_id, data={"arrived": True})

    async def observe(call, intent, context):
        calls.append(call.capability_id)
        return CapabilityResult(CapabilityResultStatus.OK, call.capability_id, data={"occupied": False})

    registry = build_default_registry(mobility_enabled=True, providers={
        "mobility.navigate_to_location": move,
        "vision.observe_location": observe,
    })
    router = RequestRouter(registry)
    context = RequestContext(allowed_locations=("书房",))
    decision = router.route("去书房看看有没有人", context)
    assert decision.status == RouteStatus.READY

    def guard(call, intent, current_context):
        if call.capability_id == "vision.observe_location":
            return "CAMERA_CONSENT_CHANGED"
        return None

    result = asyncio.run(RequestDispatcher(registry, pre_dispatch_guard=guard).execute(decision, context))
    assert result.status == CapabilityResultStatus.FAILED
    assert result.error_code == "PRE_DISPATCH_GUARD_DENIED"
    assert result.data["denial_reason"] == "CAMERA_CONSENT_CHANGED"
    assert calls == ["mobility.navigate_to_location"]


def test_object_observation_is_distinct_from_general_scene_observation():
    registry = build_default_registry(providers={"vision.observe_object": ok_provider})
    router = RequestRouter(registry)
    decision = router.route("识别这个物体")
    assert decision.intent.name == IntentName.OBSERVE_OBJECT
    assert decision.capability_ids == ("vision.observe_object",)
    assert decision.calls[0].arguments["target"] == "referenced_object"


def test_audio_capability_query_does_not_claim_currently_hearing_without_fresh_input():
    registry = build_default_registry(providers={"audio.input": ok_provider})
    router = RequestRouter(registry)
    stale = router.route("你能听见我吗？", RequestContext())
    assert "不能声称正在听到你" in (stale.response_text or "")
    fresh = router.route(
        "你能听见我吗？",
        RequestContext(world_facts={"audio.active": True, "audio.last_input_age_s": 0.5}),
    )
    assert "最近收到了新音频数据" in (fresh.response_text or "")


def test_scene_observation_variants_route_consistently():
    registry = build_default_registry(providers={"vision.observe_scene": ok_provider})
    router = RequestRouter(registry)
    for utterance in ("看看周围", "看一下周围", "看一眼周围", "看一下房间", "你在看什么"):
        assert router.route(utterance).intent.name == IntentName.OBSERVE_SCENE, utterance


def test_room_visibility_question_requests_current_scene_when_provider_is_wired():
    registry = build_default_registry(providers={"vision.observe_scene": ok_provider})
    decision = RequestRouter(registry).route("你能看见房间吗？")
    assert decision.intent.name == IntentName.OBSERVE_SCENE
    assert decision.status == RouteStatus.READY
    assert decision.capability_ids == ("vision.observe_scene",)


def test_camera_existence_question_does_not_activate_vision():
    registry = build_default_registry(providers={"vision.observe_user": ok_provider})
    decision = RequestRouter(registry).route("你有摄像头吗？")
    assert decision.intent.name == IntentName.CAPABILITY_QUERY
    assert decision.status == RouteStatus.CAPABILITY_QUERY
    assert decision.calls == ()

@pytest.mark.parametrize(
    ("utterance", "expected_intent"),
    [
        ("闭嘴", IntentName.QUIET_ENABLE),
        ("安静点", IntentName.QUIET_ENABLE),
        ("别吵", IntentName.QUIET_ENABLE),
        ("别再说了", IntentName.QUIET_ENABLE),
        ("停一下", IntentName.STOP),
        ("先停一下", IntentName.STOP),
    ],
)
def test_short_control_synonyms_route_to_local_control(utterance, expected_intent):
    router = RequestRouter(build_default_registry())
    decision = router.route(utterance)
    assert decision.status == RouteStatus.CONTROL_ACTION
    assert decision.intent.name == expected_intent
    assert decision.control_event_kind is not None


def test_pause_media_phrase_is_not_captured_by_standalone_stop_alias():
    registry = build_default_registry(providers={"media.control": ok_provider})
    decision = RequestRouter(registry).route("暂停音乐")
    assert decision.intent.name == IntentName.MEDIA_CONTROL
    assert decision.status == RouteStatus.READY


@pytest.mark.parametrize(
    "utterance",
    [
        "你能听见我说话吗",
        "你听得到我说话吗",
        "你听得见我吗",
        "现在能听到我吗",
        "麦克风正常吗",
    ],
)
def test_microphone_status_synonyms_route_to_audio_capability(utterance):
    registry = build_default_registry(providers={"audio.input": ok_provider})
    router = RequestRouter(registry)
    decision = router.route(utterance, RequestContext(
        world_facts={"audio.active": True, "audio.last_input_age_s": 0.5}
    ))
    assert decision.intent.name == IntentName.CAPABILITY_QUERY
    assert decision.status == RouteStatus.CAPABILITY_QUERY
    assert decision.intent.slots["capability_id"] == "audio.input"
    assert "最近收到了新音频数据" in (decision.response_text or "")


def test_composite_observation_generalizes_to_living_room_and_unknown_objects():
    registry = build_default_registry(mobility_enabled=True, providers={
        "mobility.navigate_to_location": ok_provider,
        "vision.observe_location": ok_provider,
    })
    context = RequestContext(allowed_locations=("客厅",))
    decision = RequestRouter(registry).route("你去客厅看看有什么", context)
    assert decision.intent.name == IntentName.COMPOSITE_OBSERVE_LOCATION
    assert decision.status == RouteStatus.READY
    assert decision.intent.slots["location"] == "客厅"
    assert tuple(call.capability_id for call in decision.calls) == (
        "mobility.navigate_to_location", "vision.observe_location"
    )
    assert decision.calls[1].arguments["query"] == "你去客厅看看有什么"


def test_location_observation_without_navigation_accepts_open_ended_question():
    registry = build_default_registry(providers={"vision.observe_location": ok_provider})
    context = RequestContext(allowed_locations=("客厅",))
    decision = RequestRouter(registry).route("客厅有什么", context)
    assert decision.intent.name == IntentName.OBSERVE_LOCATION
    assert decision.status == RouteStatus.READY
    assert decision.calls[0].arguments["query"] == "客厅有什么"


def test_device_control_recognizes_turn_off_light_paraphrase():
    registry = build_default_registry(providers={"device.control": ok_provider})
    decision = RequestRouter(registry).route("把灯关掉")
    assert decision.intent.name == IntentName.DEVICE_CONTROL
    assert decision.status == RouteStatus.READY
    assert decision.calls[0].arguments["command"] == "把灯关掉"
