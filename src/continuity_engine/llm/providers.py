"""Production-oriented capability providers for reminders, weather and knowledge retrieval.

External network capabilities are opt-in. Unit tests use httpx.MockTransport and never
contact external services. Reminder delivery uses an explicitly injected sink; the
platform default is a desktop notification driver.
"""
from __future__ import annotations

import asyncio
import html
import json
import math
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..execution.real_actions import ActionDriverError, DesktopNotificationDriver
from ..routing.request_router import (
    CapabilityCall, CapabilityResult, CapabilityResultStatus, ClassifiedIntent, RequestContext,
)


class ProviderServiceConfigurationError(ValueError):
    """Raised when a provider cannot be configured safely."""


class ReminderServiceUnavailable(RuntimeError):
    """Raised when the reminder scheduler cannot start its real delivery path."""


class ReminderParseError(ValueError):
    def __init__(self, message: str, code: str = "INVALID_REMINDER_TIME"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ReminderRecord:
    reminder_id: str
    text: str
    due_at_wall_s: float
    timezone_name: str
    status: str
    created_at_wall_s: float
    delivered_at_wall_s: float | None = None
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reminder_id": self.reminder_id,
            "text": self.text,
            "due_at_wall_s": self.due_at_wall_s,
            "timezone": self.timezone_name,
            "status": self.status,
            "created_at_wall_s": self.created_at_wall_s,
            "delivered_at_wall_s": self.delivered_at_wall_s,
            "error_code": self.error_code,
        }


class ReminderDelivery(Protocol):
    async def deliver(self, reminder: ReminderRecord) -> Mapping[str, Any] | bool: ...
    async def health_check(self) -> bool: ...
    async def aclose(self) -> None: ...


class PlatformReminderDelivery:
    """Desktop notification delivery. No speech is forced by a reminder."""

    def __init__(self, driver: DesktopNotificationDriver | None = None):
        self.driver = driver or DesktopNotificationDriver(display_seconds=5)

    async def deliver(self, reminder: ReminderRecord) -> Mapping[str, Any]:
        return await self.driver.notify(
            action_id=f"reminder-{reminder.reminder_id}",
            title="Continuity Engine 提醒",
            text=reminder.text,
            cancel_event=asyncio.Event(),
        )

    async def health_check(self) -> bool:
        return await self.driver.health_check()

    async def aclose(self) -> None:
        await self.driver.close()


_CN_NUMBERS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15,
    "十六": 16, "十七": 17, "十八": 18, "十九": 19, "二十": 20,
    "二十一": 21, "二十二": 22, "二十三": 23, "二十四": 24,
    "三十": 30, "四十": 40, "五十": 50, "六十": 60,
}


def parse_chinese_number(token: str) -> int:
    token = token.strip()
    if token in _CN_NUMBERS:
        return _CN_NUMBERS[token]
    if token == "半":
        return 0  # interpreted specially by duration/time parser
    # General form such as 二十三 or 三十五.
    if "十" in token:
        left, right = token.split("十", 1)
        tens = 1 if not left else _CN_NUMBERS.get(left, 0)
        ones = _CN_NUMBERS.get(right, 0) if right else 0
        if tens and ones >= 0:
            return tens * 10 + ones
    raise ReminderParseError("我没能确定提醒的时间，请说清楚几分钟后或具体日期和时间。", "TIME_EXPRESSION_UNPARSEABLE")


def _parse_duration_expression(expression: str) -> int | None:
    compact = re.sub(r"\s+", "", expression.lower())
    pattern = re.search(
        r"(?:再过|过|在)?(?P<num>\d{1,7}|[零〇一二两三四五六七八九十]+|半)"
        r"(?P<unit>秒钟|秒|分钟|分|小时|小时钟|天|日|时)后(?:面|之后|以后)?",
        compact,
    )
    if not pattern:
        return None
    num = pattern.group("num")
    unit = pattern.group("unit")
    if num == "半":
        if unit in {"分钟", "分"}:
            seconds = 30
        elif unit in {"小时", "小时钟", "时"}:
            seconds = 1800
        else:
            raise ReminderParseError("“半秒”这类时间暂不支持，请给出明确秒数。", "TIME_EXPRESSION_UNSUPPORTED")
    else:
        amount = int(num) if num.isdigit() else parse_chinese_number(num)
        multiplier = 86400 if unit in {"天", "日"} else 3600 if unit in {"小时", "小时钟", "时"} else 60 if unit in {"分钟", "分"} else 1
        seconds = amount * multiplier
    if seconds <= 0:
        raise ReminderParseError("提醒时间必须在未来。", "TIME_NOT_IN_FUTURE")
    if seconds > 30 * 86400:
        raise ReminderParseError("第一版只支持 30 天以内的提醒。", "REMINDER_HORIZON_EXCEEDED")
    return int(seconds)


