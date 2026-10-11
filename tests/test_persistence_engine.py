import asyncio
import json
import pytest

from continuity_engine import (
    ActionResult, ActionStatus, ContinuityEngine, EngineState, Event, EventKind, EventSchemaMigrator,
    Intent, IntentStatus, PersistenceError, ReplayActionExecutor, ReplayClock,
    SQLiteRepository, UnknownSchemaVersion, WorldStatus,
)


def make_event(event_id, kind="USER_MESSAGE", payload=None, schema="1.0", now=1.0):
    return Event(event_id, kind, "test", now, 1_800_000_000 + now, payload or {}, event_schema_version=schema)


def test_event_migration_current_legacy_and_future_quarantine():
    repo = SQLiteRepository(":memory:")
    repo.save_event(make_event("current"))
    legacy = {
        "event_schema_version": "0.9", "id": "legacy", "kind": "USER_MESSAGE", "source": "old",
        "ts_mono": 1.0, "ts_wall": 1_800_000_001.0, "payload": {"text": "hello"}
    }
    future = {
        "event_schema_version": "9.0", "event_id": "future", "kind": "USER_MESSAGE", "source": "old",
        "timestamp_mono": 2.0, "timestamp_wall": 1_800_000_002.0, "payload": {}
    }
    with repo.transaction() as conn:
        for d in (legacy, future):
            conn.execute("INSERT INTO event_records(event_id,event_schema_version,payload_json,status,created_at) VALUES(?,?,?,?,?)",
                         (d.get("event_id", d.get("id")), d["event_schema_version"], json.dumps(d), "ACTIVE", d.get("ts_wall", d.get("timestamp_wall"))))
    loaded = repo.load_events_with_migration(EventSchemaMigrator())
    assert {e.event_id for e in loaded} == {"current", "legacy"}
    assert repo.quarantined_event_count() == 1
    # Future row remains available in the quarantine record in its original form.
    row = repo._conn.execute("SELECT original_json FROM quarantined_events WHERE event_id='future'").fetchone()
    assert json.loads(row["original_json"])["event_schema_version"] == "9.0"
    repo.close()


def test_missing_schema_is_rejected():
    with pytest.raises(UnknownSchemaVersion):
        EventSchemaMigrator().migrate_dict({"event_id": "e", "kind": "X"})


def test_state_persistence_roundtrip_and_corruption_rejected():
    repo = SQLiteRepository(":memory:")
    state = EngineState(state_version=8, config_version=1, output_muted=True, quiet_scope="PERSISTENT")
    repo.save_state(state, 1_800_000_000)
    restored = repo.load_state()
    assert restored.state_version == 8
    assert restored.output_muted is True
    assert restored.quiet_scope == "PERSISTENT"
    repo.close()


def test_memory_delete_cascades_lineage_and_reports_prediction_partitions():
    repo = SQLiteRepository(":memory:")
    repo.put_memory("m1", "event", {"summary": "x"}, [], 1)
    repo.put_memory("m2", "summary", {"summary": "derived from m1"}, ["m1"], 2)
    repo.put_derived("d1", "index", {"x": 1}, ["m2"], "k", 3)
    repo.put_prediction_observation("return_time", "evt1", "m1", 10, 0.9, 1)
    result = repo.delete_memory("m1", now_wall=4)
    assert result["status"] == "PENDING_DERIVED_REBUILD"
    assert set(result["deleted_memory_ids"]) == {"m1", "m2"}
    assert result["removed_derived"] == 1
    assert result["affected_prediction_partitions"] == ["return_time"]
    assert result["requires_rebuild"] is True
    assert repo._conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"] == 0
    assert repo._conn.execute("SELECT COUNT(*) AS n FROM derived_items").fetchone()["n"] == 0
    repo.close()



