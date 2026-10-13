"""End-to-end tests for LLMTaskPlanner using deterministic fixture-only adapters."""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from continuity_engine import (
    ActionStatus, ConfigManager, ContinuityEngine, EventKind, LLMGateway,
    LLMTaskPlanner, PlanningStatus, ReplayActionExecutor, ReplayClock,
    ReplayLLMAdapter, TaskStatus,
)


def make_engine(config, fixture, *, result_by_action_type=None, delay_s=0.0):
    clock = ReplayClock(mono=10.0, wall=1_800_000_000.0)
    executor = ReplayActionExecutor(result_by_action_type=result_by_action_type or {})
    adapter = ReplayLLMAdapter({"task_plan": fixture}, delay_s=delay_s)
    budgets = config.get()["llm"]["context_token_budgets"]
    gateway = LLMGateway(adapter, timeout_s=2.0,
                         max_concurrent=int(config.get()["llm"]["max_concurrent_requests"]),
                         context_token_budgets=budgets,
                         replay_mode=True, action_executor=executor)
    engine = ContinuityEngine(config, clock=clock, action_executor=executor,
                              llm_gateway=gateway, replay_mode=True)
    return engine, adapter, executor, clock


@pytest.mark.asyncio
async def test_plan_goal_generates_filtered_candidate_and_enqueues_task(config_manager):
    fixture = {"steps": [
        {"description": "Wait briefly", "action_type": "WAIT", "args": {"duration_s": 1.0}},
        {"description": "Show success light", "action_type": "LIGHT", "args": {"pattern": "success", "duration_s": 2.0}},
    ]}
    engine, adapter, executor, clock = make_engine(config_manager, fixture)
    planner = LLMTaskPlanner()
    try:
        result = await planner.plan_goal(engine, "Wait then show success", request_id="plan-001")
        assert result.status == PlanningStatus.QUEUED
        assert result.task_id and result.event_id
        assert len(adapter.calls) == 1
        view = __import__("json").loads(adapter.calls[0].context_json)
        assert view["purpose"] == "task_plan"
        assert set(view) == {"purpose", "goal", "confirmed_world_facts", "constraints",
                             "allowed_action_types", "allowed_world_fact_keys", "action_argument_schemas",
                             "validation_feedback", "task_progress", "failed_plan", "max_steps"}
        assert view["action_argument_schemas"]["LIGHT"]["required"]["pattern"]["enum"] == [
            "idle", "attention", "success", "warning"
        ]
        assert "room_activity" in view["allowed_world_fact_keys"]
        assert view["validation_feedback"] == ""
        assert "raw" not in str(view).lower() or "raw" not in view

        accepted = engine.step(dt=0.0)
        assert any(x.startswith("task_plan:PLAN_ACCEPTED:") for x in accepted.diagnostics)
        assert engine.task_manager.active_plan is not None
        assert engine.task_manager.active_plan.task_id == result.task_id
        assert accepted.proposals and accepted.proposals[0].action_type == "WAIT"

        # Same stable request ID is idempotent: no duplicate LLM call or task event.
        repeated = await planner.plan_goal(engine, "Wait then show success", request_id="plan-001")
        assert repeated.status == PlanningStatus.ALREADY_QUEUED
        assert len(adapter.calls) == 1
    finally:
        engine.close()


@pytest.mark.asyncio
async def test_task_plan_rejects_unallowlisted_action_before_queue(config_manager):
    fixture = {"steps": [
        {"description": "Run shell", "action_type": "SHELL", "args": {"command": "id"}},
    ]}
    engine, adapter, _executor, _clock = make_engine(config_manager, fixture)
    try:
        result = await LLMTaskPlanner().plan_goal(engine, "Do something", request_id="plan-shell")
        assert result.status == PlanningStatus.REJECTED
        assert result.error_code == "DISALLOWED_TASK_ACTION"
        assert not len(engine.router) > 0
    finally:
        engine.close()