def parse_reminder_due_at(
    expression: str,
    *,
    timezone_name: str,
    now_wall_s: float | None = None,
    max_horizon_s: float = 30 * 86400,
) -> float:
    """Parse relative or absolute Chinese/ISO reminder time into a Unix timestamp.

    Supports relative phrases such as ``一分钟后`` and ``2小时后``, ISO-like dates,
    and common Chinese date/time phrases (``明天下午三点``). Ambiguous/unparseable
    expressions are rejected rather than guessed.
    """
    if not isinstance(expression, str) or not expression.strip():
        raise ReminderParseError("请说明提醒时间，例如“一分钟后”或“明天下午三点”。", "TIME_REQUIRED")
    if not math.isfinite(max_horizon_s) or not 1 <= max_horizon_s <= 366 * 86400:
        raise ValueError("max_horizon_s must be finite and in [1 second, 366 days]")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ReminderParseError("当前时区配置无效，无法安排提醒。", "INVALID_TIMEZONE") from exc
    now_ts = time.time() if now_wall_s is None else float(now_wall_s)
    if not math.isfinite(now_ts):
        raise ReminderParseError("当前系统时间无效，无法安排提醒。", "INVALID_CLOCK")
    now_local = datetime.fromtimestamp(now_ts, tz=zone)
    text = expression.strip()

    duration = _parse_duration_expression(text)
    if duration is not None:
        if duration > max_horizon_s:
            raise ReminderParseError("提醒时间超出了允许的最远期限。", "REMINDER_HORIZON_EXCEEDED")
        return now_ts + duration

    # ISO date/time with optional seconds and optional explicit UTC offset.
    iso_match = re.search(r"(20\d{2}-\d{1,2}-\d{1,2})(?:[T ](\d{1,2}:\d{2}(?::\d{2})?))?", text)
    if iso_match:
        date_part, time_part = iso_match.groups()
        try:
            day = date.fromisoformat(date_part)
            parsed_time = datetime.strptime(time_part or "09:00", "%H:%M:%S" if time_part and time_part.count(":") == 2 else "%H:%M").time()
            due = datetime.combine(day, parsed_time, tzinfo=zone)
        except ValueError as exc:
            raise ReminderParseError("日期或时间格式无效。", "TIME_EXPRESSION_UNPARSEABLE") from exc
        return _validate_due(due.timestamp(), now_ts, max_horizon_s, explicitly_dated=True)

    explicit_day = False
    day_offset = 0
    if "后天" in text:
        day_offset, explicit_day = 2, True
    elif "明天" in text or "明早" in text or "今晚" in text:
        day_offset, explicit_day = 1 if "明天" in text or "明早" in text else 0, True
    elif "今天" in text:
        day_offset, explicit_day = 0, True

    hour: int | None = None
    minute = 0
    period = None
    if any(word in text for word in ("凌晨", "早上", "上午", "明早")):
        period = "am"
    elif any(word in text for word in ("下午", "晚上", "今晚", "傍晚")):
        period = "pm"

    numeric_time = re.search(r"(?<!\d)(\d{1,2})(?::(\d{2}))(?::\d{2})?", text)
    if numeric_time:
        hour, minute = int(numeric_time.group(1)), int(numeric_time.group(2))
    else:
        chinese_time = re.search(r"([零〇一二两三四五六七八九十]{1,3}|\d{1,2})点(半|一刻|三刻|\d{1,2}分)?", text)
        if chinese_time:
            hour_token, minute_token = chinese_time.groups()
            hour = int(hour_token) if hour_token.isdigit() else parse_chinese_number(hour_token)
            if minute_token == "半":
                minute = 30
            elif minute_token == "一刻":
                minute = 15
            elif minute_token == "三刻":
                minute = 45
            elif minute_token and minute_token.endswith("分"):
                minute = int(minute_token[:-1])
    if hour is None:
        raise ReminderParseError("我没能解析这个具体时间，请说“一分钟后”或“明天下午三点”。", "TIME_EXPRESSION_UNPARSEABLE")
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ReminderParseError("提醒时间不在有效范围内。", "TIME_OUT_OF_RANGE")
    if period == "pm" and 1 <= hour <= 11:
        hour += 12
    elif period == "am" and hour == 12:
        hour = 0
    target_day = now_local.date() + timedelta(days=day_offset)
    due_dt = datetime.combine(target_day, dt_time(hour, minute), tzinfo=zone)
    if due_dt.timestamp() <= now_ts:
        if explicit_day:
            raise ReminderParseError("这个具体时间已经过去了，请重新指定一个未来时间。", "TIME_NOT_IN_FUTURE")
        # An unqualified clock time means the next occurrence.
        due_dt += timedelta(days=1)
    return _validate_due(due_dt.timestamp(), now_ts, max_horizon_s, explicitly_dated=explicit_day)


def _validate_due(due_ts: float, now_ts: float, max_horizon_s: float, *, explicitly_dated: bool) -> float:
    if not math.isfinite(due_ts):
        raise ReminderParseError("提醒时间无效。", "INVALID_DUE_TIME")
    delta = due_ts - now_ts
    if delta <= 0:
        raise ReminderParseError("提醒时间必须在未来。", "TIME_NOT_IN_FUTURE")
    if delta > max_horizon_s:
        raise ReminderParseError("第一版只支持 30 天以内的提醒。", "REMINDER_HORIZON_EXCEEDED")
    return due_ts