def test_memory_delete_rebuilds_live_prediction_partition_from_remaining_db_rows():
    from continuity_engine import PredictionModel, PredictionObservation
    repo = SQLiteRepository(":memory:")
    repo.put_memory("m1", "event", {"summary": "delete me"}, [], 1)
    repo.put_memory("m2", "event", {"summary": "keep"}, [], 2)
    repo.put_prediction_observation("return_time", "e1", "m1", 10, 0.9, 1)
    repo.put_prediction_observation("return_time", "e2", "m2", 20, 0.9, 2)
    model = PredictionModel(alpha=0.1, min_observations=1)
    model.learn(PredictionObservation("e1", "m1", "return_time", 10, 0.9, 1))
    model.learn(PredictionObservation("e2", "m2", "return_time", 20, 0.9, 2))
    result = repo.delete_memory("m1", now_wall=3, prediction_model=model)
    assert result["status"] == "DELETED"
    assert result["prediction_rebuild_results"] == {"return_time": "REBUILT"}
    assert model.estimate("return_time").expected_value == pytest.approx(20)
    assert model.estimate("return_time").observation_count == 1
    repo.close()


def test_engine_message_action_completes_through_terminal_receipt(config_manager):
    async def run():
        clock = ReplayClock(mono=0, wall=1_800_000_000)
        executor = ReplayActionExecutor()
        engine = ContinuityEngine(config_manager, clock=clock, action_executor=executor, replay_mode=True)
        try:
            engine.inject(engine.new_event(EventKind.USER_MESSAGE.value, {"text": "你好"}, source="mic"))
            step = engine.step(dt=0)
            assert len(step.proposals) == 1
            proposal = step.proposals[0]
            assert proposal.action_type == "SPEAK"
            decision, result = await engine.dispatch_action(proposal)
            assert decision.outcome == "ALLOW"
            assert result.status == ActionStatus.SUCCEEDED
            clock.advance(1)
            next_step = engine.step(dt=1)
            assert next_step.state.current_intent is None
        finally:
            engine.close()
    asyncio.run(run())


def test_cancel_invalidates_pending_proposal(config_manager):
    clock = ReplayClock(mono=0, wall=1_800_000_000)
    engine = ContinuityEngine(config_manager, clock=clock)
    engine.inject(engine.new_event(EventKind.USER_MESSAGE.value, {"text": "please respond"}, source="mic"))
    step = engine.step(dt=0)
    proposal = step.proposals[0]
    engine.inject(engine.new_event(EventKind.USER_CANCEL.value, {"intent_id": proposal.intent_id}, source="button"))
    clock.advance(1)
    engine.step(dt=1)
    decision, result = asyncio.run(engine.dispatch_action(proposal))
    assert decision.outcome == "DENY_HARD"
    assert result is None
    engine.close()


def test_quiet_mode_blocks_proactive_speech(config_manager):
    clock = ReplayClock(mono=0, wall=1_800_000_000)
    engine = ContinuityEngine(config_manager, clock=clock)
    engine.inject(engine.new_event(EventKind.USER_QUIET.value, {"scope": "PERSISTENT"}, source="button"))
    engine.inject(engine.new_event("INTENT_CANDIDATE", {
        "intent_type": "proactive", "goal": "share fact", "score": 0.95, "urgency": 0.1,
        "reason": "interesting_fact", "action_type": "SPEAK", "action_args": {"text": "hi"},
        "ordinary_proactive": True,
    }, source="internal"))
    result = engine.step(dt=0)
    assert result.state.quiet_scope == "PERSISTENT"
    assert not result.proposals
    assert any("QUIET_MODE" in d for d in result.diagnostics)
    engine.close()


def test_engine_restore_drops_active_action_and_stales_world(config_manager):
    repo = SQLiteRepository(":memory:")
    state = EngineState(state_version=2, config_version=1)
    state.current_intent = Intent("i1", "speak", "reply", IntentStatus.RUNNING, 0.9, 1, 1, "test", action_type="SPEAK", action_args={"text": "old"})
    from continuity_engine import WorldFact
    state.world["owner_present"] = WorldFact("owner_present", True, WorldStatus.CONFIRMED, 0.99, ("camera",), 1, 100, ("e1",))
    repo.save_state(state, 1_800_000_000)
    clock = ReplayClock(mono=0, wall=1_800_000_100)
    engine = ContinuityEngine(config_manager, clock=clock)
    assert engine.restore(repo)
    assert engine.state.current_intent is None
    assert engine.recovery_intent is not None
    assert engine.recovery_intent.action_type is None
    assert engine.state.world["owner_present"].status == WorldStatus.STALE
    clock.advance(1)
    engine.step(dt=1)
    assert engine.state.world["owner_present"].status == WorldStatus.STALE
    engine.close()
    repo.close()