@pytest.mark.asyncio
async def test_task_plan_preflight_rejects_observation_key_not_allowlisted(config_manager):
    fixture = {"steps": [
        {"description": "Observe secrets", "action_type": "OBSERVE", "args": {"key": "private_notes"}},
    ]}
    engine, _adapter, _executor, _clock = make_engine(config_manager, fixture)
    try:
        result = await LLMTaskPlanner().plan_goal(engine, "Inspect something", request_id="plan-bad-key")
        assert result.status == PlanningStatus.REJECTED
        assert result.error_code == "INVALID_TASK_ACTION_ARGS"
        assert result.details["attempts"] == 2
        assert len(_adapter.calls) == 2
        second_view = __import__("json").loads(_adapter.calls[1].context_json)
        assert "room_activity" in second_view["allowed_world_fact_keys"]
        assert "room_human_activity" not in second_view["allowed_world_fact_keys"]
        assert "INVALID_TASK_ACTION_ARGS" in second_view["validation_feedback"]
        assert engine.task_manager.active_plan is None
        assert not len(engine.router) > 0
    finally:
        engine.close()


@pytest.mark.asyncio
async def test_invalid_plan_is_repaired_once_using_explicit_action_schema(config_manager):
    adapter = ReplayLLMAdapter(
        {"task_plan": {"steps": [
            {"description": "Set normal green indicator", "action_type": "LIGHT", "args": {"pattern": "success"}}
        ]}},
        response_sequences={"task_plan": [
            {"steps": [
                {"description": "Set green indicator", "action_type": "LIGHT", "args": {"pattern": "green"}}
            ]},
            {"steps": [
                {"description": "Set normal green indicator", "action_type": "LIGHT", "args": {"pattern": "success"}}
            ]},
        ]},
    )
    clock = ReplayClock(mono=10.0, wall=1_800_000_000.0)
    executor = ReplayActionExecutor()
    gateway = LLMGateway(adapter, timeout_s=2.0,
                         context_token_budgets=config_manager.get()["llm"]["context_token_budgets"],
                         replay_mode=True, action_executor=executor)
    engine = ContinuityEngine(config_manager, clock=clock, action_executor=executor,
                              llm_gateway=gateway, replay_mode=True)
    try:
        result = await LLMTaskPlanner().plan_goal(
            engine, "Show the normal green indicator", request_id="plan-repair-light",
        )
        assert result.status == PlanningStatus.QUEUED
        assert result.details["attempts"] == 2
        assert result.details["repaired"] is True
        assert len(adapter.calls) == 2
        first_view = __import__("json").loads(adapter.calls[0].context_json)
        second_view = __import__("json").loads(adapter.calls[1].context_json)
        assert first_view["validation_feedback"] == ""
        assert "INVALID_TASK_ACTION_ARGS" in second_view["validation_feedback"]
        assert "success" in second_view["action_argument_schemas"]["LIGHT"]["required"]["pattern"]["enum"]
        assert engine.task_manager.active_plan is None  # plan is queued, core has not accepted it yet
        consumed = engine.step(dt=0.0)
        assert any(row.startswith("task_plan:PLAN_ACCEPTED:") for row in consumed.diagnostics)
        assert engine.task_manager.active_plan is not None
    finally:
        engine.close()


@pytest.mark.asyncio
async def test_empty_world_allowlist_disables_observe_in_prompt_and_validator(config_manager):
    data = config_manager.get()
    data["llm"]["world_fact_allowlist"] = []
    data["tasks"]["observe_world_key_allowlist"] = []
    restricted_config = ConfigManager(data)
    fixture = {"steps": [
        {"description": "Observe room", "action_type": "OBSERVE", "args": {"key": "room_activity"}},
    ]}
    engine, adapter, _executor, _clock = make_engine(restricted_config, fixture)
    try:
        result = await LLMTaskPlanner().plan_goal(engine, "Observe the room", request_id="plan-no-world-keys")
        assert result.status == PlanningStatus.REJECTED
        assert result.error_code == "DISALLOWED_TASK_ACTION"
        assert len(adapter.calls) == 2  # one repair attempt, bounded by config
        view = __import__("json").loads(adapter.calls[0].context_json)
        assert view["allowed_world_fact_keys"] == []
        assert "OBSERVE" not in view["allowed_action_types"]
        assert "OBSERVE" not in view["action_argument_schemas"]
        assert engine.task_manager.active_plan is None
    finally:
        engine.close()


