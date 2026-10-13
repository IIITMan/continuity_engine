"""Generalized user-request routing for Continuity Engine.

The router separates intent understanding from capability availability and execution.
It does not invoke an LLM, sensor, network, or actuator implicitly. A host may inject
capability providers and then explicitly dispatch the returned decision.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import json
import re
import time
import uuid
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..core.models import EventKind, Priority, thaw, validate_json_compatible


class RequestDomain(str, Enum):
    CONVERSATION = "CONVERSATION"
    INFORMATION = "INFORMATION"
    CAPABILITY = "CAPABILITY"
    CONTROL = "CONTROL"
    PERCEPTION = "PERCEPTION"
    MOBILITY = "MOBILITY"
    DEVICE_CONTROL = "DEVICE_CONTROL"
    MEDIA = "MEDIA"
    REMINDER = "REMINDER"
    TASK = "TASK"
    UNKNOWN = "UNKNOWN"


class IntentName(str, Enum):
    SMALL_TALK = "SMALL_TALK"
    KNOWLEDGE_QUERY = "KNOWLEDGE_QUERY"
    CURRENT_INFO_QUERY = "CURRENT_INFO_QUERY"
    TIME_QUERY = "TIME_QUERY"
    WEATHER_QUERY = "WEATHER_QUERY"
    CAPABILITY_QUERY = "CAPABILITY_QUERY"
    CAPABILITY_LIST_QUERY = "CAPABILITY_LIST_QUERY"
    QUIET_ENABLE = "QUIET_ENABLE"
    QUIET_DISABLE = "QUIET_DISABLE"
    STOP = "STOP"
    CANCEL = "CANCEL"
    OBSERVE_USER = "OBSERVE_USER"
    OBSERVE_OBJECT = "OBSERVE_OBJECT"
    OBSERVE_SCENE = "OBSERVE_SCENE"
    OBSERVE_LOCATION = "OBSERVE_LOCATION"
    MOVE_TO_USER = "MOVE_TO_USER"
    MOVE_TO_LOCATION = "MOVE_TO_LOCATION"
    COMPOSITE_OBSERVE_LOCATION = "COMPOSITE_OBSERVE_LOCATION"
    REMINDER_CREATE = "REMINDER_CREATE"
    REMINDER_LIST = "REMINDER_LIST"
    REMINDER_CANCEL = "REMINDER_CANCEL"
    DEVICE_CONTROL = "DEVICE_CONTROL"
    MEDIA_CONTROL = "MEDIA_CONTROL"
    TIMER_CREATE = "TIMER_CREATE"
    GENERAL_TASK = "GENERAL_TASK"
    UNKNOWN = "UNKNOWN"


class RouteStatus(str, Enum):
    READY = "READY"
    DELEGATE_LLM = "DELEGATE_LLM"
    DELEGATE_TASK_PLANNER = "DELEGATE_TASK_PLANNER"
    CAPABILITY_QUERY = "CAPABILITY_QUERY"
    CONTROL_ACTION = "CONTROL_ACTION"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
    UNSUPPORTED = "UNSUPPORTED"
    BLOCKED = "BLOCKED"
    INVALID = "INVALID"


class CapabilityResultStatus(str, Enum):
    OK = "OK"
    NEEDS_INPUT = "NEEDS_INPUT"
    UNAVAILABLE = "UNAVAILABLE"
    STALE = "STALE"
    NOT_FOUND = "NOT_FOUND"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class RequestContext:
    """A sanitized context from the host application; no raw sensor data by default."""
    conversation_id: str = "default"
    timezone_name: str = "Asia/Tokyo"
    default_location: str | None = None
    now_wall_s: float | None = None
    user_speaking: bool = False
    quiet_scope: str | None = None
    safety_stopped: bool = False
    user_location_known: bool = False
    allowed_locations: tuple[str, ...] = ()
    protected_locations: tuple[str, ...] = ("卧室", "浴室", "卫生间", "bathroom")
    world_facts: Mapping[str, Any] = field(default_factory=dict)
    confirmed_request_ids: frozenset[str] = frozenset()
    conversation_context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.conversation_id:
            raise ValueError("conversation_id must be non-empty")
        if self.now_wall_s is not None and (isinstance(self.now_wall_s, bool) or not isinstance(self.now_wall_s, (int, float))):
            raise ValueError("now_wall_s must be a finite timestamp")
        if self.now_wall_s is not None:
            import math
            if not math.isfinite(float(self.now_wall_s)):
                raise ValueError("now_wall_s must be finite")
        validate_json_compatible(dict(self.world_facts), "request_context.world_facts")
        validate_json_compatible(dict(self.conversation_context), "request_context.conversation_context")
        if len(json.dumps(dict(self.conversation_context), ensure_ascii=False)) > 12_000:
            raise ValueError("request_context.conversation_context exceeds 12000 characters")


@dataclass(frozen=True, slots=True)
class ClassifiedIntent:
    name: IntentName
    domain: RequestDomain
    raw_text: str
    normalized_text: str
    slots: Mapping[str, Any] = field(default_factory=dict)
    confidence: float = 0.8
    explicit_command: bool = False
    requires_llm: bool = False
    compound: bool = False
    source: str = "RULE"  # RULE, HOST, LLM; LLM-derived high-impact actions require confirmation.
    clarification_question: str | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be in [0,1]")
        validate_json_compatible(dict(self.slots), "classified_intent.slots")


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    capability_id: str
    domain: RequestDomain
    description: str
    enabled: bool = True
    execution_mode: str = "PROVIDER"  # BUILTIN, PROVIDER, LLM, TASK_PLANNER, CONTROL
    required_slots: tuple[str, ...] = ()
    risk_level: RiskLevel = RiskLevel.LOW
    side_effect: bool = False
    confirm_below_confidence: float | None = None
    freshness_s: float | None = None

    def __post_init__(self) -> None:
        if not self.capability_id or not self.description:
            raise ValueError("capability_id and description are required")
        if self.execution_mode not in {"BUILTIN", "PROVIDER", "LLM", "TASK_PLANNER", "CONTROL"}:
            raise ValueError("invalid capability execution_mode")
        if self.confirm_below_confidence is not None and not 0 <= self.confirm_below_confidence <= 1:
            raise ValueError("confirm_below_confidence must be in [0,1]")
        if self.freshness_s is not None and self.freshness_s <= 0:
            raise ValueError("freshness_s must be > 0")


@dataclass(frozen=True, slots=True)
class CapabilityCall:
    capability_id: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    step_id: str = "step-1"

    def __post_init__(self) -> None:
        validate_json_compatible(dict(self.arguments), "capability_call.arguments")


@dataclass(frozen=True, slots=True)
class RequestDecision:
    request_id: str
    conversation_id: str
    intent: ClassifiedIntent
    status: RouteStatus
    calls: tuple[CapabilityCall, ...] = ()
    capability_ids: tuple[str, ...] = ()
    missing_capabilities: tuple[str, ...] = ()
    missing_slots: tuple[str, ...] = ()
    question: str | None = None
    response_text: str | None = None
    control_event_kind: str | None = None
    control_payload: Mapping[str, Any] = field(default_factory=dict)
    requires_confirmation: bool = False
    reason_code: str = "ROUTED"

    def __post_init__(self) -> None:
        validate_json_compatible(dict(self.control_payload), "request_decision.control_payload")


@dataclass(frozen=True, slots=True)
class CapabilityResult:
    status: CapabilityResultStatus
    capability_id: str
    message: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    source: str | None = None
    observed_at_wall_s: float | None = None
    expires_at_wall_s: float | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, CapabilityResultStatus) or not self.capability_id:
            raise ValueError("capability result requires a valid status and capability_id")
        validate_json_compatible(dict(self.data), "capability_result.data")
        for name, value in (("observed_at_wall_s", self.observed_at_wall_s), ("expires_at_wall_s", self.expires_at_wall_s)):
            if value is not None:
                import math
                if not math.isfinite(value):
                    raise ValueError(f"{name} must be finite")


class CapabilityHandler(Protocol):
    async def __call__(self, call: CapabilityCall, intent: ClassifiedIntent, context: RequestContext) -> CapabilityResult: ...


class IntentClassifier(Protocol):
    def classify(self, text: str) -> ClassifiedIntent: ...


@dataclass
class _Registration:
    spec: CapabilitySpec
    handler: CapabilityHandler | None = None


class CapabilityRegistry:
    """Explicit registry: capabilities are unavailable until enabled and wired."""
    def __init__(self) -> None:
        self._items: dict[str, _Registration] = {}

    def register(self, spec: CapabilitySpec, handler: CapabilityHandler | None = None) -> None:
        if spec.capability_id in self._items:
            raise ValueError(f"capability already registered: {spec.capability_id}")
        if spec.execution_mode == "PROVIDER" and spec.enabled and handler is None:
            # An enabled provider without an implementation would be a false capability claim.
            spec = CapabilitySpec(
                capability_id=spec.capability_id, domain=spec.domain, description=spec.description,
                enabled=False, execution_mode=spec.execution_mode, required_slots=spec.required_slots,
                risk_level=spec.risk_level, side_effect=spec.side_effect,
                confirm_below_confidence=spec.confirm_below_confidence, freshness_s=spec.freshness_s,
            )
        self._items[spec.capability_id] = _Registration(spec, handler)

    def get(self, capability_id: str) -> CapabilitySpec | None:
        item = self._items.get(capability_id)
        return item.spec if item else None

    def handler(self, capability_id: str) -> CapabilityHandler | None:
        item = self._items.get(capability_id)
        return item.handler if item else None

    def is_available(self, capability_id: str) -> bool:
        item = self._items.get(capability_id)
        if item is None or not item.spec.enabled:
            return False
        if item.spec.execution_mode == "PROVIDER":
            if item.handler is None:
                return False
            # Lifecycle-managed providers can report unavailable until started or
            # after a delivery backend fails health checks. Providers without this
            # optional attribute retain the original explicit-registration behavior.
            dynamic = getattr(item.handler, "available", None)
            if isinstance(dynamic, bool):
                return dynamic
            return True
        return True

    def availability(self) -> dict[str, bool]:
        return {key: self.is_available(key) for key in sorted(self._items)}

    def describe(self) -> list[dict[str, Any]]:
        return [{"capability_id": c.capability_id, "domain": c.domain.value,
                 "description": c.description, "enabled": c.enabled,
                 "available": self.is_available(c.capability_id),
                 "execution_mode": c.execution_mode, "risk_level": c.risk_level.value}
                for c in (item.spec for item in self._items.values())]

    async def invoke(self, call: CapabilityCall, intent: ClassifiedIntent, context: RequestContext) -> CapabilityResult:
        item = self._items.get(call.capability_id)
        if item is None or not self.is_available(call.capability_id):
            return CapabilityResult(CapabilityResultStatus.UNAVAILABLE, call.capability_id,
                                    message=f"能力 {call.capability_id} 当前未接入。", error_code="CAPABILITY_UNAVAILABLE")
        if item.handler is None:
            return CapabilityResult(CapabilityResultStatus.UNKNOWN, call.capability_id,
                                    message="此能力需要宿主的 LLM 或任务规划适配器处理。", error_code="DELEGATE_REQUIRED")
        return await item.handler(call, intent, context)


class ClockProvider:
    async def __call__(self, call: CapabilityCall, intent: ClassifiedIntent, context: RequestContext) -> CapabilityResult:
        now = time.time() if context.now_wall_s is None else float(context.now_wall_s)
        try:
            dt = datetime.fromtimestamp(now, tz=ZoneInfo(context.timezone_name))
        except (ValueError, ZoneInfoNotFoundError):
            return CapabilityResult(CapabilityResultStatus.FAILED, call.capability_id,
                                    message="时区配置无效，暂时无法确认本地时间。", error_code="INVALID_TIMEZONE")
        data = {"date": dt.strftime("%Y-%m-%d"), "time": dt.strftime("%H:%M:%S"),
                "weekday": dt.strftime("%A"), "timezone": context.timezone_name, "timestamp": now}
        return CapabilityResult(CapabilityResultStatus.OK, call.capability_id,
                                message=f"现在是 {dt.strftime('%Y年%m月%d日')} {dt.strftime('%H:%M:%S')}。",
                                data=data, source="system_clock", observed_at_wall_s=now)


class RuleBasedIntentClassifier:
    """Conservative high-precision rules for common requests, especially direct controls."""
    _quiet_on = (
        "不要打扰我", "别打扰我", "别烦我", "先别说话", "安静一会", "安静一下",
        "安静点", "安静一点", "别吵", "别吵了", "闭嘴", "别说了", "别再说了",
        "不要再说话", "不要说话", "别出声", "不要主动说话", "不要主动打扰我",
        "以后都不要主动打扰我", "别主动找我", "不用主动提醒我", "别主动提醒我",
        "以后不主动找我", "dontbotherme", "don'tbotherme", "donotbotherme",
        "pleasebequiet", "stoptalking", "shutup", "bequiet"
    )
    _quiet_off = ("可以说话了", "解除安静", "退出安静模式", "可以主动找我了", "恢复主动提醒", "youcantalknow", "resumetalking")
    _stop = ("紧急停止", "立即停止", "停止所有动作", "停下来", "emergencystop", "stopallactions", "stopmoving")
    _cancel = ("取消当前任务", "取消这个任务", "算了别做了", "取消吧", "cancelthistask", "nevermind")
    _capability_prefix = ("你能", "你可以", "你会", "你有没有能力", "你是否能", "你能不能")

    def classify(self, text: str) -> ClassifiedIntent:
        if not isinstance(text, str) or not text.strip():
            return ClassifiedIntent(IntentName.UNKNOWN, RequestDomain.UNKNOWN, str(text or ""), "", confidence=0.0)
        raw = text.strip()
        t = re.sub(r"\s+", "", raw.lower())
        def result(name: IntentName, domain: RequestDomain, *, slots: dict[str, Any] | None = None,
                   conf: float = 0.92, explicit: bool = False, llm: bool = False, compound: bool = False) -> ClassifiedIntent:
            return ClassifiedIntent(name, domain, raw, t, slots or {}, conf, explicit, llm, compound)

        # Safety/user control always precedes ordinary intent recognition.
        # Short standalone stop phrases are accepted as direct control, but we avoid
        # matching the same substring inside an ordinary media/device command.
        punctuation_trimmed = t.strip("，。！？!?.,;；:：、\"'“”‘’ ")
        if any(p in t for p in self._stop) or punctuation_trimmed in {"停一下", "先停一下", "暂停一下", "先暂停一下"}:
            return result(IntentName.STOP, RequestDomain.CONTROL, conf=0.99, explicit=True)
        if any(p in t for p in self._cancel):
            return result(IntentName.CANCEL, RequestDomain.CONTROL, conf=0.98, explicit=True)
        if any(p in t for p in self._quiet_off):
            return result(IntentName.QUIET_DISABLE, RequestDomain.CONTROL, conf=0.98, explicit=True)
        if any(p in t for p in self._quiet_on):
            scope = "PERSISTENT" if any(x in t for x in ("以后都", "一直", "永久", "以后不要主动")) else "SESSION"
            return result(IntentName.QUIET_ENABLE, RequestDomain.CONTROL, slots={"scope": scope}, conf=0.99, explicit=True)

        if any(p in t for p in ("你能做什么", "你会做什么", "你有什么功能", "你有哪些能力", "能帮我做什么")):
            return result(IntentName.CAPABILITY_LIST_QUERY, RequestDomain.CAPABILITY)

        # A question can still be an action request: "can you see me?" asks the
        # robot to check its current visual input. Keep pure hardware/capability
        # questions separate so they never activate a sensor.
        if any(p in t for p in ("你能看见我吗", "你能看到我吗", "你看得见我吗", "你看得到我吗",
                                "你看见我了吗", "你看到我了吗", "现在看得到我吗", "你能看见我",
                                "canyouseeme", "canyousemenow")):
            return result(IntentName.OBSERVE_USER, RequestDomain.PERCEPTION,
                          slots={"target": "user", "query": "current_visibility_check"}, explicit=True)
        if any(p in t for p in ("摄像头开着吗", "摄像头开了吗", "你有视觉吗", "你有摄像头吗",
                                "do you have a camera", "isyourcameraon", "doyouhaveacamera")):
            return result(IntentName.CAPABILITY_QUERY, RequestDomain.CAPABILITY,
                          slots={"capability_id": "vision.observe_user", "question_kind": "camera_status"})
        if any(p in t for p in ("你会走路吗", "你能移动吗", "你能走过来吗", "你有移动能力吗", "canyouwalk", "canyoumove")):
            return result(IntentName.CAPABILITY_QUERY, RequestDomain.CAPABILITY, slots={"capability_id": "mobility.navigate_to_user"})
        if any(p in t for p in (
            "你能听见我吗", "你能听见我说话吗", "你听得到我吗", "你听得到我说话吗",
            "你听得见我吗", "你听得见我说话吗", "你能听到我吗", "你能听到我说话吗",
            "现在能听到我吗", "现在能听见我吗", "麦克风能用吗", "麦克风正常吗",
            "麦克风有没有声音", "麦克风有声音吗", "音频输入正常吗",
            "canyouhearme", "canyouhearmespeaking", "isthemicrophoneworking"
        )):
            return result(IntentName.CAPABILITY_QUERY, RequestDomain.CAPABILITY,
                          slots={"capability_id": "audio.input", "question_kind": "microphone_status"})
        if any(p in t for p in ("你能说话吗", "你会说话吗", "你有语音吗", "canyouspeak", "canyoutalk")):
            return result(IntentName.CAPABILITY_QUERY, RequestDomain.CAPABILITY, slots={"capability_id": "audio.output"})
        if any(p in t for p in ("你能看见房间吗", "你能看到房间吗", "你能看到周围吗", "你现在能看到房间吗")):
            return result(IntentName.OBSERVE_SCENE, RequestDomain.PERCEPTION,
                          slots={"target": "current_scene", "query": "current_visibility_check"}, explicit=True)
        if any(p in t for p in ("你能识别物体吗", "你会识别物体吗", "你能看懂这个东西吗", "canyouidentifyobjects")):
            return result(IntentName.CAPABILITY_QUERY, RequestDomain.CAPABILITY, slots={"capability_id": "vision.observe_object"})
        if any(p in t for p in ("你能查天气吗", "你会查天气吗", "你有天气查询能力吗", "canyoucheckweather")):
            return result(IntentName.CAPABILITY_QUERY, RequestDomain.CAPABILITY, slots={"capability_id": "weather.current"})
        if any(p in t for p in ("现在几点", "现在时间", "几点了", "今天几号", "今天星期几", "今天周几", "现在是几月几号", "当前日期", "whattimeisit", "whatisthetime", "whatdateisit", "whatdayisit")):
            return result(IntentName.TIME_QUERY, RequestDomain.INFORMATION)

        if any(p in t for p in ("天气", "会下雨吗", "温度多少", "多少度", "降雨概率", "要带伞吗", "weather", "temperature", "willitrain", "chanceofrain", "needanumbrella")):
            forecast_words = ("明天", "后天", "今晚", "下午", "周末", "未来", "tomorrow", "tonight", "weekend", "nextweek", "forecast")
            period = "forecast" if any(p in t for p in forecast_words) else "current"
            forecast_day = "day_after_tomorrow" if "后天" in t else "today" if "今天" in t else "weekend" if "周末" in t else "tonight" if "今晚" in t else "afternoon" if "下午" in t else "tomorrow"
            location = self._extract_location(raw)
            slots = {"period": period, "query": raw, "forecast_day": forecast_day}
            if location:
                slots["location"] = location
            return result(IntentName.WEATHER_QUERY, RequestDomain.INFORMATION, slots=slots, llm=False)

        # Generic capability questions are read-only. Concrete requests such as a
        # specified forecast or duration have already been recognized above.
        capability_question = any(p in t for p in ("能不能", "可以吗", "能吗", "会不会", "能够", "canyou", "areyouableto")) or ("你能" in t and ("吗" in t or "么" in t))
        explicit_request_marker = any(p in t for p in ("帮我", "帮忙", "替我", "给我", "查一下", "现在就", "please", "helpme"))
        if capability_question and not explicit_request_marker and self._extract_duration_s(raw) is None:
            if any(p in t for p in ("提醒", "remind")):
                return result(IntentName.CAPABILITY_QUERY, RequestDomain.CAPABILITY, slots={"capability_id": "reminder.create"})
            if any(p in t for p in ("计时", "倒计时", "timer", "countdown")):
                return result(IntentName.CAPABILITY_QUERY, RequestDomain.CAPABILITY, slots={"capability_id": "timer.create"})
            if any(p in t for p in ("音乐", "歌曲", "放歌", "music", "song", "media")):
                return result(IntentName.CAPABILITY_QUERY, RequestDomain.CAPABILITY, slots={"capability_id": "media.control"})
            if any(p in t for p in ("开灯", "关灯", "空调", "窗帘", "设备", "灯光", "turnonthelight", "turnoffthelight", "airconditioner")):
                return result(IntentName.CAPABILITY_QUERY, RequestDomain.CAPABILITY, slots={"capability_id": "device.control"})
            if any(p in t for p in ("搜索", "联网", "上网", "查资料", "searchonline", "internet")):
                return result(IntentName.CAPABILITY_QUERY, RequestDomain.CAPABILITY, slots={"capability_id": "knowledge.search"})

        # Generalize composite navigate-and-inspect requests across configured room
        # names. Do not key this behavior to only "卧室/有没有人" phrasing: e.g.
        # "去客厅看看有什么" is the same compound action.
        room_location = self._extract_room(raw)
        move_markers = ("去", "前往", "移动到", "走到", "到我", "goto", "gointo", "moveto", "moveinto")
        observe_markers = (
            "看看有没有", "看看有什么", "看看有啥", "看看里面", "看看周围", "检查有没有",
            "检查一下", "检查", "查看有没有", "查看有什么", "查看一下", "观察",
            "看看", "瞧瞧", "lookaround", "lookat", "inspect", "checkif", "seewhat",
            "seeifanyone", "whetheranyone", "isanyonethere"
        )
        media_viewing_words = ("电影", "电视", "视频", "节目", "比赛", "movie", "video", "show", "match")
        requests_inspection = any(p in t for p in observe_markers) or any(p in t for p in ("有什么", "有啥", "有没有人", "有没有东西"))
        requests_media_viewing = any(p in t for p in media_viewing_words)
        if room_location and any(p in t for p in move_markers) and requests_inspection and not requests_media_viewing:
            return result(IntentName.COMPOSITE_OBSERVE_LOCATION, RequestDomain.TASK,
                          slots={"location": room_location, "observation_query": raw},
                          explicit=True, compound=True)

        if any(p in t for p in ("不要打扰", "别打扰", "别烦我")):
            return result(IntentName.QUIET_ENABLE, RequestDomain.CONTROL, slots={"scope": "SESSION"}, conf=0.99, explicit=True)

        if any(p in t for p in ("来我这里", "过来我这", "到我身边", "来我身边", "过来一下", "来找我", "comehere", "cometome")):
            return result(IntentName.MOVE_TO_USER, RequestDomain.MOBILITY, explicit=True)

        if any(p in t for p in ("看一下这个东西", "看看这个东西", "识别这个物体", "识别一下这个", "帮我辨认这个", "这是什么物体", "whatsthisobject", "identifythisobject", "describethisobject")):
            return result(IntentName.OBSERVE_OBJECT, RequestDomain.PERCEPTION,
                          slots={"target": "referenced_object", "query": raw}, explicit=True)

        # A named room takes precedence over generic scene observation. A user who asks
        # to inspect the bedroom does not mean "look at the current scene".
        location = self._extract_room(raw)
        if location and any(p in t for p in ("去", "前往", "移动到", "走到", "goto", "moveto")):
            return result(IntentName.MOVE_TO_LOCATION, RequestDomain.MOBILITY,
                          slots={"location": location}, explicit=True)
        if location and any(p in t for p in (
            "看看", "观察", "有没有人", "有人吗", "有什么", "有啥", "有没有东西",
            "检查", "查看", "瞧瞧", "look", "observe", "check", "inspect"
        )):
            return result(IntentName.OBSERVE_LOCATION, RequestDomain.PERCEPTION,
                          slots={"location": location, "observation_query": raw}, explicit=True)

        # Scene observation must precede user observation: "看看我周围" is not "看我".
        scene_phrases = ("你现在看到什么", "你看到了什么", "你看到什么了", "你在看什么", "描述一下眼前",
                         "看看周围", "看一下周围", "看一眼周围", "看下周围", "看看附近", "看一下附近", "看看四周",
                         "看看房间", "看一下房间", "房间里有没有人", "这边有没有人",
                         "lookaround", "whatdoyousee", "whatareyouseeing", "whatareyoulookingat")
        if any(p in t for p in scene_phrases) or (
            any(p in t for p in ("看", "观察", "look", "check"))
            and any(p in t for p in ("周围", "附近", "四周", "眼前", "当前场景", "surroundings"))
        ):
            return result(IntentName.OBSERVE_SCENE, RequestDomain.PERCEPTION, slots={"target": "current_scene"}, explicit=True)
        if any(p in t for p in ("看看我", "看一下我", "观察我", "看看我在干什么", "识别我现在", "lookatme", "observeme", "whatamidoing")):
            return result(IntentName.OBSERVE_USER, RequestDomain.PERCEPTION, slots={"target": "user"}, explicit=True)

        if any(p in t for p in ("我有哪些提醒", "查看提醒", "列出提醒", "我的提醒", "还有什么提醒", "有哪些待办提醒", "list my reminders", "show reminders")):
            return result(IntentName.REMINDER_LIST, RequestDomain.REMINDER, explicit=True)
        if (any(p in t for p in ("取消", "删除", "撤销")) and "提醒" in t) or ("cancel reminder" in t.lower()):
            query = raw
            for prefix in ("帮我取消", "请取消", "取消", "删除", "撤销"):
                query = query.replace(prefix, "", 1)
            query = query.replace("提醒我", "").replace("提醒", "").strip(" ，,。！？!?：:")
            # A generic cancellation stays target-less; the provider will list candidates
            # rather than cancel multiple reminders by accident.
            return result(IntentName.REMINDER_CANCEL, RequestDomain.REMINDER,
                          slots={"query": query} if query else {}, explicit=True)

        timer_requested = any(p in t for p in ("计时", "计时器", "倒计时", "timer", "countdown"))
        if timer_requested:
            timer_duration = self._extract_duration_s(raw)
            slots = {"duration_s": timer_duration} if timer_duration is not None else {}
            return result(IntentName.TIMER_CREATE, RequestDomain.REMINDER, slots=slots, explicit=True)

        if any(p in t for p in ("播放音乐", "放首歌", "放一首歌", "暂停音乐", "停止音乐", "下一首", "上一首", "音量调大", "音量调小", "播放播客", "playmusic", "pausemusic", "stopmusic", "nexttrack", "previoustrack", "volumeup", "volumedown")):
            return result(IntentName.MEDIA_CONTROL, RequestDomain.MEDIA, slots={"command": raw}, explicit=True)

        if any(p in t for p in ("提醒我", "记得提醒", "到时候提醒", "设置提醒", "叫我", "通知我")):
            reminder_text, time_slot = self._extract_reminder_request(raw)
            return result(IntentName.REMINDER_CREATE, RequestDomain.REMINDER,
                          slots={"text": reminder_text, **({"time": time_slot} if time_slot else {})}, explicit=True)

        if any(p in t for p in (
            "开灯", "关灯", "把灯关掉", "把灯打开", "关掉灯", "打开灯", "关一下灯",
            "打开灯光", "关闭灯光", "把灯关了", "把灯打开一下", "调亮", "调暗",
            "打开空调", "关闭空调", "把温度调到", "打开窗帘", "关闭窗帘",
            "turnonthelight", "turnoffthelight", "setthethermostat", "openthecurtains", "closethecurtains"
        )):
            return result(IntentName.DEVICE_CONTROL, RequestDomain.DEVICE_CONTROL, slots={"command": raw}, explicit=True)

        if any(p in t for p in ("看一下", "看看") ) and any(p in t for p in ("我", "我这边", "前面")):
            return result(IntentName.OBSERVE_USER, RequestDomain.PERCEPTION, slots={"target": "user"}, explicit=True)

        if any(p in t for p in ("最新消息", "最近发生", "现在的", "现在的政策", "最新版本", "今天公布", "查一下", "搜索一下", "找资料", "现任", "目前", "最新", "截至", "新闻", "latest", "currentstatus", "whoisthecurrent", "asof2026")):
            return result(IntentName.CURRENT_INFO_QUERY, RequestDomain.INFORMATION, slots={"query": raw}, llm=True)

        if any(p in t for p in (
            "什么是", "是什么", "何为", "为什么", "怎么回事", "解释一下", "介绍一下", "原理", "定义", "百科", "是谁", "讲讲", "知识",
            "多高", "多大", "多长", "多远", "多少米", "多少人", "多少岁", "海拔", "高度", "身高", "哪一年", "哪一年", "什么时候",
            "哪个国家", "哪里来的", "什么意思", "代表什么", "历史", "区别", "有什么不同", "比较一下",
            "howtall", "howhigh", "howfar", "howmany", "whatis", "whois", "whenwas", "whereis", "define"
        )):
            return result(IntentName.KNOWLEDGE_QUERY, RequestDomain.INFORMATION, slots={"query": raw}, llm=True)

        if any(p in t for p in ("讲个笑话", "说个笑话", "聊聊天", "陪我聊", "闲聊", "聊一聊", "聊聊", "你好", "嗨", "早上好", "晚安", "谢谢", "再见", "我今天", "我觉得", "我有点", "letschat", "tellmeajoke", "hello", "hi", "goodmorning", "goodnight", "thankyou", "goodbye")):
            return result(IntentName.SMALL_TALK, RequestDomain.CONVERSATION, slots={"text": raw}, llm=True)

        if any(p in t for p in ("帮我", "请你", "替我", "给我安排", "执行一下", "完成这个")):
            return result(IntentName.GENERAL_TASK, RequestDomain.TASK, slots={"goal": raw}, llm=True, compound=True)

        # Unclassified ordinary natural language falls back to conversation, not an action.
        # An ambiguous imperative is flagged for clarification rather than guessed into motion.
        if re.match(r"^(去|到|拿|开|关|打开|关闭|把|移动|跟着|找一下|播放|拍|录制|删除|发送|启动|暂停|调|bring|go|move|take|fetch|come|open|close|look|check|turnon|turnoff|play|pause|delete|send|start)", t):
            return result(IntentName.UNKNOWN, RequestDomain.UNKNOWN, slots={"text": raw}, conf=0.35)
        return result(IntentName.SMALL_TALK, RequestDomain.CONVERSATION, slots={"text": raw}, conf=0.65, llm=True)

    @staticmethod
    def _extract_room(text: str) -> str | None:
        candidates = ("卧室", "客厅", "厨房", "书房", "卫生间", "浴室", "bedroom", "living room", "kitchen", "study", "bathroom")
        return next((room for room in candidates if room in text.lower()), None)

    @staticmethod
    def _extract_location(text: str) -> str | None:
        known = re.search(r"(?:在|查|看看|查询)?\s*(北京|上海|广州|深圳|杭州|成都|南京|东京|大阪|新加坡|台北|香港|伦敦|纽约)(?:市|地区)?", text)
        if known:
            return known.group(1)
        # Conservative free-form extraction for phrases such as “查一下西雅图的天气”
        # and “weather forecast for Boston”; do not guess if the phrase is ambiguous.
        zh = re.search(r"(?:查询|查一下|查查|在)\s*([\u4e00-\u9fffA-Za-z0-9·—-]{2,24}?)\s*(?:的)?(?:天气|温度|降雨|预报)", text)
        if zh:
            candidate = zh.group(1).strip()
            if candidate not in {"今天", "明天", "后天", "现在", "当地", "这里", "我们这里"}:
                return candidate
        en = re.search(r"(?:weather|forecast|temperature)(?:\s+(?:in|for|at))\s+([A-Za-z][A-Za-z .'-]{1,35})", text, re.IGNORECASE)
        if en:
            candidate = en.group(1).strip().rstrip(".,")
            candidate = re.split(r"\b(?:today|tomorrow|tonight|this\s+weekend|next\s+week)\b", candidate, maxsplit=1, flags=re.IGNORECASE)[0].strip()
            return candidate or None
        return None

    @staticmethod
    def _extract_time_phrase(text: str) -> str | None:
        relative = re.search(r"(?:再过|过|在)?(?:\d{1,7}|[零〇一二两三四五六七八九十百半]+)\s*(?:秒钟|秒|分钟|分|小时|天|日|时)\s*(?:后|之后|以后)", text)
        if relative:
            return re.sub(r"\s+", "", relative.group(0))
        absolute = re.search(
            r"(?:(?:今天|明天|后天|今晚|明早)\s*)?"
            r"(?:(?:凌晨|清晨|早上|上午|中午|下午|傍晚|晚上)\s*)?"
            r"(?:\d{1,2}:\d{2}(?::\d{2})?|[零〇一二两三四五六七八九十]{1,3}点(?:半|一刻|三刻|\d{1,2}分)?)",
            text,
        )
        return re.sub(r"\s+", "", absolute.group(0)) if absolute else None

    @classmethod
    def _extract_reminder_request(cls, text: str) -> tuple[str, str | None]:
        """Extract reminder text/time from both ``in 1 minute remind me X`` and
        ``remind me X in 1 minute`` word orders. Parsing the due time happens in the
        provider; this method only preserves the phrase without guessing a timestamp.
        """
        raw = text.strip()
        compact = re.sub(r"\s+", "", raw)
        unit = r"(?:秒钟|秒|分钟|分|小时|天|日|时)"
        number = r"(?:\d{1,7}|[零〇一二两三四五六七八九十百半]+)"
        before = re.match(rf"^(?P<time>(?:再过|过|在)?{number}{unit}(?:后|之后|以后))[，,。！？!? ]*(?:提醒我|提醒一下我|叫我|通知我)(?P<task>.+)$", compact)
        if before:
            task = before.group("task").strip("，,。！？!? ")
            return (task or raw, before.group("time"))

        # Remove a single reminder trigger, then separate a time phrase wherever it
        # occurs in the remaining text. This supports ``提醒我吃饭，一分钟后`` too.
        body = re.sub(r"^(?:记得提醒我|到时候提醒我|设置提醒|提醒一下我|提醒我|叫我|通知我)", "", compact, count=1)
        time_phrase = cls._extract_time_phrase(body)
        if time_phrase:
            # For relative expressions, include the complete unit+postfix; for absolute
            # expressions, this removes only the date/time fragment, not task text.
            index = body.find(time_phrase)
            if index >= 0:
                body = body[:index] + body[index + len(time_phrase):]
            body = body.strip("，,。！？!?：: ")
        task = body.strip("，,。！？!?：: ")
        return task or raw, time_phrase

    @staticmethod
    def _extract_duration_s(text: str) -> int | None:
        compact = re.sub(r"\s+", "", text.lower())
        match = re.search(r"(\d{1,5})(秒|分钟|分|小时|时|seconds?|minutes?|hours?)", compact)
        if match:
            amount = int(match.group(1))
            unit = match.group(2)
            multiplier = 3600 if unit in {"小时", "时", "hour", "hours"} else 60 if unit in {"分钟", "分", "minute", "minutes"} else 1
            if amount <= 0 or amount > 86400 // multiplier:
                return None
            return amount * multiplier
        chinese_nums = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        match = re.search(r"([一二两三四五六七八九十]+)(秒|分钟|分|小时|时)", compact)
        if not match:
            return None
        token, unit = match.groups()
        if token == "十":
            amount = 10
        elif token.startswith("十"):
            amount = 10 + chinese_nums.get(token[-1], 0)
        elif token.endswith("十"):
            amount = chinese_nums.get(token[0], 1) * 10
        elif "十" in token:
            left, right = token.split("十", 1)
            amount = chinese_nums.get(left, 1) * 10 + chinese_nums.get(right, 0)
        else:
            amount = chinese_nums.get(token, 0)
        multiplier = 3600 if unit in {"小时", "时"} else 60 if unit in {"分钟", "分"} else 1
        if amount <= 0 or amount > 86400 // multiplier:
            return None
        return amount * multiplier


class RequestRouter:
    """Classify natural-language requests and bind them to explicit capabilities."""
    _MAP: dict[IntentName, tuple[str, ...]] = {
        IntentName.TIME_QUERY: ("clock.now",),
        IntentName.WEATHER_QUERY: (),  # selected dynamically by period
        IntentName.KNOWLEDGE_QUERY: ("knowledge.answer",),
        IntentName.CURRENT_INFO_QUERY: ("knowledge.search",),
        IntentName.OBSERVE_USER: ("vision.observe_user",),
        IntentName.OBSERVE_OBJECT: ("vision.observe_object",),
        IntentName.OBSERVE_SCENE: ("vision.observe_scene",),
        IntentName.OBSERVE_LOCATION: ("vision.observe_location",),
        IntentName.MOVE_TO_USER: ("mobility.navigate_to_user",),
        IntentName.MOVE_TO_LOCATION: ("mobility.navigate_to_location",),
        IntentName.COMPOSITE_OBSERVE_LOCATION: ("mobility.navigate_to_location", "vision.observe_location"),
        IntentName.REMINDER_CREATE: ("reminder.create",),
        IntentName.REMINDER_LIST: ("reminder.list",),
        IntentName.REMINDER_CANCEL: ("reminder.cancel",),
        IntentName.DEVICE_CONTROL: ("device.control",),
        IntentName.MEDIA_CONTROL: ("media.control",),
        IntentName.TIMER_CREATE: ("timer.create",),
        IntentName.GENERAL_TASK: ("task.plan",),
    }

    def __init__(self, registry: CapabilityRegistry, classifier: IntentClassifier | None = None,
                 *, clarification_max_rounds: int = 2, clock: Callable[[], float] = time.time,
                 default_context: RequestContext | None = None, max_request_chars: int = 10_000):
        if not 0 <= clarification_max_rounds <= 5:
            raise ValueError("clarification_max_rounds must be in [0,5]")
        if not isinstance(max_request_chars, int) or isinstance(max_request_chars, bool) or max_request_chars < 64:
            raise ValueError("max_request_chars must be an integer >= 64")
        self.registry = registry
        self.classifier = classifier or RuleBasedIntentClassifier()
        self.clarification_max_rounds = clarification_max_rounds
        self._clock = clock
        self.default_context = default_context or RequestContext()
        self.max_request_chars = max_request_chars
        self._pending: dict[str, tuple[ClassifiedIntent, int, tuple[str, ...]]] = {}

    def route(self, text: str, context: RequestContext | None = None, *, request_id: str | None = None) -> RequestDecision:
        ctx = context or self.default_context
        req_id = request_id or uuid.uuid4().hex
        if not isinstance(text, str) or not text.strip():
            intent = ClassifiedIntent(IntentName.UNKNOWN, RequestDomain.UNKNOWN, str(text or ""), "", confidence=0.0)
            return RequestDecision(req_id, ctx.conversation_id, intent, RouteStatus.INVALID,
                                   response_text="请求内容为空，无法判断需要执行什么。", reason_code="EMPTY_REQUEST")
        if len(text) > self.max_request_chars:
            intent = ClassifiedIntent(IntentName.UNKNOWN, RequestDomain.UNKNOWN, text[:256], "", confidence=0.0)
            return RequestDecision(req_id, ctx.conversation_id, intent, RouteStatus.INVALID,
                                   response_text="请求内容过长，未执行。", reason_code="REQUEST_TOO_LARGE")
        intent = self.classifier.classify(text)
        return self._route_with_intent(intent, ctx, req_id)

    def _route_with_intent(self, intent: ClassifiedIntent, ctx: RequestContext, req_id: str) -> RequestDecision:
        if intent.name in {IntentName.QUIET_ENABLE, IntentName.QUIET_DISABLE, IntentName.STOP, IntentName.CANCEL}:
            kind, payload = self._control_event(intent)
            return RequestDecision(req_id, ctx.conversation_id, intent, RouteStatus.CONTROL_ACTION,
                                   control_event_kind=kind, control_payload=payload,
                                   response_text=self._control_ack(intent), reason_code="DIRECT_CONTROL")

        if intent.name in {IntentName.CAPABILITY_QUERY, IntentName.CAPABILITY_LIST_QUERY}:
            message = self._capability_answer(intent, ctx)
            return RequestDecision(req_id, ctx.conversation_id, intent, RouteStatus.CAPABILITY_QUERY,
                                   response_text=message, reason_code="CAPABILITY_STATUS_QUERY")

        if intent.name == IntentName.UNKNOWN:
            question = intent.clarification_question or "你希望我具体做什么？"
            return RequestDecision(req_id, ctx.conversation_id, intent, RouteStatus.NEEDS_CLARIFICATION,
                                   missing_slots=("goal_or_object",), question=question,
                                   response_text=question, reason_code="AMBIGUOUS_ACTION")

        capability_ids = self._capabilities_for(intent, ctx)
        if intent.name == IntentName.SMALL_TALK:
            if self.registry.is_available("conversation.respond"):
                return self._decision(req_id, ctx, intent, RouteStatus.DELEGATE_LLM, capability_ids=("conversation.respond",), reason="CONVERSATION_REQUEST")
            return RequestDecision(req_id, ctx.conversation_id, intent, RouteStatus.UNSUPPORTED,
                                   missing_capabilities=("conversation.respond",),
                                   response_text="当前对话模型不可用，但本地控制仍然可以工作。", reason_code="LLM_UNAVAILABLE")

        # Report missing integrations before asking the user for parameters. Do not collect
        # location/time details for a capability the installed product cannot execute.
        early_missing = tuple(cid for cid in capability_ids if not self.registry.is_available(cid))
        if early_missing:
            return RequestDecision(req_id, ctx.conversation_id, intent, RouteStatus.UNSUPPORTED,
                                   capability_ids=capability_ids, missing_capabilities=early_missing,
                                   response_text=self._unsupported_message(intent, early_missing),
                                   reason_code="CAPABILITY_NOT_AVAILABLE")

        if intent.name == IntentName.WEATHER_QUERY:
            location = intent.slots.get("location") or ctx.default_location
            intent = self._with_slots(intent, {"location": location} if location else {})
            cap = capability_ids[0]
            if not location:
                return self._clarify(req_id, ctx, intent, ("location",), "你想查询哪个城市的天气？")

        if intent.name == IntentName.REMINDER_CREATE and not intent.slots.get("time"):
            return self._clarify(req_id, ctx, intent, ("time",), "你希望我什么时候提醒你？")
        if intent.name == IntentName.OBSERVE_LOCATION and not intent.slots.get("location"):
            return self._clarify(req_id, ctx, intent, ("location",), "你想让我观察哪个房间或位置？")
        if intent.name == IntentName.MOVE_TO_LOCATION and not intent.slots.get("location"):
            return self._clarify(req_id, ctx, intent, ("location",), "你希望我移动到哪个已配置的位置？")
        if intent.name == IntentName.COMPOSITE_OBSERVE_LOCATION and not intent.slots.get("location"):
            return self._clarify(req_id, ctx, intent, ("location",), "你希望我去哪个已配置的房间查看？")

        # Hard constraints are checked before any provider is invoked.
        if ctx.safety_stopped and any((self.registry.get(cid) and self.registry.get(cid).risk_level in {RiskLevel.HIGH, RiskLevel.CRITICAL}) for cid in capability_ids):
            return RequestDecision(req_id, ctx.conversation_id, intent, RouteStatus.BLOCKED,
                                   missing_capabilities=capability_ids, response_text="安全停止状态仍然有效，我不会执行这项动作。",
                                   reason_code="SAFETY_STOP_ACTIVE")

        # Enforce provider-declared slots so future capabilities cannot accidentally
        # execute with incomplete arguments just because the classifier didn't know them.
        calls = self._calls(intent, capability_ids)
        missing_from_specs: list[str] = []
        for call in calls:
            spec = self.registry.get(call.capability_id)
            if spec is not None:
                for slot in spec.required_slots:
                    if not call.arguments.get(slot):
                        missing_from_specs.append(slot)
        if missing_from_specs:
            unique_missing = tuple(dict.fromkeys(missing_from_specs))
            return self._clarify(req_id, ctx, intent, unique_missing, self._question_for(unique_missing[0]))

        # High-risk capabilities may require a confirmation when intent understanding is uncertain.
        confirmation_caps = []
        for cid in capability_ids:
            spec = self.registry.get(cid)
            if spec is None:
                continue
            llm_inferred_sensitive = intent.source == "LLM" and (
                spec.side_effect or spec.risk_level in {RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL}
                or intent.name == IntentName.GENERAL_TASK
            )
            below_configured_confidence = (
                spec.confirm_below_confidence is not None
                and intent.confidence < spec.confirm_below_confidence
            )
            if (llm_inferred_sensitive or below_configured_confidence) and req_id not in ctx.confirmed_request_ids:
                confirmation_caps.append(cid)
        if confirmation_caps:
            return RequestDecision(req_id, ctx.conversation_id, intent, RouteStatus.NEEDS_CONFIRMATION,
                                   capability_ids=capability_ids,
                                   response_text="我对这项操作的理解还不够确定，请确认是否执行。",
                                   requires_confirmation=True, reason_code="LLM_INFERRED_OR_LOW_CONFIDENCE_ACTION")

        # Location / visual freshness preconditions are distinct from tool availability.
        if intent.name == IntentName.MOVE_TO_USER and not ctx.user_location_known:
            return RequestDecision(req_id, ctx.conversation_id, intent, RouteStatus.BLOCKED,
                                   capability_ids=capability_ids,
                                   response_text="我还没有可靠定位到你；请先启用定位并确认用户位置，我不会猜测坐标或启动移动。",
                                   reason_code="USER_LOCATION_UNKNOWN")
        if intent.name in {IntentName.MOVE_TO_LOCATION, IntentName.COMPOSITE_OBSERVE_LOCATION, IntentName.OBSERVE_LOCATION}:
            location = str(intent.slots.get("location", ""))
            if not ctx.allowed_locations:
                return RequestDecision(req_id, ctx.conversation_id, intent, RouteStatus.BLOCKED,
                                       capability_ids=capability_ids, response_text="设备尚未配置经过验证的位置清单，我不会猜测位置或启动移动/定点观察。",
                                       reason_code="LOCATION_MAP_NOT_CONFIGURED")
            if location not in ctx.allowed_locations:
                return RequestDecision(req_id, ctx.conversation_id, intent, RouteStatus.BLOCKED,
                                       capability_ids=capability_ids, response_text=f"位置“{location}”不在已配置的位置清单中。",
                                       reason_code="LOCATION_NOT_ALLOWLISTED")
            if intent.name in {IntentName.COMPOSITE_OBSERVE_LOCATION, IntentName.OBSERVE_LOCATION} and self._is_protected_location(location, ctx):
                if req_id not in ctx.confirmed_request_ids:
                    verb = "进入并观察" if intent.name == IntentName.COMPOSITE_OBSERVE_LOCATION else "观察"
                    return RequestDecision(req_id, ctx.conversation_id, intent, RouteStatus.NEEDS_CONFIRMATION,
                                           capability_ids=capability_ids,
                                           response_text=f"该请求需要确认：是否允许我{verb}“{location}”？",
                                           requires_confirmation=True, reason_code="PROTECTED_LOCATION_OBSERVATION")

        if intent.name == IntentName.KNOWLEDGE_QUERY:
            if capability_ids == ("knowledge.search",):
                calls = self._calls(intent, capability_ids)
                return RequestDecision(req_id, ctx.conversation_id, intent, RouteStatus.READY,
                                       calls=calls, capability_ids=capability_ids, reason_code="KNOWLEDGE_RETRIEVAL")
            spec = self.registry.get("knowledge.answer")
            if spec is not None and spec.execution_mode == "LLM":
                return self._decision(req_id, ctx, intent, RouteStatus.DELEGATE_LLM, capability_ids, "KNOWLEDGE_REQUEST")
        if intent.name == IntentName.CURRENT_INFO_QUERY:
            spec = self.registry.get("knowledge.search")
            if spec is not None and spec.execution_mode == "PROVIDER":
                return self._decision(req_id, ctx, intent, RouteStatus.READY, capability_ids, "CURRENT_INFO_RETRIEVAL")
        if intent.name == IntentName.GENERAL_TASK:
            return self._decision(req_id, ctx, intent, RouteStatus.DELEGATE_TASK_PLANNER, capability_ids, "GENERAL_TASK_REQUEST")

        calls = self._calls(intent, capability_ids)
        return RequestDecision(req_id, ctx.conversation_id, intent, RouteStatus.READY,
                               calls=calls, capability_ids=capability_ids, reason_code="CAPABILITIES_READY")

    def route_understanding(
        self, text: str, demand: Mapping[str, Any], context: RequestContext | None = None,
        *, request_id: str | None = None,
    ) -> RequestDecision:
        """Apply engine-owned policy to a structured demand result.

        The demand schema contains no route/capability field. This method maps only the
        reviewed broad request categories to internal intents, then reuses the router's
        capability, safety, confirmation, and availability checks.
        """
        from ..llm.llm_gateway import LLMGateway
        validation_error = LLMGateway._validate_payload("demand_understanding", demand)
        if validation_error is not None:
            raise ValueError(f"invalid demand-understanding result: {validation_error}")
        ctx = context or self.default_context
        req_id = request_id or uuid.uuid4().hex
        request_type = demand["request_type"]
        text_value = text if isinstance(text, str) else str(text or "")
        slots: dict[str, Any] = {}
        slot_allowlist = {"location", "period", "query", "target", "time", "text", "command", "goal", "scope", "duration_s", "label", "object", "device", "action", "units", "repeat", "condition"}
        for entity in demand["entities"]:
            key = entity["type"].strip().lower()
            if key in slot_allowlist and key not in slots and entity["value"]:
                slots[key] = entity["value"]
        semantic_intent = demand["intent"].strip()
        if request_type in {"emotional_sharing", "casual_chat"}:
            name, domain = IntentName.SMALL_TALK, RequestDomain.CONVERSATION
            slots.setdefault("text", text_value[:12000])
        elif request_type == "information_request":
            name, domain = IntentName.KNOWLEDGE_QUERY, RequestDomain.INFORMATION
            slots.setdefault("query", text_value[:12000])
        elif request_type == "task_request":
            name, domain = IntentName.GENERAL_TASK, RequestDomain.TASK
            slots.setdefault("goal", semantic_intent[:1000])
        elif request_type == "device_control":
            name, domain = IntentName.DEVICE_CONTROL, RequestDomain.DEVICE_CONTROL
            slots.setdefault("command", semantic_intent[:1000])
        else:
            # Memory changes require a separate reviewed memory workflow. `other` and
            # `unclear` never become executable actions merely because the model returned them.
            name, domain = IntentName.UNKNOWN, RequestDomain.UNKNOWN
        intent = ClassifiedIntent(
            name=name, domain=domain, raw_text=text_value[:self.max_request_chars],
            normalized_text=re.sub(r"\s+", "", text_value.lower())[:self.max_request_chars],
            slots=slots, confidence=0.75, explicit_command=False,
            requires_llm=name in {IntentName.SMALL_TALK, IntentName.KNOWLEDGE_QUERY, IntentName.GENERAL_TASK},
            compound=name == IntentName.GENERAL_TASK, source="LLM",
        )
        if demand["clarification_needed"]:
            question = demand["response_candidate"] or "我还需要一点信息，才能准确理解你的意思。"
            return RequestDecision(req_id, ctx.conversation_id, intent, RouteStatus.NEEDS_CLARIFICATION,
                                   missing_slots=("clarification",), question=question,
                                   response_text=question, reason_code="DEMAND_REQUIRES_CLARIFICATION")
        if name == IntentName.SMALL_TALK and demand["response_candidate"]:
            # A response candidate is still only a proposal. This engine policy accepts
            # it for low-risk conversation; action and information routes never use it.
            return RequestDecision(req_id, ctx.conversation_id, intent, RouteStatus.READY,
                                   response_text=demand["response_candidate"], reason_code="ENGINE_ACCEPTED_RESPONSE_CANDIDATE")
        return self._route_with_intent(intent, ctx, req_id)

    def route_confirmed(self, previous: RequestDecision, context: RequestContext, confirmed: bool) -> RequestDecision:
        """Resolve a confirmation gate. No action is executed by this method."""
        if previous.status != RouteStatus.NEEDS_CONFIRMATION:
            return RequestDecision(previous.request_id, previous.conversation_id, previous.intent,
                                   RouteStatus.INVALID, reason_code="NOT_A_CONFIRMATION_REQUEST")
        if not confirmed:
            return RequestDecision(previous.request_id, previous.conversation_id, previous.intent,
                                   RouteStatus.BLOCKED, response_text="好的，我不会执行这项动作。", reason_code="USER_DECLINED")
        confirmed_context = RequestContext(
            conversation_id=context.conversation_id, timezone_name=context.timezone_name,
            default_location=context.default_location, now_wall_s=context.now_wall_s,
            user_speaking=context.user_speaking, quiet_scope=context.quiet_scope,
            safety_stopped=context.safety_stopped, user_location_known=context.user_location_known,
            allowed_locations=context.allowed_locations, protected_locations=context.protected_locations,
            world_facts=context.world_facts, confirmed_request_ids=context.confirmed_request_ids | {previous.request_id},
            conversation_context=context.conversation_context,
        )
        return self._route_with_intent(previous.intent, confirmed_context, previous.request_id)

    def answer_clarification(self, previous: RequestDecision, answer: str, context: RequestContext) -> RequestDecision:
        """Resolve one pending slot; at most configured number of rounds per request."""
        pending = self._pending.get(previous.request_id)
        if previous.status != RouteStatus.NEEDS_CLARIFICATION or pending is None:
            return RequestDecision(previous.request_id, previous.conversation_id, previous.intent,
                                   RouteStatus.INVALID, reason_code="NO_PENDING_CLARIFICATION")
        intent, rounds, missing = pending
        rounds += 1
        if "goal_or_object" in missing:
            # The clarification supplies the missing request itself; classify that new utterance.
            self._pending.pop(previous.request_id, None)
            return self.route(answer, context, request_id=previous.request_id)
        slots = dict(intent.slots)
        self._apply_slot_answer(slots, missing, answer)
        new_intent = self._with_slots(intent, slots)
        remaining = tuple(slot for slot in missing if not slots.get(slot))
        if remaining and rounds >= self.clarification_max_rounds:
            self._pending.pop(previous.request_id, None)
            return RequestDecision(previous.request_id, previous.conversation_id, new_intent, RouteStatus.NEEDS_CLARIFICATION,
                                   missing_slots=remaining, response_text="我还无法可靠确认所需信息，因此不会猜测或执行。你可以重新提出更明确的请求。",
                                   reason_code="CLARIFICATION_LIMIT_REACHED")
        if remaining:
            self._pending[previous.request_id] = (new_intent, rounds, remaining)
            question = self._question_for(remaining[0])
            return RequestDecision(previous.request_id, previous.conversation_id, new_intent, RouteStatus.NEEDS_CLARIFICATION,
                                   missing_slots=remaining, question=question, reason_code="CLARIFICATION_CONTINUES")
        self._pending.pop(previous.request_id, None)
        return self._route_classified(new_intent, context, previous.request_id)

    def _route_classified(self, intent: ClassifiedIntent, ctx: RequestContext, req_id: str) -> RequestDecision:
        # Route a completed clarification without mutating shared classifier state.
        return self._route_with_intent(intent, ctx, req_id)

    def _clarify(self, req_id: str, ctx: RequestContext, intent: ClassifiedIntent,
                 missing: tuple[str, ...], question: str) -> RequestDecision:
        if self.clarification_max_rounds == 0:
            return RequestDecision(req_id, ctx.conversation_id, intent, RouteStatus.NEEDS_CLARIFICATION,
                                   missing_slots=missing, response_text="缺少必要信息，当前不会执行。", reason_code="CLARIFICATION_DISABLED")
        self._pending[req_id] = (intent, 0, missing)
        return RequestDecision(req_id, ctx.conversation_id, intent, RouteStatus.NEEDS_CLARIFICATION,
                               missing_slots=missing, question=question, reason_code="MISSING_REQUIRED_SLOT")

    def _capabilities_for(self, intent: ClassifiedIntent, ctx: RequestContext) -> tuple[str, ...]:
        if intent.name == IntentName.WEATHER_QUERY:
            period = str(intent.slots.get("period", "current"))
            return ("weather.forecast" if period == "forecast" else "weather.current",)
        if intent.name == IntentName.KNOWLEDGE_QUERY and self.registry.is_available("knowledge.search"):
            # When a retrieval provider is wired, search-backed facts take precedence
            # over unsupported model-only knowledge claims. The host may summarize
            # the returned cited material with an LLM afterwards.
            return ("knowledge.search",)
        return self._MAP.get(intent.name, ())

    def _calls(self, intent: ClassifiedIntent, capability_ids: Sequence[str]) -> tuple[CapabilityCall, ...]:
        slots = dict(intent.slots)
        calls: list[CapabilityCall] = []
        for idx, capability_id in enumerate(capability_ids, 1):
            if capability_id == "clock.now":
                args: dict[str, Any] = {}
            elif capability_id.startswith("weather."):
                args = {"location": slots.get("location"), "period": slots.get("period", "current"),
                        "forecast_day": slots.get("forecast_day", "tomorrow"),
                        "query": slots.get("query", intent.raw_text)}
            elif capability_id == "vision.observe_user":
                args = {"target": "user"}
                if slots.get("query"):
                    args["query"] = slots["query"]
            elif capability_id == "vision.observe_scene":
                args = {"target": "current_scene"}
            elif capability_id == "vision.observe_object":
                args = {"target": slots.get("target", "referenced_object"), "query": slots.get("query", intent.raw_text)}
            elif capability_id in {"vision.observe_location", "mobility.navigate_to_location"}:
                args = {"location": slots.get("location")}
                if capability_id == "vision.observe_location":
                    query = slots.get("observation_query", slots.get("query"))
                    if query:
                        args["query"] = query
            elif capability_id == "mobility.navigate_to_user":
                args = {"target": "current_user_location"}
            elif capability_id == "reminder.create":
                args = {"text": slots.get("text", intent.raw_text), "time": slots.get("time")}
            elif capability_id == "reminder.list":
                args = {}
            elif capability_id == "reminder.cancel":
                args = {"query": slots.get("query"), "reminder_id": slots.get("reminder_id")}
            elif capability_id == "timer.create":
                args = {"duration_s": slots.get("duration_s"), "label": slots.get("label", intent.raw_text)}
            elif capability_id in {"device.control", "media.control"}:
                args = {"command": slots.get("command", intent.raw_text)}
            elif capability_id in {"knowledge.answer", "knowledge.search"}:
                args = {"query": slots.get("query", intent.raw_text)}
            elif capability_id == "task.plan":
                args = {"goal": slots.get("goal", intent.raw_text)}
            else:
                args = slots
            calls.append(CapabilityCall(capability_id, args, f"step-{idx}"))
        return tuple(calls)

    def _capability_answer(self, intent: ClassifiedIntent, ctx: RequestContext) -> str:
        if intent.name == IntentName.CAPABILITY_LIST_QUERY:
            names = [row["description"] for row in self.registry.describe() if row["available"]]
            extras = ["停止、取消与安静模式"]
            if self.registry.is_available("conversation.respond"):
                extras.append("日常闲聊")
            if self.registry.is_available("knowledge.answer"):
                extras.append("百科知识解释")
            return "当前已接入的能力包括：" + "、".join(names + extras) + "。具体能力以设备当前状态为准。"
        target = str(intent.slots.get("capability_id", ""))
        spec = self.registry.get(target)
        available = self.registry.is_available(target)
        if target == "vision.observe_user":
            camera_active = ctx.world_facts.get("camera.active") is True
            camera_known_off = ctx.world_facts.get("camera.active") is False
            if intent.slots.get("question_kind") == "camera_status":
                if not available:
                    return "当前没有接入可用的视觉输入。"
                if camera_known_off:
                    return "视觉功能已接入，但目前的状态信息显示摄像头未开启。"
                if camera_active:
                    return "状态信息显示摄像头已开启；这不代表画面中一定有人或识别结果一定正确。"
                return "视觉功能已接入，但我无法确认摄像头当前是否开启。"
            age = ctx.world_facts.get("vision.last_frame_age_s")
            fresh = isinstance(age, (int, float)) and not isinstance(age, bool) and 0 <= float(age) <= 3.0
            if not available:
                return "当前没有接入可用的视觉输入，所以我现在不能检查是否看见你。"
            if not camera_active or not fresh:
                return "视觉能力已接入，但目前没有可确认的新画面；请确认摄像头已开启并重新尝试。"
            return "视觉输入当前可用，而且最近收到了一帧新画面；我可以尝试确认画面中的人，但这不保证身份判断一定正确，也不能仅凭画面准确判断情绪。"
        if target == "mobility.navigate_to_user":
            return "当前已接入移动能力。" if available else "当前版本没有接入可用的移动底盘控制，因此我不能实际移动到你身边。"
        if target == "audio.input":
            if not available:
                return "当前没有接入可用的麦克风输入。"
            audio_active = ctx.world_facts.get("audio.active") is True
            age = ctx.world_facts.get("audio.last_input_age_s")
            fresh = isinstance(age, (int, float)) and not isinstance(age, bool) and 0 <= float(age) <= 3.0
            if not audio_active or not fresh:
                return "音频输入能力已接入，但我当前没有可确认的新音频输入，不能声称正在听到你。"
            return "音频输入当前可用，而且最近收到了新音频数据；这不代表我一定正确识别了所有语音内容。"
        if target == "audio.output":
            if not available:
                return "当前没有接入可用的语音输出设备或 TTS。"
            if ctx.world_facts.get("audio.output_ready") is not True:
                return "语音输出能力已接入，但当前无法确认扬声器/TTS 已就绪。"
            return "语音输出当前报告为就绪。"
        return (f"{spec.description}：当前可用。" if spec and available else
                f"{spec.description if spec else target}：当前未接入或已禁用。")

    @staticmethod
    def _control_event(intent: ClassifiedIntent) -> tuple[str, dict[str, Any]]:
        if intent.name == IntentName.QUIET_ENABLE:
            return EventKind.USER_QUIET.value, {"enabled": True, "scope": intent.slots.get("scope", "SESSION")}
        if intent.name == IntentName.QUIET_DISABLE:
            return EventKind.USER_QUIET.value, {"enabled": False}
        if intent.name == IntentName.STOP:
            return EventKind.USER_STOP.value, {"reason": "user_request"}
        return EventKind.USER_CANCEL.value, {"reason": "user_request"}

    @staticmethod
    def _control_ack(intent: ClassifiedIntent) -> str:
        return {
            IntentName.QUIET_ENABLE: "好的，我会安静下来，不再主动打扰你。",
            IntentName.QUIET_DISABLE: "好的，已解除安静模式。",
            IntentName.STOP: "好的，我会停止当前可取消的动作。",
            IntentName.CANCEL: "好的，我会取消当前任务。",
        }.get(intent.name, "好的。")

    @staticmethod
    def _unsupported_message(intent: ClassifiedIntent, missing: Sequence[str]) -> str:
        if intent.name in {IntentName.MOVE_TO_USER, IntentName.MOVE_TO_LOCATION, IntentName.COMPOSITE_OBSERVE_LOCATION}:
            return "当前设备缺少完成这项移动/观察任务所需的能力：" + "、".join(missing) + "。我不会假装已经执行。"
        if intent.name in {IntentName.WEATHER_QUERY, IntentName.CURRENT_INFO_QUERY}:
            return "当前没有可用的实时数据或检索服务，因此我无法可靠确认这项信息。"
        if intent.name == IntentName.MEDIA_CONTROL:
            return "当前没有接入获授权的媒体控制服务，因此我没有执行播放或音量操作。"
        if intent.name == IntentName.TIMER_CREATE:
            return "当前没有接入计时器服务，因此没有创建计时任务。"
        if intent.name in {IntentName.REMINDER_CREATE, IntentName.REMINDER_LIST, IntentName.REMINDER_CANCEL}:
            return "当前没有运行可用的本地提醒服务，因此没有创建、查看或取消提醒。"
        if intent.name in {IntentName.OBSERVE_USER, IntentName.OBSERVE_OBJECT, IntentName.OBSERVE_SCENE, IntentName.OBSERVE_LOCATION}:
            return "当前视觉观察能力没有接入或不可用，我不能声称已经看到了现场。"
        return "当前缺少完成这项请求所需的能力：" + "、".join(missing) + "。"

    @staticmethod
    def _decision(req_id: str, ctx: RequestContext, intent: ClassifiedIntent, status: RouteStatus,
                  capability_ids: Sequence[str], reason: str) -> RequestDecision:
        calls = tuple(CapabilityCall(cid, {"query": intent.slots.get("query", intent.raw_text)}, f"step-{i}")
                      for i, cid in enumerate(capability_ids, 1))
        return RequestDecision(req_id, ctx.conversation_id, intent, status, calls=calls,
                               capability_ids=tuple(capability_ids), reason_code=reason)

    @staticmethod
    def _with_slots(intent: ClassifiedIntent, updates: Mapping[str, Any]) -> ClassifiedIntent:
        merged = dict(intent.slots)
        merged.update(updates)
        return ClassifiedIntent(intent.name, intent.domain, intent.raw_text, intent.normalized_text,
                                merged, intent.confidence, intent.explicit_command, intent.requires_llm, intent.compound, intent.source)

    @staticmethod
    def _is_protected_location(location: str, context: RequestContext) -> bool:
        values = {str(x).casefold() for x in context.protected_locations}
        return location.casefold() in values

    @staticmethod
    def _apply_slot_answer(slots: dict[str, Any], missing: Sequence[str], answer: str) -> None:
        value = answer.strip()
        if not value:
            return
        for slot in missing:
            if slot == "location":
                slots[slot] = value
            elif slot == "time":
                slots[slot] = value
            elif slot == "duration_s":
                duration = RuleBasedIntentClassifier._extract_duration_s(value)
                if duration is not None:
                    slots[slot] = duration
            elif slot == "goal_or_object":
                slots["goal"] = value

    @staticmethod
    def _question_for(slot: str) -> str:
        return {"location": "请告诉我城市或具体位置。", "time": "请告诉我具体时间。",
                "duration_s": "你希望计时多久？例如 10 分钟。",
                "goal_or_object": "请再说明一下你希望我做什么。"}.get(slot, "请补充必要信息。")


class RequestDispatcher:
    """Execute routed calls; high-risk capabilities fail closed without a live guard.

    `pre_dispatch_guard` runs immediately before *each* provider call, including every
    step in a compound request. It returns `None` to allow or a reason string to deny.
    Production mobility, protected-scene and other high-risk providers must supply a
    guard that checks current safety/consent state rather than trusting an old route-time
    snapshot. The generic dispatcher does not pretend that RequestContext is live state.
    """
    def __init__(
        self,
        registry: CapabilityRegistry,
        *,
        control_event_sink: Callable[[str, Mapping[str, Any]], bool] | None = None,
        pre_dispatch_guard: Callable[[CapabilityCall, ClassifiedIntent, RequestContext], str | None] | None = None,
    ):
        self.registry = registry
        self.control_event_sink = control_event_sink
        self.pre_dispatch_guard = pre_dispatch_guard

    async def execute(self, decision: RequestDecision, context: RequestContext) -> CapabilityResult:
        if decision.status == RouteStatus.CONTROL_ACTION:
            if self.control_event_sink is None:
                return CapabilityResult(CapabilityResultStatus.UNAVAILABLE, "interaction.control",
                                       message="控制事件没有接入引擎，未执行。", error_code="CONTROL_SINK_MISSING")
            accepted = self.control_event_sink(str(decision.control_event_kind), decision.control_payload)
            if not accepted:
                return CapabilityResult(CapabilityResultStatus.FAILED, "interaction.control",
                                       message="控制事件未被引擎接受。", error_code="CONTROL_EVENT_REJECTED")
            return CapabilityResult(CapabilityResultStatus.OK, "interaction.control", message=decision.response_text,
                                    data={"event_kind": decision.control_event_kind, "payload": dict(decision.control_payload)},
                                    source="local_control")
        if decision.status == RouteStatus.CAPABILITY_QUERY:
            return CapabilityResult(CapabilityResultStatus.OK, "capability.query", message=decision.response_text,
                                    data={"intent": decision.intent.name.value}, source="capability_registry")
        if decision.status in {RouteStatus.NEEDS_CLARIFICATION, RouteStatus.NEEDS_CONFIRMATION}:
            return CapabilityResult(CapabilityResultStatus.NEEDS_INPUT, "request.router",
                                    message=decision.question or decision.response_text,
                                    data={"missing_slots": list(decision.missing_slots), "confirmation_required": decision.requires_confirmation},
                                    error_code=decision.reason_code)
        if decision.status in {RouteStatus.UNSUPPORTED, RouteStatus.BLOCKED, RouteStatus.INVALID}:
            status = CapabilityResultStatus.UNAVAILABLE if decision.status == RouteStatus.UNSUPPORTED else CapabilityResultStatus.FAILED
            return CapabilityResult(status, "request.router", message=decision.response_text,
                                    data={"missing_capabilities": list(decision.missing_capabilities)}, error_code=decision.reason_code)
        if decision.status in {RouteStatus.DELEGATE_LLM, RouteStatus.DELEGATE_TASK_PLANNER}:
            return CapabilityResult(CapabilityResultStatus.UNKNOWN, decision.capability_ids[0] if decision.capability_ids else "request.router",
                                    message="该请求已路由到宿主处理器；核心路由器不会隐式调用 LLM 或任务规划器。",
                                    data={"delegate": decision.status.value, "raw_text": decision.intent.raw_text}, error_code="DELEGATE_REQUIRED")
        if decision.status != RouteStatus.READY or not decision.calls:
            return CapabilityResult(CapabilityResultStatus.FAILED, "request.router", message="路由决策没有可执行调用。", error_code="NO_EXECUTABLE_CALLS")
        last_result: CapabilityResult | None = None
        step_results: list[dict[str, Any]] = []
        for call in decision.calls:
            spec = self.registry.get(call.capability_id)
            if spec is not None and spec.risk_level in {RiskLevel.HIGH, RiskLevel.CRITICAL} and self.pre_dispatch_guard is None:
                return CapabilityResult(
                    CapabilityResultStatus.FAILED, call.capability_id,
                    message="高风险能力缺少执行前安全检查，未执行。",
                    data={"completed_steps": step_results}, error_code="LIVE_PRE_DISPATCH_GUARD_REQUIRED",
                )
            if self.pre_dispatch_guard is not None:
                try:
                    denial_reason = self.pre_dispatch_guard(call, decision.intent, context)
                except Exception as exc:
                    return CapabilityResult(
                        CapabilityResultStatus.FAILED, call.capability_id,
                        message="执行前安全检查异常，已阻止动作。",
                        data={"completed_steps": step_results}, error_code=f"PRE_DISPATCH_GUARD_ERROR:{type(exc).__name__}",
                    )
                if denial_reason:
                    return CapabilityResult(
                        CapabilityResultStatus.FAILED, call.capability_id,
                        message="执行前安全检查拒绝了动作，后续步骤已停止。",
                        data={"completed_steps": step_results, "denial_reason": str(denial_reason)},
                        error_code="PRE_DISPATCH_GUARD_DENIED",
                    )
            try:
                last_result = await self.registry.invoke(call, decision.intent, context)
            except Exception as exc:  # capability failures must not escape the request boundary
                return CapabilityResult(CapabilityResultStatus.FAILED, call.capability_id,
                                        message="能力执行失败，后续步骤已停止。",
                                        data={"completed_steps": step_results},
                                        error_code=f"PROVIDER_EXCEPTION:{type(exc).__name__}")
            step_results.append({"step_id": call.step_id, "capability_id": call.capability_id,
                                 "status": last_result.status.value, "data": dict(last_result.data),
                                 "error_code": last_result.error_code})
            if last_result.status != CapabilityResultStatus.OK:
                return CapabilityResult(last_result.status, last_result.capability_id, message=last_result.message,
                                        data={"steps": step_results, "failed_step": call.step_id},
                                        source=last_result.source, observed_at_wall_s=last_result.observed_at_wall_s,
                                        expires_at_wall_s=last_result.expires_at_wall_s, error_code=last_result.error_code)
        assert last_result is not None
        if len(step_results) == 1:
            return last_result
        return CapabilityResult(CapabilityResultStatus.OK, "composite.request", message=last_result.message,
                                data={"steps": step_results, "final_result": dict(last_result.data)},
                                source="sequential_capability_dispatch")


def build_default_registry(
    *,
    llm_available: bool = False,
    task_planner_available: bool = False,
    mobility_enabled: bool = False,
    providers: Mapping[str, CapabilityHandler] | None = None,
) -> CapabilityRegistry:
    """Create a conservative, extensible registry. External capabilities are opt-in."""
    wired = dict(providers or {})
    registry = CapabilityRegistry()
    registry.register(CapabilitySpec("clock.now", RequestDomain.INFORMATION, "查询本地时间与日期", True, "BUILTIN"), ClockProvider())
    registry.register(CapabilitySpec("conversation.respond", RequestDomain.CONVERSATION, "自然语言对话", llm_available, "LLM"))
    registry.register(CapabilitySpec("knowledge.answer", RequestDomain.INFORMATION, "百科知识解释", llm_available, "LLM"))
    registry.register(CapabilitySpec("knowledge.search", RequestDomain.INFORMATION, "外部资料检索", "knowledge.search" in wired, "PROVIDER"), wired.get("knowledge.search"))
    registry.register(CapabilitySpec("audio.input", RequestDomain.CAPABILITY, "麦克风与语音输入", "audio.input" in wired, "PROVIDER"), wired.get("audio.input"))
    registry.register(CapabilitySpec("audio.output", RequestDomain.CAPABILITY, "语音输出", "audio.output" in wired, "PROVIDER"), wired.get("audio.output"))
    registry.register(CapabilitySpec("weather.current", RequestDomain.INFORMATION, "查询当前天气", "weather.current" in wired, "PROVIDER", required_slots=("location",), freshness_s=900), wired.get("weather.current"))
    registry.register(CapabilitySpec("weather.forecast", RequestDomain.INFORMATION, "查询天气预报", "weather.forecast" in wired, "PROVIDER", required_slots=("location",), freshness_s=3600), wired.get("weather.forecast"))
    registry.register(CapabilitySpec("vision.observe_user", RequestDomain.PERCEPTION, "观察当前用户", "vision.observe_user" in wired, "PROVIDER", risk_level=RiskLevel.MEDIUM, freshness_s=3), wired.get("vision.observe_user"))
    registry.register(CapabilitySpec("vision.observe_object", RequestDomain.PERCEPTION, "识别当前画面中的指定物体", "vision.observe_object" in wired, "PROVIDER", risk_level=RiskLevel.MEDIUM, freshness_s=3), wired.get("vision.observe_object"))
    registry.register(CapabilitySpec("vision.observe_scene", RequestDomain.PERCEPTION, "观察当前场景", "vision.observe_scene" in wired, "PROVIDER", risk_level=RiskLevel.MEDIUM, freshness_s=3), wired.get("vision.observe_scene"))
    registry.register(CapabilitySpec("vision.observe_location", RequestDomain.PERCEPTION, "观察指定房间或位置", "vision.observe_location" in wired, "PROVIDER", required_slots=("location",), risk_level=RiskLevel.HIGH, freshness_s=3), wired.get("vision.observe_location"))
    registry.register(CapabilitySpec("mobility.navigate_to_user", RequestDomain.MOBILITY, "移动到当前用户位置", mobility_enabled and "mobility.navigate_to_user" in wired, "PROVIDER", risk_level=RiskLevel.HIGH, side_effect=True, confirm_below_confidence=0.75), wired.get("mobility.navigate_to_user"))
    registry.register(CapabilitySpec("mobility.navigate_to_location", RequestDomain.MOBILITY, "移动到已配置的位置", mobility_enabled and "mobility.navigate_to_location" in wired, "PROVIDER", required_slots=("location",), risk_level=RiskLevel.HIGH, side_effect=True, confirm_below_confidence=0.75), wired.get("mobility.navigate_to_location"))
    registry.register(CapabilitySpec("reminder.create", RequestDomain.REMINDER, "创建提醒", "reminder.create" in wired, "PROVIDER", required_slots=("text", "time"), side_effect=True), wired.get("reminder.create"))
    registry.register(CapabilitySpec("reminder.list", RequestDomain.REMINDER, "查看待触发提醒", "reminder.list" in wired, "PROVIDER"), wired.get("reminder.list"))
    registry.register(CapabilitySpec("reminder.cancel", RequestDomain.REMINDER, "取消指定提醒", "reminder.cancel" in wired, "PROVIDER", side_effect=True), wired.get("reminder.cancel"))
    registry.register(CapabilitySpec("timer.create", RequestDomain.REMINDER, "创建本地计时器", "timer.create" in wired, "PROVIDER", required_slots=("duration_s",), risk_level=RiskLevel.LOW, side_effect=True), wired.get("timer.create"))
    registry.register(CapabilitySpec("media.control", RequestDomain.MEDIA, "播放、暂停或控制媒体", "media.control" in wired, "PROVIDER", risk_level=RiskLevel.MEDIUM, side_effect=True, confirm_below_confidence=0.80), wired.get("media.control"))
    registry.register(CapabilitySpec("device.control", RequestDomain.DEVICE_CONTROL, "控制已授权的智能设备", "device.control" in wired, "PROVIDER", risk_level=RiskLevel.HIGH, side_effect=True, confirm_below_confidence=0.80), wired.get("device.control"))
    registry.register(CapabilitySpec("task.plan", RequestDomain.TASK, "生成并执行受限任务计划", task_planner_available, "TASK_PLANNER"))
    return registry


def make_engine_control_event_sink(engine: Any) -> Callable[[str, Mapping[str, Any]], bool]:
    """Build a control sink that injects router decisions into a ContinuityEngine.

    The host scheduler still calls `engine.step()`; this helper never steps the engine
    implicitly and never dispatches an action itself.
    """
    def sink(kind: str, payload: Mapping[str, Any]) -> bool:
        priority = int(Priority.CRITICAL if kind in {EventKind.SAFETY_STOP.value, EventKind.USER_STOP.value}
                       else Priority.HIGH)
        event = engine.new_event(kind, dict(payload), source="request_router", priority=priority)
        return bool(engine.inject(event))
    return sink


def build_request_router_from_config(
    config: Mapping[str, Any] | Any,
    *,
    providers: Mapping[str, CapabilityHandler] | None = None,
    llm_available: bool = False,
    task_planner_available: bool = False,
    classifier: IntentClassifier | None = None,
) -> RequestRouter:
    """Build a router from a raw config mapping or ConfigManager.

    Physical/sensor capabilities are enabled only when product flags permit them and a
    provider is explicitly injected. Config may lower capabilities, but cannot make a
    provider-backed capability available without an implementation.
    """
    if isinstance(config, Mapping):
        data = dict(config)
    elif callable(getattr(config, "get", None)):
        data = config.get()
    else:
        raise TypeError("config must be a mapping or expose get()")
    product = data.get("product", {})
    interaction = data.get("interaction", {})
    request_cfg = data.get("request_routing", {})
    if request_cfg.get("enabled", True) is not True:
        raise ValueError("request_routing.enabled is false; host must not construct an active RequestRouter")
    caps_cfg = data.get("capabilities", {})
    supplied = dict(providers or {})
    filtered: dict[str, CapabilityHandler] = {}
    for cap_id, handler in supplied.items():
        if cap_id.startswith("vision.") and not bool(product.get("camera_enabled", False)):
            continue
        if cap_id.startswith("mobility.") and not bool(product.get("autonomous_mobility_enabled", False)):
            continue
        if cap_id.startswith("weather.") and not bool(caps_cfg.get("weather_enabled", False)):
            continue
        if cap_id == "knowledge.search" and not bool(caps_cfg.get("knowledge_search_enabled", False)):
            continue
        if cap_id.startswith("reminder.") and not bool(caps_cfg.get("reminders_enabled", False)):
            continue
        if cap_id == "timer.create" and not bool(caps_cfg.get("timers_enabled", False)):
            continue
        if cap_id == "media.control" and not bool(caps_cfg.get("media_control_enabled", False)):
            continue
        if cap_id == "device.control" and not bool(caps_cfg.get("device_control_enabled", False)):
            continue
        if cap_id.startswith("audio.") and not bool(caps_cfg.get("audio_capabilities_enabled", True)):
            continue
        filtered[cap_id] = handler
    registry = build_default_registry(
        llm_available=llm_available,
        task_planner_available=task_planner_available,
        mobility_enabled=bool(product.get("autonomous_mobility_enabled", False)),
        providers=filtered,
    )
    default_context = RequestContext(
        timezone_name=str(request_cfg.get("default_timezone", "Asia/Tokyo")),
        default_location=request_cfg.get("default_location"),
        protected_locations=tuple(request_cfg.get("protected_locations", ("卧室", "浴室", "卫生间", "bathroom"))),
    )
    max_rounds = int(request_cfg.get("clarification_max_rounds", interaction.get("clarification_max_rounds_per_intent", 2)))
    max_chars = int(request_cfg.get("max_request_chars", 10_000))
    return RequestRouter(registry, classifier, clarification_max_rounds=max_rounds,
                         default_context=default_context, max_request_chars=max_chars)