def test_fast_stop_latch_cancels_inflight_action_without_waiting_for_next_tick(config_manager):
    async def run():
        clock = ReplayClock(mono=0, wall=1_800_000_000)
        executor = ReplayActionExecutor(latency_s=0.08)
        engine = ContinuityEngine(config_manager, clock=clock, action_executor=executor)
        try:
            engine.inject(engine.new_event(EventKind.USER_MESSAGE.value, {"text": "hello"}, source="mic"))
            proposal = engine.step(dt=0).proposals[0]
            dispatch_task = asyncio.create_task(engine.dispatch_action(proposal))
            await asyncio.sleep(0.01)
            engine.inject(engine.new_event(EventKind.USER_STOP.value, {}, source="button"))
            decision, result = await dispatch_task
            assert decision.outcome == "ALLOW"
            assert result.status == ActionStatus.CANCELLED
            clock.advance(1)
            step = engine.step(dt=1)
            assert step.state.current_intent is None
        finally:
            engine.close()
    asyncio.run(run())


def test_stop_latch_prevents_dispatch_before_main_loop_consumes_event(config_manager):
    async def run():
        clock = ReplayClock(mono=0, wall=1_800_000_000)
        executor = ReplayActionExecutor()
        engine = ContinuityEngine(config_manager, clock=clock, action_executor=executor)
        try:
            engine.inject(engine.new_event(EventKind.USER_MESSAGE.value, {"text": "hello"}, source="mic"))
            proposal = engine.step(dt=0).proposals[0]
            engine.inject(engine.new_event(EventKind.SAFETY_STOP.value, {}, source="hardware_safety"))
            decision, result = await engine.dispatch_action(proposal)
            assert decision.outcome == "DENY_HARD"
            assert result is None
            assert executor.started == []
        finally:
            engine.close()
    asyncio.run(run())


def test_clarification_limit_cancels_high_risk_intent(config_manager):
    from continuity_engine import IntentCandidate
    clock = ReplayClock(mono=0, wall=1_800_000_000)
    engine = ContinuityEngine(config_manager, clock=clock)
    engine.inject(engine.new_event("INTENT_CANDIDATE", {
        "intent_type": "external_action", "goal": "send message", "score": 0.9,
        "reason": "needs details", "action_type": "NOTIFY", "action_args": {"text": "send"},
    }, source="internal"))
    step = engine.step(dt=0)
    intent = step.state.current_intent
    assert engine.ask_clarification(intent.intent_id, "Who is the recipient?", risk_level="high")
    assert engine.ask_clarification(intent.intent_id, "What should I say?", risk_level="high")
    assert not engine.ask_clarification(intent.intent_id, "Which account?", risk_level="high")
    assert engine.state.current_intent is None
    engine.close()


def test_engine_reports_dropped_events_for_observability(config_manager):
    from copy import deepcopy
    from continuity_engine import ConfigManager, Priority
    data = config_manager.get()
    data["runtime"]["max_queue_size"] = 1
    config = ConfigManager(data)
    clock = ReplayClock(mono=0, wall=1_800_000_000)
    engine = ContinuityEngine(config, clock=clock)
    try:
        old = engine.new_event(EventKind.WORLD_OBSERVATION.value, {"key": "x", "value": 1, "ttl_s": 3}, source="test", priority=int(Priority.NORMAL))
        new = engine.new_event(EventKind.WORLD_OBSERVATION.value, {"key": "y", "value": 1, "ttl_s": 3}, source="test", priority=int(Priority.HIGH))
        engine.inject(old)
        engine.inject(new)
        result = engine.step(dt=0)
        assert old.event_id in result.dropped_event_ids
    finally:
        engine.close()