@pytest.mark.asyncio
async def test_replan_from_definite_failure_generates_replacement_and_completes(config_manager):
    first_fixture = {"steps": [
        {"description": "Greet", "action_type": "SPEAK", "args": {"text": "你好"}},
    ]}
    engine, adapter, executor, clock = make_engine(
        config_manager, first_fixture, result_by_action_type={"SPEAK": ActionStatus.FAILED}
    )
    planner = LLMTaskPlanner()
    try:
        initial = await planner.plan_goal(engine, "Greet the user", request_id="plan-replan-source")
        assert initial.status == PlanningStatus.QUEUED
        started = engine.step(dt=0.0)
        assert started.proposals and started.proposals[0].action_type == "SPEAK"
        await engine.dispatch_action(started.proposals[0])
        clock.advance(0.1)
        failed = engine.step(dt=0.1)
        request = failed.replan_request
        assert request is not None
        assert engine.task_manager.active_plan.status == TaskStatus.FAILED

        adapter.fixtures["task_plan"] = {"steps": [
            {"description": "Observe room activity instead", "action_type": "OBSERVE", "args": {"key": "room_activity"}},
        ]}
        replacement = await planner.replan(engine, request)
        assert replacement.status == PlanningStatus.QUEUED
        assert replacement.task_id != initial.task_id
        assert replacement.details["replaces_task_id"] == initial.task_id

        replanned = engine.step(dt=0.0)
        assert engine.task_manager.active_plan.task_id == replacement.task_id
        assert replanned.proposals and replanned.proposals[0].action_type == "OBSERVE"
        await engine.dispatch_action(replanned.proposals[0])
        clock.advance(0.1)
        done = engine.step(dt=0.1)
        assert engine.task_manager.active_plan.status == TaskStatus.SUCCEEDED
        assert done.replan_request is None
    finally:
        engine.close()


@pytest.mark.asyncio
async def test_replan_refuses_when_failed_task_changed(config_manager):
    fixture = {"steps": [{"description": "Wait", "action_type": "WAIT", "args": {"duration_s": 1}}]}
    engine, _adapter, _executor, _clock = make_engine(config_manager, fixture)
    try:
        result = await LLMTaskPlanner().replan(engine, {
            "request_id": "replan:missing", "failed_task_id": "not-current", "goal": "retry",
            "completed_steps": [], "remaining_steps": [], "constraints": [],
        })
        assert result.status == PlanningStatus.STALE
        assert result.error_code == "FAILED_TASK_NO_LONGER_CURRENT"
    finally:
        engine.close()


@pytest.mark.asyncio
async def test_cancel_during_planning_prevents_task_event(config_manager):
    fixture = {"steps": [{"description": "Wait", "action_type": "WAIT", "args": {"duration_s": 1}}]}
    engine, _adapter, _executor, _clock = make_engine(config_manager, fixture, delay_s=0.5)
    planner = LLMTaskPlanner()
    try:
        task = asyncio.create_task(planner.plan_goal(engine, "wait", request_id="plan-cancel"))
        for _ in range(50):
            await asyncio.sleep(0.005)
            # cancel() only succeeds while gateway planning is inflight
            if planner.cancel(engine, "plan-cancel"):
                break
        result = await task
        assert result.status == PlanningStatus.CANCELLED
        assert not len(engine.router) > 0
        assert engine.task_manager.active_plan is None
    finally:
        engine.close()


