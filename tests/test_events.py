import pytest

from continuity_engine import Event, EventKind, EventRouter, Priority, ControlLaneFull


def make_event(event_id, kind="TELEMETRY", *, payload=None, priority=Priority.LOW, expires=None, now=0.0):
    return Event(event_id, kind, "test", now, 1_800_000_000 + now, payload or {},
                 priority=int(priority), expires_at_mono=expires)


def test_duplicate_event_id_is_ignored():
    router = EventRouter(max_queue_size=5)
    event = make_event("e1")
    assert router.enqueue(event, 0).accepted
    receipt = router.enqueue(event, 0)
    assert not receipt.accepted
    assert receipt.disposition == "duplicate_event_id"
    assert [e.event_id for e in router.drain()] == ["e1"]


def test_control_lane_is_drained_before_normal_queue():
    router = EventRouter(max_queue_size=1)
    router.enqueue(make_event("normal"), 0)
    router.enqueue(make_event("stop", EventKind.USER_STOP.value, priority=Priority.CRITICAL), 0)
    assert [e.event_id for e in router.drain()] == ["stop", "normal"]


def test_control_overflow_latches_and_raises_instead_of_silent_drop():
    router = EventRouter(max_queue_size=2, reserved_control_capacity=1)
    router.enqueue(make_event("stop1", EventKind.USER_STOP.value, priority=Priority.CRITICAL), 0)
    with pytest.raises(ControlLaneFull):
        router.enqueue(make_event("stop2", EventKind.SAFETY_STOP.value, priority=Priority.CRITICAL), 0)
    assert router.latches.safety_stop is True
    assert router.latches.degraded is True


def test_expired_incoming_event_is_rejected():
    router = EventRouter(max_queue_size=4)
    r = router.enqueue(make_event("expired", expires=2), 2)
    assert not r.accepted
    assert r.disposition == "expired_incoming"


def test_replaceable_state_coalesces_by_key():
    router = EventRouter(max_queue_size=4)
    router.enqueue(make_event("p1", "PERSON_POSITION", payload={"coalesce_key": "owner", "x": 1}), 0)
    receipt = router.enqueue(make_event("p2", "PERSON_POSITION", payload={"coalesce_key": "owner", "x": 2}), 1)
    assert "p1" in receipt.dropped_event_ids
    queued = router.drain()
    assert [e.event_id for e in queued] == ["p2"]
    assert router.dropped_summary()["coalesced_replaceable_state"] == 1


def test_low_priority_event_is_discarded_when_queue_full():
    router = EventRouter(max_queue_size=1)
    router.enqueue(make_event("old", priority=Priority.LOW), 0)
    receipt = router.enqueue(make_event("new", priority=Priority.HIGH), 1)
    assert receipt.accepted
    assert "old" in receipt.dropped_event_ids
    assert [e.event_id for e in router.drain()] == ["new"]


def test_user_message_backpressures_instead_of_silent_drop_when_only_high_priority_items_remain():
    router = EventRouter(max_queue_size=1)
    router.enqueue(make_event("high", "URGENT_TELEMETRY", priority=Priority.CRITICAL), 0)
    with pytest.raises(BufferError):
        router.enqueue(make_event("user-msg", EventKind.USER_MESSAGE.value, payload={"text": "hi"}, priority=Priority.NORMAL), 1)