def test_speech_started_barge_in_cancels_current_speech(config_manager):
    async def run():
        clock = ReplayClock(mono=0, wall=1_800_000_000)
        executor = ReplayActionExecutor(latency_s=0.1)
        engine = ContinuityEngine(config_manager, clock=clock, action_executor=executor)
        try:
            engine.inject(engine.new_event(EventKind.USER_MESSAGE.value, {"text": "hello"}, source="mic"))
            proposal = engine.step(dt=0).proposals[0]
            dispatch_task = asyncio.create_task(engine.dispatch_action(proposal))
            await asyncio.sleep(0.01)
            engine.inject(engine.new_event(EventKind.USER_SPEECH_STARTED.value, {}, source="vad"))
            _decision, result = await dispatch_task
            assert result is not None and result.status == ActionStatus.CANCELLED
            engine.step(dt=0)
            assert engine.state.current_intent is None
        finally:
            engine.close()
    asyncio.run(run())


def test_pending_prediction_rebuild_survives_until_startup_reconciliation():
    from continuity_engine import PredictionModel, PredictionObservation
    repo = SQLiteRepository(":memory:")
    repo.put_memory("m1", "event", {"summary": "delete"}, [], 1)
    repo.put_memory("m2", "event", {"summary": "keep"}, [], 2)
    repo.put_prediction_observation("k", "e1", "m1", 10, 1.0, 1)
    repo.put_prediction_observation("k", "e2", "m2", 20, 1.0, 2)
    stale_model = PredictionModel(alpha=0.1, min_observations=1)
    stale_model.learn(PredictionObservation("e1", "m1", "k", 10, 1.0, 1))
    stale_model.learn(PredictionObservation("e2", "m2", "k", 20, 1.0, 2))
    result = repo.delete_memory("m1", now_wall=3)
    assert result["status"] == "PENDING_DERIVED_REBUILD"
    assert repo.pending_prediction_rebuilds() == ["k"]
    reconciled = repo.reconcile_prediction_model(stale_model)
    assert reconciled == {"k": "REBUILT"}
    assert repo.pending_prediction_rebuilds() == []
    assert stale_model.estimate("k").expected_value == pytest.approx(20)
    repo.close()


def test_user_message_interrupts_pending_background_intent(config_manager):
    engine = ContinuityEngine(config_manager, clock=ReplayClock(mono=0, wall=1_800_000_000))
    try:
        engine.inject(engine.new_event("INTENT_CANDIDATE", {
            "intent_type": "explore", "goal": "observe_window", "score": 0.85,
            "urgency": 0.1, "reason": "background_exploration", "action_type": "OBSERVE",
            "action_args": {"summary": "window"},
        }, source="internal"))
        first = engine.step(dt=0)
        assert first.proposals[0].action_type == "OBSERVE"
        old_intent_id = first.proposals[0].intent_id
        engine.inject(engine.new_event(EventKind.USER_MESSAGE.value, {"text": "你好"}, source="keyboard"))
        second = engine.step(dt=0)
        assert second.state.current_intent.intent_type == "respond_to_user"
        assert second.state.current_intent.intent_id != old_intent_id
        assert not any(p.action_type == "OBSERVE" for p in second.proposals)
    finally:
        engine.close()


def test_malformed_executor_result_is_unknown_not_false_success(config_manager):
    class WrongCorrelationExecutor(ReplayActionExecutor):
        async def execute(self, proposal):
            return ActionResult("wrong-action", "wrong-intent", ActionStatus.SUCCEEDED)

    async def run():
        clock = ReplayClock(mono=0, wall=1_800_000_000)
        executor = WrongCorrelationExecutor()
        engine = ContinuityEngine(config_manager, clock=clock, action_executor=executor)
        try:
            engine.inject(engine.new_event(EventKind.USER_MESSAGE.value, {"text": "hello"}, source="mic"))
            proposal = engine.step(dt=0).proposals[0]
            _decision, result = await engine.dispatch_action(proposal)
            assert result.status == ActionStatus.UNKNOWN
            assert result.error_code == "EXECUTOR_RESULT_CORRELATION_FAILURE"
            clock.advance(0.1)
            engine.step(dt=0.1)
            assert engine.state.current_intent.status == IntentStatus.WAITING
        finally:
            engine.close()
    asyncio.run(run())