@pytest.mark.asyncio
async def test_plan_rejected_during_safety_stop(config_manager):
    fixture = {"steps": [{"description": "Say hello", "action_type": "SPEAK", "args": {"text": "hello"}}]}
    engine, _adapter, _executor, _clock = make_engine(config_manager, fixture)
    try:
        engine.state.safety_stopped = True
        result = await LLMTaskPlanner().plan_goal(engine, "say hello", request_id="plan-stopped")
        assert result.status == PlanningStatus.REJECTED
        assert result.error_code == "SAFETY_STOP_ACTIVE"
    finally:
        engine.close()


@pytest.mark.asyncio
async def test_replan_request_is_idempotent_even_after_replacement_is_consumed(config_manager):
    first_fixture = {"steps": [{"description": "say hello", "action_type": "SPEAK", "args": {"text": "hello"}}]}
    engine, adapter, _executor, clock = make_engine(
        config_manager, first_fixture, result_by_action_type={"SPEAK": ActionStatus.FAILED}
    )
    planner = LLMTaskPlanner()
    try:
        first = await planner.plan_goal(engine, "say hello", request_id="idempotent-plan")
        engine.step(dt=0.0)
        proposal = engine.policy.current
        assert proposal is not None
        # One current SPEAK proposal is dispatched and fails deterministically.
        pending = next(p for p in engine._pending_proposals.values() if p.action_type == "SPEAK")
        await engine.dispatch_action(pending)
        clock.advance(0.1)
        failed = engine.step(dt=0.1)
        request = failed.replan_request
        assert request is not None
        adapter.fixtures["task_plan"] = {"steps": [
            {"description": "wait", "action_type": "WAIT", "args": {"duration_s": 1.0}},
        ]}
        replacement = await planner.replan(engine, request)
        assert replacement.status == PlanningStatus.QUEUED
        engine.step(dt=0.0)
        duplicate = await planner.replan(engine, request)
        assert duplicate.status == PlanningStatus.ALREADY_QUEUED
    finally:
        engine.close()


@pytest.mark.asyncio
async def test_plan_result_becomes_stale_if_another_task_starts_during_llm_call(config_manager):
    fixture = {"steps": [{"description": "model wait", "action_type": "WAIT", "args": {"duration_s": 1.0}}]}
    engine, _adapter, _executor, _clock = make_engine(config_manager, fixture, delay_s=0.08)
    planner = LLMTaskPlanner()
    try:
        pending = asyncio.create_task(planner.plan_goal(engine, "wait for model", request_id="stale-plan"))
        await asyncio.sleep(0.01)
        engine.inject(engine.new_event(EventKind.TASK_PLAN_SUBMITTED.value, {
            "task_id": "external-task", "goal": "host task wins", "priority": 0.88,
            "steps": [{"step_id": "host-wait", "action_type": "WAIT", "args": {"duration_s": 1.0}}],
        }, source="trusted_host", priority=90))
        engine.step(dt=0.0)
        result = await pending
        assert result.status == PlanningStatus.STALE
        assert result.error_code == "ACTIVE_TASK_STARTED_DURING_PLANNING"
        assert engine.task_manager.active_plan.task_id == "external-task"
    finally:
        engine.close()


@pytest.mark.asyncio
async def test_cancel_after_plan_was_queued_returns_false(config_manager):
    fixture = {"steps": [{"description": "wait", "action_type": "WAIT", "args": {"duration_s": 1.0}}]}
    engine, _adapter, _executor, _clock = make_engine(config_manager, fixture)
    planner = LLMTaskPlanner()
    try:
        result = await planner.plan_goal(engine, "wait", request_id="queued-cancel")
        assert result.status == PlanningStatus.QUEUED
        assert planner.cancel(engine, "queued-cancel") is False
    finally:
        engine.close()