class ReminderScheduler:
    """SQLite-backed reminder service with an explicit delivery worker.

    Exactly-once delivery across process crashes cannot be guaranteed for external
    notifications. A reminder found in FIRING after restart becomes UNKNOWN and is
    not automatically re-fired, preventing accidental duplicate reminders.
    """

    capability_id = "reminder.create"

    def __init__(
        self,
        database_path: str | Path,
        *,
        delivery: ReminderDelivery | Callable[[ReminderRecord], Awaitable[Mapping[str, Any] | bool]] | None = None,
        default_timezone: str = "Asia/Tokyo",
        poll_interval_s: float = 0.5,
        delivery_timeout_s: float = 15.0,
        max_horizon_s: float = 30 * 86400,
        clock: Callable[[], float] = time.time,
    ):
        if not 0.05 <= poll_interval_s <= 60 or not math.isfinite(poll_interval_s):
            raise ValueError("poll_interval_s must be finite and in [0.05, 60]")
        if not 0.5 <= delivery_timeout_s <= 120 or not math.isfinite(delivery_timeout_s):
            raise ValueError("delivery_timeout_s must be finite and in [0.5, 120]")
        if not 1 <= max_horizon_s <= 366 * 86400 or not math.isfinite(max_horizon_s):
            raise ValueError("max_horizon_s must be finite and in [1 second, 366 days]")
        try:
            ZoneInfo(default_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"invalid default timezone: {default_timezone}") from exc
        self.database_path = str(database_path)
        self.delivery = delivery if delivery is not None else PlatformReminderDelivery()
        self.default_timezone = default_timezone
        self.poll_interval_s = poll_interval_s
        self.delivery_timeout_s = delivery_timeout_s
        self.max_horizon_s = float(max_horizon_s)
        self.clock = clock
        self._db: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._started = False
        self._closed = False
        self._last_delivery_error: str | None = None

    @property
    def available(self) -> bool:
        return self._started and not self._closed

    async def start(self) -> None:
        if self._closed:
            raise ReminderServiceUnavailable("reminder service is closed")
        if self._started:
            return
        health = getattr(self.delivery, "health_check", None)
        if health is not None:
            healthy = await health()
            if not healthy:
                raise ReminderServiceUnavailable("no usable notification backend found; reminder provider will stay unavailable")
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.database_path, isolation_level=None, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("""CREATE TABLE IF NOT EXISTS reminders (
            reminder_id TEXT PRIMARY KEY,
            text TEXT NOT NULL,
            due_at_wall_s REAL NOT NULL,
            timezone_name TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at_wall_s REAL NOT NULL,
            delivered_at_wall_s REAL,
            error_code TEXT,
            request_id TEXT
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_reminders_due_status ON reminders(status, due_at_wall_s)")
        # A crash after entering FIRING leaves the delivery outcome unknowable.
        # Do not duplicate an external side effect after restart.
        db.execute("UPDATE reminders SET status='UNKNOWN', error_code='PROCESS_RESTART_DURING_DELIVERY' WHERE status='FIRING'")
        self._db = db
        self._started = True
        self._worker = asyncio.create_task(self._run_loop(), name="continuity-reminder-scheduler")
        self._wake.set()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._started = False
        self._wake.set()
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        if self._db is not None:
            self._db.close()
            self._db = None
        close = getattr(self.delivery, "aclose", None)
        if close is not None:
            await close()

    async def __call__(self, call: CapabilityCall, intent: ClassifiedIntent, context: RequestContext) -> CapabilityResult:
        if not self.available or self._db is None:
            return CapabilityResult(CapabilityResultStatus.UNAVAILABLE, call.capability_id,
                                    message="提醒服务当前没有运行，未创建提醒。", error_code="REMINDER_SCHEDULER_NOT_RUNNING")
        if call.capability_id == "reminder.list":
            return await self._handle_list(call)
        if call.capability_id == "reminder.cancel":
            return await self._handle_cancel(call)
        if call.capability_id != "reminder.create":
            return CapabilityResult(CapabilityResultStatus.UNAVAILABLE, call.capability_id,
                                    message="当前提醒服务不支持此操作。", error_code="REMINDER_OPERATION_UNSUPPORTED")
        text = call.arguments.get("text")
        time_expression = call.arguments.get("time")
        if not isinstance(text, str) or not text.strip() or len(text) > 500:
            return CapabilityResult(CapabilityResultStatus.NEEDS_INPUT, call.capability_id,
                                    message="请说明需要提醒的内容（最多 500 个字符）。", error_code="REMINDER_TEXT_REQUIRED")
        if not isinstance(time_expression, str) or not time_expression.strip():
            return CapabilityResult(CapabilityResultStatus.NEEDS_INPUT, call.capability_id,
                                    message="请说明提醒时间，例如“一分钟后”或“明天下午三点”。", error_code="TIME_REQUIRED")
        now = self.clock() if context.now_wall_s is None else float(context.now_wall_s)
        timezone_name = context.timezone_name or self.default_timezone
        try:
            due_at = parse_reminder_due_at(time_expression, timezone_name=timezone_name,
                                           now_wall_s=now, max_horizon_s=self.max_horizon_s)
        except ReminderParseError as exc:
            return CapabilityResult(CapabilityResultStatus.NEEDS_INPUT, call.capability_id,
                                    message=str(exc), error_code=exc.code)
        reminder_id = uuid.uuid4().hex
        try:
            async with self._lock:
                assert self._db is not None
                self._db.execute(
                    "INSERT INTO reminders(reminder_id,text,due_at_wall_s,timezone_name,status,created_at_wall_s,request_id) VALUES(?,?,?,?,?,?,?)",
                    (reminder_id, text.strip(), due_at, timezone_name, "PENDING", now, call.step_id),
                )
        except sqlite3.Error as exc:
            return CapabilityResult(CapabilityResultStatus.FAILED, call.capability_id,
                                    message="提醒保存失败，因此没有创建成功。", error_code=f"REMINDER_DB_{type(exc).__name__}")
        self._wake.set()
        due_dt = datetime.fromtimestamp(due_at, ZoneInfo(timezone_name))
        return CapabilityResult(
            CapabilityResultStatus.OK, call.capability_id,
            message=f"已设置提醒：{text.strip()}，时间是{due_dt.strftime('%Y年%m月%d日 %H:%M')}。",
            data={"reminder_id": reminder_id, "text": text.strip(), "due_at_wall_s": due_at,
                  "due_at_local": due_dt.isoformat(), "timezone": timezone_name, "status": "PENDING"},
            source="sqlite_persistent_reminder", observed_at_wall_s=now,
        )

    async def list_reminders(self, *, statuses: tuple[str, ...] = ("PENDING", "UNKNOWN", "FAILED"), limit: int = 100) -> list[dict[str, Any]]:
        if self._db is None:
            raise ReminderServiceUnavailable("reminder service is not started")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be in [1,1000]")
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        async with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM reminders WHERE status IN ({placeholders}) ORDER BY due_at_wall_s LIMIT ?",
                (*statuses, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    async def cancel(self, reminder_id: str) -> bool:
        if self._db is None:
            raise ReminderServiceUnavailable("reminder service is not started")
        async with self._lock:
            cursor = self._db.execute(
                "UPDATE reminders SET status='CANCELLED' WHERE reminder_id=? AND status='PENDING'",
                (reminder_id,),
            )
        self._wake.set()
        return cursor.rowcount == 1

    async def get(self, reminder_id: str) -> dict[str, Any] | None:
        if self._db is None:
            raise ReminderServiceUnavailable("reminder service is not started")
        async with self._lock:
            row = self._db.execute("SELECT * FROM reminders WHERE reminder_id=?", (reminder_id,)).fetchone()
        return dict(row) if row is not None else None

    async def _run_loop(self) -> None:
        while not self._closed:
            if self._db is None:
                return
            # Clear before querying so a newly inserted reminder cannot have its wake
            # signal lost between a no-row query and the subsequent wait.
            self._wake.clear()
            async with self._lock:
                row = self._db.execute(
                    "SELECT * FROM reminders WHERE status='PENDING' ORDER BY due_at_wall_s LIMIT 1"
                ).fetchone()
            if row is None:
                try:
                    await self._wake.wait()
                except asyncio.CancelledError:
                    raise
                continue
            delay = float(row["due_at_wall_s"]) - self.clock()
            if delay > 0:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=min(delay, 60.0))
                except asyncio.TimeoutError:
                    pass
                except asyncio.CancelledError:
                    raise
                continue
            await self._deliver_due(str(row["reminder_id"]))

    async def _handle_list(self, call: CapabilityCall) -> CapabilityResult:
        rows = await self.list_reminders(statuses=("PENDING", "UNKNOWN", "FAILED", "FIRING"), limit=100)
        if not rows:
            return CapabilityResult(CapabilityResultStatus.OK, call.capability_id,
                                    message="当前没有待触发或需要确认的提醒。", data={"reminders": [], "count": 0},
                                    source="sqlite_persistent_reminder", observed_at_wall_s=self.clock())
        summaries = []
        for row in rows:
            try:
                due = datetime.fromtimestamp(float(row["due_at_wall_s"]), ZoneInfo(str(row["timezone_name"])))
                due_text = due.strftime("%Y-%m-%d %H:%M")
            except Exception:
                due_text = "时间未知"
            summaries.append({"reminder_id": row["reminder_id"], "text": row["text"],
                              "due_at_local": due_text, "timezone": row["timezone_name"],
                              "status": row["status"], "error_code": row.get("error_code")})
        message = "当前提醒：" + "；".join(
            f"{item['text']}（{item['due_at_local']}，状态 {item['status']}，编号 {item['reminder_id'][:8]}）"
            for item in summaries
        )
        return CapabilityResult(CapabilityResultStatus.OK, call.capability_id,
                                message=message, data={"reminders": summaries, "count": len(summaries)},
                                source="sqlite_persistent_reminder", observed_at_wall_s=self.clock())

    async def _handle_cancel(self, call: CapabilityCall) -> CapabilityResult:
        query = call.arguments.get("query")
        reminder_id = call.arguments.get("reminder_id")
        if reminder_id is not None and not isinstance(reminder_id, str):
            return CapabilityResult(CapabilityResultStatus.FAILED, call.capability_id,
                                    message="提醒编号无效。", error_code="INVALID_REMINDER_ID")
        if query is not None and (not isinstance(query, str) or len(query) > 500):
            return CapabilityResult(CapabilityResultStatus.FAILED, call.capability_id,
                                    message="提醒内容筛选条件无效。", error_code="INVALID_REMINDER_QUERY")
        if reminder_id:
            row = await self.get(reminder_id)
            candidates = [row] if row and row.get("status") == "PENDING" else []
        else:
            candidates = await self.list_reminders(statuses=("PENDING",), limit=100)
            if query and query.strip():
                needle = query.strip().casefold()
                candidates = [item for item in candidates if needle in str(item.get("text", "")).casefold()]
        if not candidates:
            return CapabilityResult(CapabilityResultStatus.NOT_FOUND, call.capability_id,
                                    message="没有找到匹配的待触发提醒，没有执行取消。",
                                    data={"query": query, "reminder_id": reminder_id}, error_code="REMINDER_NOT_FOUND")
        if len(candidates) > 1:
            choices = [{"reminder_id": x["reminder_id"], "text": x["text"], "due_at_wall_s": x["due_at_wall_s"]}
                       for x in candidates[:10]]
            return CapabilityResult(CapabilityResultStatus.NEEDS_INPUT, call.capability_id,
                                    message="找到多个匹配提醒，请指定内容或编号；没有取消任何提醒。",
                                    data={"candidates": choices}, error_code="REMINDER_CANCEL_AMBIGUOUS")
        target = str(candidates[0]["reminder_id"])
        did_cancel = await self.cancel(target)
        if not did_cancel:
            return CapabilityResult(CapabilityResultStatus.FAILED, call.capability_id,
                                    message="提醒状态已发生变化，没有取消。", data={"reminder_id": target},
                                    error_code="REMINDER_STATE_CHANGED")
        return CapabilityResult(CapabilityResultStatus.OK, call.capability_id,
                                message=f"已取消提醒：{candidates[0]['text']}。",
                                data={"reminder_id": target, "text": candidates[0]["text"], "status": "CANCELLED"},
                                source="sqlite_persistent_reminder", observed_at_wall_s=self.clock())

    async def run_due_once(self) -> int:
        """Process all reminders currently due; useful for explicit host catch-up and tests."""
        if self._db is None or not self.available:
            raise ReminderServiceUnavailable("reminder service is not started")
        async with self._lock:
            rows = self._db.execute(
                "SELECT reminder_id FROM reminders WHERE status='PENDING' AND due_at_wall_s<=? ORDER BY due_at_wall_s",
                (self.clock(),),
            ).fetchall()
        count = 0
        for row in rows:
            processed = await self._deliver_due(str(row["reminder_id"]))
            if processed:
                count += 1
        return count

    async def _deliver_due(self, reminder_id: str) -> bool:
        """Claim and process a due reminder; return false if another worker won the claim."""
        if self._db is None:
            return False
        async with self._lock:
            cursor = self._db.execute(
                "UPDATE reminders SET status='FIRING' WHERE reminder_id=? AND status='PENDING' AND due_at_wall_s<=?",
                (reminder_id, self.clock()),
            )
            if cursor.rowcount != 1:
                return False
            row = self._db.execute("SELECT * FROM reminders WHERE reminder_id=?", (reminder_id,)).fetchone()
        record = self._row_to_record(row)
        try:
            result = await asyncio.wait_for(self._invoke_delivery(record), timeout=self.delivery_timeout_s)
            success = result is True or (isinstance(result, Mapping) and result.get("ok", True) is True)
            status = "FIRED" if success else "FAILED"
            error = None if success else "DELIVERY_REJECTED"
        except asyncio.TimeoutError:
            status, error = "UNKNOWN", "DELIVERY_TIMEOUT_OUTCOME_UNKNOWN"
        except ActionDriverError as exc:
            status = "UNKNOWN" if exc.outcome_unknown else "FAILED"
            error = exc.code
        except asyncio.CancelledError:
            # The external notification may already have been displayed.
            async with self._lock:
                self._db.execute("UPDATE reminders SET status='UNKNOWN', error_code='SCHEDULER_CANCELLED_DURING_DELIVERY' WHERE reminder_id=? AND status='FIRING'", (reminder_id,))
            raise
        except Exception as exc:  # do not auto-retry a possibly delivered notification
            status, error = "UNKNOWN", f"DELIVERY_EXCEPTION_{type(exc).__name__}"
        async with self._lock:
            self._db.execute(
                "UPDATE reminders SET status=?, delivered_at_wall_s=?, error_code=? WHERE reminder_id=? AND status='FIRING'",
                (status, self.clock(), error, reminder_id),
            )
        if status != "FIRED":
            self._last_delivery_error = error
        return True

    async def _invoke_delivery(self, reminder: ReminderRecord) -> Mapping[str, Any] | bool:
        deliver = getattr(self.delivery, "deliver", None)
        if deliver is not None:
            return await deliver(reminder)
        result = self.delivery(reminder)  # type: ignore[operator]
        if asyncio.iscoroutine(result):
            return await result
        raise TypeError("reminder delivery callback must return an awaitable")

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ReminderRecord:
        return ReminderRecord(
            reminder_id=str(row["reminder_id"]), text=str(row["text"]),
            due_at_wall_s=float(row["due_at_wall_s"]), timezone_name=str(row["timezone_name"]),
            status=str(row["status"]), created_at_wall_s=float(row["created_at_wall_s"]),
            delivered_at_wall_s=None if row["delivered_at_wall_s"] is None else float(row["delivered_at_wall_s"]),
            error_code=row["error_code"],
        )


_WMO_ZH = {
    0: "晴朗", 1: "大致晴朗", 2: "局部多云", 3: "阴天", 45: "雾", 48: "雾凇雾",
    51: "毛毛雨", 53: "中等毛毛雨", 55: "强毛毛雨", 56: "轻微冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "轻微冻雨", 67: "强冻雨", 71: "小雪",
    73: "中雪", 75: "大雪", 77: "米雪", 80: "小阵雨", 81: "中阵雨", 82: "强阵雨",
    85: "小阵雪", 86: "强阵雪", 95: "雷暴", 96: "雷暴并有小冰雹", 99: "雷暴并有大冰雹",
}


class OpenMeteoWeatherProvider:
    """Weather provider using Open-Meteo geocoding + forecast APIs.

    This makes real external HTTP calls only when explicitly constructed and wired.
    It uses the provider's public non-commercial endpoint and returns source/time metadata.
    """

    GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
    FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, *, client: Any | None = None, timeout_s: float = 8.0, max_location_chars: int = 100):
        if not math.isfinite(timeout_s) or not 1 <= timeout_s <= 30:
            raise ValueError("timeout_s must be finite and in [1,30]")
        if not 1 <= max_location_chars <= 200:
            raise ValueError("max_location_chars must be in [1,200]")
        try:
            import httpx
        except ImportError as exc:
            raise ProviderServiceConfigurationError("Weather provider requires the optional dependency httpx; install continuity-engine[provider].") from exc
        self._httpx = httpx
        self.client = client or httpx.AsyncClient(timeout=timeout_s, headers={"User-Agent": "ContinuityEngine/0.9 weather-provider"})
        self._owns_client = client is None
        self.timeout_s = timeout_s
        self.max_location_chars = max_location_chars
        self.available = True

    async def __call__(self, call: CapabilityCall, intent: ClassifiedIntent, context: RequestContext) -> CapabilityResult:
        location = call.arguments.get("location") or context.default_location
        if not isinstance(location, str) or not location.strip():
            return CapabilityResult(CapabilityResultStatus.NEEDS_INPUT, call.capability_id,
                                    message="请告诉我想查询的城市或地区。", error_code="WEATHER_LOCATION_REQUIRED")
        location = location.strip()
        if len(location) > self.max_location_chars or any(ord(c) < 32 for c in location):
            return CapabilityResult(CapabilityResultStatus.FAILED, call.capability_id,
                                    message="地点名称无效或过长。", error_code="INVALID_LOCATION")
        try:
            geocode = await self._get_json(self.GEOCODING_URL, {
                "name": location, "count": 5, "language": "zh", "format": "json",
            })
            results = geocode.get("results") or []
            if not results:
                return CapabilityResult(CapabilityResultStatus.NOT_FOUND, call.capability_id,
                                        message=f"没有找到“{location}”对应的天气地点。", data={"location_query": location},
                                        source="Open-Meteo Geocoding", error_code="LOCATION_NOT_FOUND")
            selected = self._select_location(location, results)
            if selected is None:
                candidates = [self._location_label(x) for x in results[:5]]
                return CapabilityResult(CapabilityResultStatus.NEEDS_INPUT, call.capability_id,
                                        message="找到多个可能的地点，请补充国家或地区：" + "、".join(candidates),
                                        data={"location_query": location, "candidates": candidates}, source="Open-Meteo Geocoding",
                                        error_code="AMBIGUOUS_LOCATION")
            data = await self._get_json(self.FORECAST_URL, {
                "latitude": selected["latitude"], "longitude": selected["longitude"],
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,is_day,precipitation,weather_code,wind_speed_10m",
                "hourly": "temperature_2m,precipitation_probability,weather_code",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset",
                "timezone": "auto", "forecast_days": 7,
            })
            if call.capability_id == "weather.current":
                return self._current_result(call.capability_id, location, selected, data)
            query = str(call.arguments.get("query") or intent.raw_text)
            forecast_day = str(call.arguments.get("forecast_day") or "tomorrow")
            return self._forecast_result(call.capability_id, location, selected, data, query, forecast_day)
        except self._httpx.TimeoutException:
            return CapabilityResult(CapabilityResultStatus.UNAVAILABLE, call.capability_id,
                                    message="天气服务响应超时，暂时无法确认天气。", source="Open-Meteo",
                                    error_code="WEATHER_TIMEOUT")
        except self._httpx.HTTPError as exc:
            return CapabilityResult(CapabilityResultStatus.UNAVAILABLE, call.capability_id,
                                    message="天气服务暂时不可用，未能取得可靠数据。", source="Open-Meteo",
                                    error_code=f"WEATHER_HTTP_{type(exc).__name__}")
        except (KeyError, TypeError, ValueError, IndexError, OverflowError) as exc:
            return CapabilityResult(CapabilityResultStatus.FAILED, call.capability_id,
                                    message="天气服务返回的数据不完整或格式异常，未将其作为事实使用。", source="Open-Meteo",
                                    error_code=f"WEATHER_INVALID_RESPONSE_{type(exc).__name__}")

    async def _get_json(self, url: str, params: Mapping[str, Any]) -> dict[str, Any]:
        response = await self.client.get(url, params=params)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("response root must be an object")
        if "error" in payload and payload.get("error"):
            raise ValueError("provider returned an error")
        return payload

    @staticmethod
    def _location_label(row: Mapping[str, Any]) -> str:
        parts = [str(row.get("name", "")).strip(), str(row.get("admin1", "")).strip(), str(row.get("country", "")).strip()]
        return "，".join(dict.fromkeys(x for x in parts if x))

    @classmethod
    def _select_location(cls, query: str, rows: list[Any]) -> Mapping[str, Any] | None:
        valid = [r for r in rows if isinstance(r, Mapping) and isinstance(r.get("latitude"), (int, float))
                 and isinstance(r.get("longitude"), (int, float))]
        if not valid:
            return None
        norm = query.strip().casefold()
        exact = [r for r in valid if str(r.get("name", "")).strip().casefold() == norm]
        candidates = exact if exact else valid
        # Exact same-name results across multiple countries are genuinely ambiguous.
        distinct = {(str(r.get("name", "")).casefold(), str(r.get("country", "")).casefold(), str(r.get("admin1", "")).casefold()) for r in candidates}
        if len(distinct) > 1 and len(exact) > 1:
            return None
        return candidates[0]

    @staticmethod
    def _weather_label(code: Any) -> str:
        try:
            return _WMO_ZH.get(int(code), "天气现象未分类")
        except (TypeError, ValueError):
            return "天气现象未分类"

    @staticmethod
    def _parse_local_timestamp(value: Any, timezone_name: str) -> float:
        if not isinstance(value, str):
            raise ValueError("timestamp must be a string")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
        return parsed.timestamp()

    @classmethod
    def _current_result(cls, capability_id: str, requested_location: str,
                        selected: Mapping[str, Any], payload: Mapping[str, Any]) -> CapabilityResult:
        current = payload.get("current")
        if not isinstance(current, Mapping):
            raise ValueError("missing current conditions")
        timezone_name = str(payload.get("timezone") or "UTC")
        observed = cls._parse_local_timestamp(current.get("time"), timezone_name)
        code = current.get("weather_code")
        location_label = cls._location_label(selected)
        info = {
            "location": location_label, "requested_location": requested_location,
            "temperature_c": current.get("temperature_2m"),
            "feels_like_c": current.get("apparent_temperature"),
            "relative_humidity_pct": current.get("relative_humidity_2m"),
            "precipitation_mm": current.get("precipitation"),
            "wind_speed_kmh": current.get("wind_speed_10m"),
            "weather_code": code, "condition_zh": cls._weather_label(code),
            "local_time": current.get("time"), "timezone": timezone_name,
            "latitude": selected.get("latitude"), "longitude": selected.get("longitude"),
            "source_url": "https://open-meteo.com/",
        }
        message = (f"{location_label}当前{info['condition_zh']}，气温 {info['temperature_c']}°C，"
                   f"体感 {info['feels_like_c']}°C，降水 {info['precipitation_mm']} 毫米，"
                   f"风速 {info['wind_speed_kmh']} 公里/小时。数据时间：{current.get('time')}（{timezone_name}）。")
        return CapabilityResult(CapabilityResultStatus.OK, capability_id, message=message, data=info,
                                source="Open-Meteo", observed_at_wall_s=observed, expires_at_wall_s=observed + 900)

    @classmethod
    def _forecast_result(cls, capability_id: str, requested_location: str, selected: Mapping[str, Any],
                         payload: Mapping[str, Any], query: str, forecast_day: str) -> CapabilityResult:
        daily = payload.get("daily")
        if not isinstance(daily, Mapping) or not isinstance(daily.get("time"), list):
            raise ValueError("missing daily forecast")
        dates = daily["time"]
        q = query.replace(" ", "")
        if forecast_day == "day_after_tomorrow" or "后天" in q:
            idx = 2
        elif forecast_day == "today" or "今天" in q:
            idx = 0
        elif forecast_day == "weekend" or "周末" in q:
            # Return up to two upcoming weekend days found in the provider's daily list.
            date_values = [date.fromisoformat(str(x)) for x in dates]
            today_local = date_values[0]
            candidates = [i for i, d in enumerate(date_values) if d.weekday() >= 5 and d >= today_local]
            if candidates:
                rows = [cls._daily_at(daily, i) for i in candidates[:2]]
                location_label = cls._location_label(selected)
                rendered = "；".join(cls._render_day(x) for x in rows)
                return CapabilityResult(CapabilityResultStatus.OK, capability_id,
                    message=f"{location_label}周末预报：{rendered}。天气来源：Open-Meteo。", data={
                        "location": location_label, "requested_location": requested_location, "days": rows,
                        "timezone": payload.get("timezone"), "source_url": "https://open-meteo.com/"},
                    source="Open-Meteo", observed_at_wall_s=time.time(), expires_at_wall_s=time.time() + 1800)
            idx = min(1, len(dates) - 1)
        elif "今晚" in q or "下午" in q or forecast_day in {"tonight", "afternoon"}:
            idx = 0 if len(dates) else 1
            location_label = cls._location_label(selected)
            period_message = "今晚" if "今晚" in q or forecast_day == "tonight" else "下午"
            row = cls._daily_at(daily, idx)
            hourly = payload.get("hourly")
            if isinstance(hourly, Mapping):
                row["hourly_summary"] = cls._hourly_period(hourly, payload.get("timezone", "UTC"), period_message)
            message = f"{location_label}{period_message}预报：{cls._render_day(row)}。天气来源：Open-Meteo。"
            now = time.time()
            return CapabilityResult(CapabilityResultStatus.OK, capability_id, message=message,
                data={"location": location_label, "requested_location": requested_location, **row,
                      "timezone": payload.get("timezone"), "source_url": "https://open-meteo.com/"},
                source="Open-Meteo", observed_at_wall_s=now, expires_at_wall_s=now + 1800)
        else:
            idx = 1 if len(dates) > 1 else 0
        if idx >= len(dates):
            raise ValueError("requested forecast day is outside API response")
        row = cls._daily_at(daily, idx)
        location_label = cls._location_label(selected)
        day_label = {0: "今天", 1: "明天", 2: "后天"}.get(idx, row["date"])
        message = f"{location_label}{day_label}预报：{cls._render_day(row)}。天气来源：Open-Meteo。"
        now = time.time()
        return CapabilityResult(CapabilityResultStatus.OK, capability_id, message=message,
            data={"location": location_label, "requested_location": requested_location, **row,
                  "timezone": payload.get("timezone"), "source_url": "https://open-meteo.com/"},
            source="Open-Meteo", observed_at_wall_s=now, expires_at_wall_s=now + 1800)

    @staticmethod
    def _daily_at(daily: Mapping[str, Any], idx: int) -> dict[str, Any]:
        def at(key: str) -> Any:
            values = daily.get(key)
            return values[idx] if isinstance(values, list) and idx < len(values) else None
        return {
            "date": at("time"), "weather_code": at("weather_code"),
            "condition_zh": OpenMeteoWeatherProvider._weather_label(at("weather_code")),
            "temperature_max_c": at("temperature_2m_max"), "temperature_min_c": at("temperature_2m_min"),
            "precipitation_probability_max_pct": at("precipitation_probability_max"),
            "sunrise": at("sunrise"), "sunset": at("sunset"),
        }

    @staticmethod
    def _hourly_period(hourly: Mapping[str, Any], timezone_name: str, period: str) -> dict[str, Any] | None:
        times = hourly.get("time")
        if not isinstance(times, list):
            return None
        try:
            now_local = datetime.now(ZoneInfo(str(timezone_name)))
        except (ZoneInfoNotFoundError, ValueError):
            now_local = datetime.now(timezone.utc)
        target_hour = 20 if period == "今晚" else 15
        candidates: list[tuple[int, int]] = []
        for i, raw in enumerate(times):
            try:
                dt = datetime.fromisoformat(str(raw)).replace(tzinfo=now_local.tzinfo)
            except ValueError:
                continue
            if dt.date() == now_local.date() and dt.hour >= target_hour and dt >= now_local.replace(tzinfo=None):
                candidates.append((abs(dt.hour - target_hour), i))
        if not candidates:
            return None
        idx = min(candidates)[1]
        def at(key: str) -> Any:
            vals = hourly.get(key)
            return vals[idx] if isinstance(vals, list) and idx < len(vals) else None
        return {"time": times[idx], "temperature_c": at("temperature_2m"),
                "precipitation_probability_pct": at("precipitation_probability"),
                "condition_zh": OpenMeteoWeatherProvider._weather_label(at("weather_code"))}

    @staticmethod
    def _render_day(row: Mapping[str, Any]) -> str:
        text = (f"{row.get('condition_zh', '天气未分类')}，最低 {row.get('temperature_min_c')}°C，"
                f"最高 {row.get('temperature_max_c')}°C，降水概率最高 {row.get('precipitation_probability_max_pct')}%")
        hourly = row.get("hourly_summary")
        if isinstance(hourly, Mapping):
            text += (f"；{hourly.get('time')}约 {hourly.get('temperature_c')}°C，"
                     f"降水概率 {hourly.get('precipitation_probability_pct')}%")
        return text

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()


class WikipediaKnowledgeProvider:
    """A bounded Wikipedia MediaWiki search provider (Chinese or English wiki).

    Returns source-backed extracts and canonical URLs. It does not let search results
    execute tools and does not claim Wikipedia is an exhaustive authority.
    """

    def __init__(self, *, client: Any | None = None, timeout_s: float = 10.0,
                 max_results: int = 3, max_extract_chars: int = 1400, cache_ttl_s: float = 900.0):
        if not math.isfinite(timeout_s) or not 1 <= timeout_s <= 30:
            raise ValueError("timeout_s must be finite and in [1,30]")
        if not 1 <= max_results <= 5 or not 200 <= max_extract_chars <= 5000:
            raise ValueError("invalid knowledge result limits")
        if not math.isfinite(cache_ttl_s) or not 0 <= cache_ttl_s <= 86400:
            raise ValueError("cache_ttl_s must be finite and in [0,86400]")
        try:
            import httpx
        except ImportError as exc:
            raise ProviderServiceConfigurationError("Knowledge provider requires the optional dependency httpx; install continuity-engine[provider].") from exc
        self._httpx = httpx
        self.client = client or httpx.AsyncClient(timeout=timeout_s, headers={"User-Agent": "ContinuityEngine/0.9 knowledge-retrieval"})
        self._owns_client = client is None
        self.max_results = max_results
        self.max_extract_chars = max_extract_chars
        self.cache_ttl_s = cache_ttl_s
        self._cache: dict[tuple[str, str], tuple[float, CapabilityResult]] = {}
        self.available = True

    async def __call__(self, call: CapabilityCall, intent: ClassifiedIntent, context: RequestContext) -> CapabilityResult:
        query = call.arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return CapabilityResult(CapabilityResultStatus.NEEDS_INPUT, call.capability_id,
                                    message="请告诉我想查询的主题。", error_code="KNOWLEDGE_QUERY_REQUIRED")
        query = query.strip()
        if len(query) > 500 or any(ord(c) < 32 for c in query):
            return CapabilityResult(CapabilityResultStatus.FAILED, call.capability_id,
                                    message="查询内容无效或过长。", error_code="INVALID_KNOWLEDGE_QUERY")
        language = "zh" if re.search(r"[\u3400-\u9fff]", query) else "en"
        key = (language, query.casefold())
        cached = self._cache.get(key)
        now = time.time() if context.now_wall_s is None else float(context.now_wall_s)
        if cached is not None and now < cached[0]:
            original = cached[1]
            return CapabilityResult(original.status, original.capability_id, original.message,
                                    {**dict(original.data), "cached": True}, original.source,
                                    original.observed_at_wall_s, original.expires_at_wall_s, original.error_code)
        endpoint = f"https://{language}.wikipedia.org/w/api.php"
        try:
            search = await self._get_json(endpoint, {
                "action": "query", "list": "search", "srsearch": query, "srlimit": self.max_results,
                "format": "json", "utf8": 1, "formatversion": 2,
            })
            rows = search.get("query", {}).get("search", [])
            if not isinstance(rows, list) or not rows:
                result = CapabilityResult(CapabilityResultStatus.NOT_FOUND, call.capability_id,
                    message=f"没有在维基百科检索到“{query}”的匹配条目。", data={"query": query, "results": []},
                    source=f"Wikipedia ({language})", observed_at_wall_s=now, expires_at_wall_s=now + self.cache_ttl_s,
                    error_code="KNOWLEDGE_NOT_FOUND")
                self._cache[key] = (now + self.cache_ttl_s, result)
                return result
            titles = [str(row.get("title", "")) for row in rows[:self.max_results] if isinstance(row, Mapping) and row.get("title")]
            pages_payload = await self._get_json(endpoint, {
                "action": "query", "prop": "extracts|info", "exintro": 1, "explaintext": 1,
                "inprop": "url", "redirects": 1, "titles": "|".join(titles), "format": "json", "utf8": 1,
                "formatversion": 2,
            })
            pages = pages_payload.get("query", {}).get("pages", [])
            if isinstance(pages, Mapping):
                pages = list(pages.values())
            if not isinstance(pages, list):
                raise ValueError("pages must be a list")
            by_title = {str(p.get("title", "")).casefold(): p for p in pages if isinstance(p, Mapping)}
            result_rows: list[dict[str, Any]] = []
            for search_row in rows[:self.max_results]:
                if not isinstance(search_row, Mapping):
                    continue
                title = str(search_row.get("title", ""))
                page = by_title.get(title.casefold(), {})
                extract = re.sub(r"\s+", " ", str(page.get("extract") or "")).strip()
                if not extract:
                    extract = html.unescape(re.sub(r"<[^>]+>", " ", str(search_row.get("snippet") or "")))
                    extract = re.sub(r"\s+", " ", extract).strip()
                extract = extract[:self.max_extract_chars]
                url = str(page.get("fullurl") or f"https://{language}.wikipedia.org/wiki/" + title.replace(" ", "_"))
                if not url.startswith(f"https://{language}.wikipedia.org/"):
                    continue
                result_rows.append({"title": title, "extract": extract, "url": url})
            if not result_rows:
                return CapabilityResult(CapabilityResultStatus.NOT_FOUND, call.capability_id,
                    message=f"检索到匹配条目，但没有取得可用摘要：{query}", data={"query": query, "results": []},
                    source=f"Wikipedia ({language})", observed_at_wall_s=now, error_code="EMPTY_KNOWLEDGE_EXTRACT")
            lines = [f"{i}. {item['title']}：{item['extract']} 来源：{item['url']}" for i, item in enumerate(result_rows, 1)]
            result = CapabilityResult(CapabilityResultStatus.OK, call.capability_id,
                message="\n".join(lines), data={"query": query, "results": result_rows, "language": language},
                source=f"Wikipedia ({language})", observed_at_wall_s=now, expires_at_wall_s=now + self.cache_ttl_s)
            self._cache[key] = (now + self.cache_ttl_s, result)
            # Bound cache growth and purge expired entries.
            if len(self._cache) > 256:
                self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
                while len(self._cache) > 200:
                    self._cache.pop(next(iter(self._cache)))
            return result
        except self._httpx.TimeoutException:
            return CapabilityResult(CapabilityResultStatus.UNAVAILABLE, call.capability_id,
                                    message="知识检索服务响应超时。", source=f"Wikipedia ({language})", error_code="KNOWLEDGE_TIMEOUT")
        except self._httpx.HTTPError as exc:
            return CapabilityResult(CapabilityResultStatus.UNAVAILABLE, call.capability_id,
                                    message="知识检索服务暂时不可用。", source=f"Wikipedia ({language})",
                                    error_code=f"KNOWLEDGE_HTTP_{type(exc).__name__}")
        except (ValueError, TypeError, KeyError) as exc:
            return CapabilityResult(CapabilityResultStatus.FAILED, call.capability_id,
                                    message="知识检索返回的数据格式无效。", source=f"Wikipedia ({language})",
                                    error_code=f"KNOWLEDGE_INVALID_RESPONSE_{type(exc).__name__}")

    async def _get_json(self, endpoint: str, params: Mapping[str, Any]) -> dict[str, Any]:
        response = await self.client.get(endpoint, params=params)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("response root must be an object")
        if payload.get("error"):
            raise ValueError("provider returned an API error")
        return payload

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()


class ProviderBundle:
    """Groups provider handlers and manages lifecycle for background services."""

    def __init__(self, providers: Mapping[str, Any], *, services: tuple[Any, ...] = (), client: Any | None = None,
                 owns_client: bool = False):
        self.providers = dict(providers)
        self.services = services
        self.client = client
        self.owns_client = owns_client
        self.start_errors: dict[str, str] = {}
        self._started = False
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("provider bundle is closed")
        if self._started:
            return
        for service in self.services:
            try:
                await service.start()
            except Exception as exc:
                name = getattr(service, "capability_id", type(service).__name__)
                self.start_errors[name] = type(exc).__name__
                # Keep handler registered but dynamic availability reports false.
        self._started = True

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        close_errors = []
        for service in reversed(self.services):
            close = getattr(service, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception as exc:
                    close_errors.append(exc)
        # Weather and knowledge may share one client, so close the bundle-owned one once.
        if self.client is not None and self.owns_client:
            try:
                await self.client.aclose()
            except Exception as exc:
                close_errors.append(exc)
        else:
            for provider in {id(v): v for v in self.providers.values()}.values():
                close = getattr(provider, "aclose", None)
                if close is not None:
                    try:
                        await close()
                    except Exception as exc:
                        close_errors.append(exc)
        if close_errors:
            raise RuntimeError(f"failed to close {len(close_errors)} provider resource(s)") from close_errors[0]


def build_v09_provider_bundle(
    config: Mapping[str, Any] | Any,
    *,
    reminder_database_path: str | Path | None = None,
    reminder_delivery: ReminderDelivery | Callable[[ReminderRecord], Awaitable[Mapping[str, Any] | bool]] | None = None,
    http_client: Any | None = None,
) -> ProviderBundle:
    """Build explicit v0.9 capabilities from config.

    Reminders are local but still opt into a real OS notification at startup. Weather
    and Wikipedia only get constructed when their flags are explicitly enabled.
    An injected httpx client is useful for testing with MockTransport.
    """
    if isinstance(config, Mapping):
        data = dict(config)
    elif callable(getattr(config, "get", None)):
        data = config.get()
    else:
        raise TypeError("config must be a mapping or expose get()")
    caps = data.get("capabilities", {})
    svc_cfg = data.get("provider_services", {})
    reminder_cfg = svc_cfg.get("reminder", {})
    weather_cfg = svc_cfg.get("weather", {})
    knowledge_cfg = svc_cfg.get("knowledge", {})
    providers: dict[str, Any] = {}
    services: list[Any] = []
    owned_client = False
    client = http_client

    if bool(caps.get("reminders_enabled", False)):
        reminder = ReminderScheduler(
            reminder_database_path or reminder_cfg.get("database_path", "data/reminders.sqlite3"),
            delivery=reminder_delivery,
            default_timezone=str(data.get("request_routing", {}).get("default_timezone", "Asia/Tokyo")),
            poll_interval_s=float(reminder_cfg.get("poll_interval_s", 0.5)),
            delivery_timeout_s=float(reminder_cfg.get("delivery_timeout_s", 15.0)),
            max_horizon_s=float(reminder_cfg.get("max_horizon_s", 30 * 86400)),
        )
        providers["reminder.create"] = reminder
        providers["reminder.list"] = reminder
        providers["reminder.cancel"] = reminder
        services.append(reminder)

    network_enabled = bool(caps.get("weather_enabled", False)) or bool(caps.get("knowledge_search_enabled", False))
    if network_enabled and client is None:
        try:
            import httpx
        except ImportError as exc:
            raise ProviderServiceConfigurationError("Weather/knowledge providers require httpx; install continuity-engine[provider].") from exc
        timeout = max(float(weather_cfg.get("timeout_s", 8.0)), float(knowledge_cfg.get("timeout_s", 10.0)))
        client = httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "ContinuityEngine/0.9 capability-providers"})
        owned_client = True
    if bool(caps.get("weather_enabled", False)):
        weather = OpenMeteoWeatherProvider(client=client, timeout_s=float(weather_cfg.get("timeout_s", 8.0)))
        providers["weather.current"] = weather
        providers["weather.forecast"] = weather
    if bool(caps.get("knowledge_search_enabled", False)):
        knowledge = WikipediaKnowledgeProvider(
            client=client,
            timeout_s=float(knowledge_cfg.get("timeout_s", 10.0)),
            max_results=int(knowledge_cfg.get("max_results", 3)),
            max_extract_chars=int(knowledge_cfg.get("max_extract_chars", 1400)),
            cache_ttl_s=float(knowledge_cfg.get("cache_ttl_s", 900.0)),
        )
        providers["knowledge.search"] = knowledge
    return ProviderBundle(providers, services=tuple(services), client=client, owns_client=owned_client)