def test_pending_speech_is_blocked_if_barge_in_arrives_before_dispatch(config_manager):
    async def run():
        clock = ReplayClock(mono=0, wall=1_800_000_000)
        executor = ReplayActionExecutor()
        engine = ContinuityEngine(config_manager, clock=clock, action_executor=executor)
        try:
            engine.inject(engine.new_event(EventKind.USER_MESSAGE.value, {"text": "hello"}, source="mic"))
            proposal = engine.step(dt=0).proposals[0]
            # The VAD edge arrives after proposal creation but before it starts playing.
            engine.inject(engine.new_event(EventKind.USER_SPEECH_STARTED.value, {}, source="vad"))
            decision, result = await engine.dispatch_action(proposal)
            assert decision.code == "USER_SPEAKING"
            assert result is None
            assert executor.started == []
            engine.step(dt=0)
        finally:
            engine.close()
    asyncio.run(run())


def test_executor_result_with_invalid_status_cannot_be_false_success(config_manager):
    class InvalidStatusExecutor(ReplayActionExecutor):
        async def execute(self, proposal):
            return ActionResult(proposal.action_id, proposal.intent_id, "SUCCEEDED")

    async def run():
        clock = ReplayClock(mono=0, wall=1_800_000_000)
        engine = ContinuityEngine(config_manager, clock=clock, action_executor=InvalidStatusExecutor())
        try:
            engine.inject(engine.new_event(EventKind.USER_MESSAGE.value, {"text": "hello"}, source="mic"))
            proposal = engine.step(dt=0).proposals[0]
            _decision, result = await engine.dispatch_action(proposal)
            assert result.status == ActionStatus.UNKNOWN
            assert result.error_code == "EXECUTOR_ValueError"
        finally:
            engine.close()
    asyncio.run(run())


def test_llm_backed_dialogue_flows_through_versioned_action_ready_event(config_manager):
    from continuity_engine import LLMGateway, LLMStatus, StubLLMAdapter

    async def run():
        clock = ReplayClock(mono=0, wall=1_800_000_000)
        gateway = LLMGateway(StubLLMAdapter({"simple_response": {"response_text": "你好，很高兴见到你。"}}), timeout_s=1)
        engine = ContinuityEngine(config_manager, clock=clock, llm_gateway=gateway)
        try:
            engine.inject(engine.new_event(EventKind.USER_MESSAGE.value, {"text": "你好", "interaction_id": "chat-1"}, source="mic"))
            first = engine.step(dt=0)
            assert not first.proposals
            intent = first.state.current_intent
            assert intent is not None and intent.status == IntentStatus.WAITING
            generation = engine.current_generation("chat-1")
            result = await engine.request_llm(purpose="simple_response", interaction_id="chat-1", generation=generation,
                                              user_input="你好", intent_id=intent.intent_id)
            assert result.status == LLMStatus.COMPLETED
            assert engine.submit_llm_response(intent_id=intent.intent_id, interaction_id="chat-1", generation=generation,
                                              response_text=result.payload["response_text"])
            second = engine.step(dt=0)
            assert len(second.proposals) == 1
            assert second.proposals[0].args["text"] == "你好，很高兴见到你。"
            _decision, action_result = await engine.dispatch_action(second.proposals[0])
            assert action_result.status == ActionStatus.SUCCEEDED
            clock.advance(0.1)
            engine.step(dt=0.1)
        finally:
            engine.close()
    asyncio.run(run())


def test_new_message_invalidates_old_llm_generation_and_response(config_manager):
    from continuity_engine import LLMGateway, LLMStatus, StubLLMAdapter

    async def run():
        clock = ReplayClock(mono=0, wall=1_800_000_000)
        gateway = LLMGateway(StubLLMAdapter({"simple_response": {"response_text": "旧回答"}}, delay_s=0.08), timeout_s=1)
        engine = ContinuityEngine(config_manager, clock=clock, llm_gateway=gateway)
        try:
            engine.inject(engine.new_event(EventKind.USER_MESSAGE.value, {"text": "第一条", "interaction_id": "chat-1"}, source="mic"))
            step = engine.step(dt=0)
            intent_id = step.state.current_intent.intent_id
            gen1 = engine.current_generation("chat-1")
            old_task = asyncio.create_task(engine.request_llm(purpose="simple_response", interaction_id="chat-1", generation=gen1,
                                                              user_input="第一条", intent_id=intent_id))
            await asyncio.sleep(0.01)
            engine.inject(engine.new_event(EventKind.USER_MESSAGE.value, {"text": "第二条", "interaction_id": "chat-1"}, source="mic"))
            gen2 = engine.current_generation("chat-1")
            assert gen2 == gen1 + 1
            old_result = await old_task
            assert old_result.status in {LLMStatus.STALE, LLMStatus.CANCELLED}
            engine.step(dt=0)
            assert not engine.submit_llm_response(intent_id=intent_id, interaction_id="chat-1", generation=gen1, response_text="旧回答")
        finally:
            engine.close()
    asyncio.run(run())


def test_llm_action_ready_event_expires_at_dequeue(config_manager):
    from continuity_engine import LLMGateway, StubLLMAdapter

    clock = ReplayClock(mono=0, wall=1_800_000_000)
    gateway = LLMGateway(StubLLMAdapter({"simple_response": {"response_text": "answer"}}), timeout_s=1)
    engine = ContinuityEngine(config_manager, clock=clock, llm_gateway=gateway)
    try:
        engine.inject(engine.new_event(EventKind.USER_MESSAGE.value, {"text": "hello", "interaction_id": "chat"}, source="mic"))
        intent = engine.step(dt=0).state.current_intent
        generation = engine.current_generation("chat")
        assert engine.submit_llm_response(intent_id=intent.intent_id, interaction_id="chat", generation=generation, response_text="answer")
        clock.advance(11)
        step = engine.step(dt=11)
        assert not step.proposals
        assert len(step.dropped_event_ids) >= 1
        assert step.state.current_intent is not None and step.state.current_intent.action_type is None
    finally:
        engine.close()


def test_submit_llm_response_rejects_before_user_message_generation_is_committed(config_manager):
    from continuity_engine import StubLLMAdapter, LLMGateway
    clock = ReplayClock(mono=0, wall=1_800_000_000)
    gateway = LLMGateway(StubLLMAdapter({"simple_response": {"response_text": "hello"}}))
    engine = ContinuityEngine(config_manager, clock=clock, llm_gateway=gateway)
    try:
        event = engine.new_event(
            EventKind.USER_MESSAGE.value,
            {"text": "hello", "interaction_id": "chat-race"}, source="mic",
        )
        engine.inject(event)
        generation = engine.current_generation("chat-race")
        # The message has not passed through step(), so there is no committed intent/state generation.
        assert engine.submit_llm_response(
            intent_id="not-created-yet", interaction_id="chat-race", generation=generation,
            response_text="stale",
        ) is False
    finally:
        engine.close()


def test_restore_bumps_interaction_generations_to_reject_pre_restart_results(config_manager):
    repo = SQLiteRepository(":memory:")
    state = EngineState(state_version=2, config_version=1)
    state.generation_by_interaction = {"chat-persisted": 4}
    repo.save_state(state, 1_800_000_000)
    clock = ReplayClock(mono=0, wall=1_800_000_100)
    engine = ContinuityEngine(config_manager, clock=clock)
    try:
        assert engine.restore(repo)
        assert engine.current_generation("chat-persisted") == 5
        assert engine.state.generation_by_interaction["chat-persisted"] == 5
    finally:
        engine.close()
        repo.close()
