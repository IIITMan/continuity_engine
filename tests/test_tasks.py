import asyncio

import pytest

from continuity_engine import (
    ActionStatus, ContinuityEngine, EventKind, ReplayActionExecutor, ReplayClock,
    SQLiteRepository, TaskManager, TaskPlanError, TaskStatus,
)


def simple_plan(task_id="task-1"):
    return {
        "task_id": task_id,
        "goal": "greet and observe",
        "priority": 0.88,
        "steps": [
            {"step_id": "say", "description": "Say hello", "action_type": "SPEAK", "args": {"text": "你好"}},
            {"step_id": "observe", "description": "Observe room activity", "action_type": "OBSERVE", "args": {"key": "room_activity", "reason": "refresh context"}},
        ],
    }


def test_task_manager_validates_and_runs_steps_sequentially():
    manager = TaskManager(allowed_world_keys=("room_activity",))
    accepted = manager.submit(simple_plan(), 0.0)
    assert accepted.accepted and accepted.code == "PLAN_ACCEPTED"
    first = manager.candidate(1.0)
    assert first is not None and first.action_type == "SPEAK"
    assert manager.bind_intent("intent-1", first.goal)
    assert manager.bind_action("intent-1", "action-1", 1.0)
    receipt = manager.on_action_result(action_id="action-1", intent_id="intent-1", status="SUCCEEDED", now_mono=2.0)
    assert receipt.code == "STEP_SUCCEEDED"
    second = manager.candidate(2.0)
    assert second is not None and second.action_type == "OBSERVE"
    assert second.goal == "task:task-1:step:observe"
    assert manager.bind_intent("intent-2", second.goal)
    assert manager.bind_action("intent-2", "action-2", 2.0)
    manager.on_action_result(action_id="action-2", intent_id="intent-2", status="SUCCEEDED", now_mono=3.0)
    assert manager.active_plan.status == TaskStatus.SUCCEEDED
    assert manager.candidate(4.0) is None


def test_task_plan_rejects_arbitrary_actions_and_unbounded_payloads():
    manager = TaskManager(allowed_world_keys=("room_activity",))
    plan = {"goal": "do something", "steps": [{"action_type": "SHELL", "args": {"command": "rm -rf"}}]}
    with pytest.raises(TaskPlanError):
        manager.submit(plan, 0.0)
    plan = {"goal": "bad observe", "steps": [{"action_type": "OBSERVE", "args": {"key": "private_notes"}}]}
    with pytest.raises(TaskPlanError):
        manager.submit(plan, 0.0)
    plan = {"goal": "unsafe retry", "steps": [{"action_type": "SPEAK", "args": {"text": "hello"}, "max_retries": 1}]}
    with pytest.raises(TaskPlanError):
        manager.submit(plan, 0.0)


def test_task_plan_rejects_priority_below_policy_threshold():
    manager = TaskManager(minimum_priority=0.7)
    plan = {"goal": "low priority", "priority": 0.6, "steps": [{"action_type": "WAIT", "args": {"duration_s": 1}}]}
    with pytest.raises(TaskPlanError):
        manager.submit(plan, 0.0)


def test_retry_only_when_executor_marks_safe_and_retry_budget_remains():
    manager = TaskManager(allowed_world_keys=("room_activity",))
    plan = {"task_id": "retry-task", "goal": "observe", "steps": [
        {"step_id": "observe", "action_type": "OBSERVE", "args": {"key": "room_activity"}, "max_retries": 1}
    ]}
    manager.submit(plan, 0.0)
    candidate = manager.candidate(0.1)
    assert candidate and manager.bind_intent("i1", candidate.goal)
    assert manager.bind_action("i1", "a1", 0.1)
    result = manager.on_action_result(action_id="a1", intent_id="i1", status="FAILED", now_mono=1.0,
                                      error_code="TRANSIENT", details={"retryable": True})
    assert result.code == "SAFE_RETRY_SCHEDULED"
    candidate = manager.candidate(1.1)
    assert candidate and manager.bind_intent("i2", candidate.goal)
    assert manager.bind_action("i2", "a2", 1.1)
    result = manager.on_action_result(action_id="a2", intent_id="i2", status="FAILED", now_mono=2.0,
                                      error_code="TRANSIENT", details={"retryable": True})
    assert result.code == "TASK_FAILED"
    assert manager.active_plan.status == TaskStatus.FAILED


def test_unknown_action_result_requires_explicit_reconciliation():
    manager = TaskManager()
    manager.submit({"task_id": "unknown-task", "goal": "speak", "steps": [
        {"step_id": "speak", "action_type": "SPEAK", "args": {"text": "hello"}}
    ]}, 0)
    candidate = manager.candidate(0.1)
    assert candidate and manager.bind_intent("i1", candidate.goal)
    assert manager.bind_action("i1", "a1", 0.1)
    result = manager.on_action_result(action_id="a1", intent_id="i1", status="UNKNOWN", now_mono=1.0,
                                      error_code="NO_CONFIRMATION")
    assert result.code == "TASK_NEEDS_RECONCILIATION"
    assert manager.active_plan.status == TaskStatus.NEEDS_RECONCILIATION
    assert manager.candidate(2.0) is None
    assert manager.resolve_step("unknown-task", "speak", "retry", 2.0) .code == "RETRY_REQUIRES_NONCOMPLETION_CONFIRMATION"
    resolved = manager.resolve_step("unknown-task", "speak", "succeeded", 2.1)
    assert resolved.accepted
    assert manager.active_plan.status == TaskStatus.SUCCEEDED


def test_task_resume_requires_explicit_resolution_after_cancelled_step():
    manager = TaskManager()
    manager.submit({"task_id": "pause-task", "goal": "say hello", "steps": [
        {"step_id": "speak", "action_type": "SPEAK", "args": {"text": "hello"}}
    ]}, 0)
    candidate = manager.candidate(0.1)
    assert candidate and manager.bind_intent("i1", candidate.goal)
    assert manager.bind_action("i1", "a1", 0.1)
    manager.on_action_result(action_id="a1", intent_id="i1", status="CANCELLED", now_mono=1.0)
    assert manager.active_plan.status == TaskStatus.PAUSED
    assert manager.resume("pause-task").code == "STEP_REQUIRES_RECONCILIATION"
    resolved = manager.resolve_step("pause-task", "speak", "retry", 2.0, confirmed_not_completed=True)
    assert resolved.accepted
    assert manager.resume("pause-task").accepted


def test_running_task_snapshot_recovers_as_needs_reconciliation():
    manager = TaskManager()
    manager.submit({"task_id": "recover-task", "goal": "speak", "steps": [
        {"step_id": "speak", "action_type": "SPEAK", "args": {"text": "hello"}}
    ]}, 0)
    candidate = manager.candidate(0.1)
    assert candidate and manager.bind_intent("i1", candidate.goal)
    assert manager.bind_action("i1", "a1", 0.1)
    restored = TaskManager(snapshot=manager.snapshot())
    assert restored.active_plan.status == TaskStatus.NEEDS_RECONCILIATION
    assert restored.current_step.status.value == "UNKNOWN"
    assert restored.candidate(5.0) is None


def test_engine_executes_a_multi_step_task_plan_end_to_end(config_manager):
    async def run():
        clock = ReplayClock(mono=0, wall=1_800_000_000)
        executor = ReplayActionExecutor()
        engine = ContinuityEngine(config_manager, clock=clock, action_executor=executor, replay_mode=True)
        try:
            engine.inject(engine.new_event(EventKind.TASK_PLAN_SUBMITTED.value, simple_plan(),
                                           source="task_planner", priority=90))
            first = engine.step(dt=0)
            assert first.state.current_intent is not None
            assert first.state.current_intent.intent_type == "task_step"
            assert [p.action_type for p in first.proposals] == ["SPEAK"]
            await engine.dispatch_action(first.proposals[0])

            clock.advance(0.1)
            second = engine.step(dt=0.1)
            assert second.state.current_intent is not None
            assert second.state.current_intent.intent_type == "task_step"
            assert [p.action_type for p in second.proposals] == ["OBSERVE"]
            await engine.dispatch_action(second.proposals[0])

            clock.advance(0.1)
            finished = engine.step(dt=0.1)
            assert engine.task_manager.active_plan.status == TaskStatus.SUCCEEDED
            assert finished.state.task_state["active_plan"]["status"] == "SUCCEEDED"
            assert not finished.proposals
        finally:
            engine.close()
    asyncio.run(run())


def test_engine_task_cancel_cancels_current_plan(config_manager):
    clock = ReplayClock(mono=0, wall=1_800_000_000)
    engine = ContinuityEngine(config_manager, clock=clock, replay_mode=True, action_executor=ReplayActionExecutor())
    try:
        engine.inject(engine.new_event(EventKind.TASK_PLAN_SUBMITTED.value, simple_plan(), source="task_planner", priority=90))
        start = engine.step(dt=0)
        assert start.proposals
        engine.inject(engine.new_event(EventKind.TASK_CANCEL.value, {"task_id": "task-1"}, source="button", priority=100))
        cancelled = engine.step(dt=0)
        assert engine.task_manager.active_plan.status == TaskStatus.CANCELLED
        assert not cancelled.proposals
    finally:
        engine.close()


def test_task_plan_state_persists_and_running_action_is_not_replayed(config_manager):
    repo = SQLiteRepository(":memory:")
    clock = ReplayClock(mono=0, wall=1_800_000_000)
    engine = ContinuityEngine(config_manager, clock=clock, replay_mode=True, action_executor=ReplayActionExecutor())
    try:
        engine.inject(engine.new_event(EventKind.TASK_PLAN_SUBMITTED.value, simple_plan(), source="task_planner", priority=90))
        step = engine.step(dt=0)
        # Mark the action as in-flight in task state to emulate a crash at an unsafe point.
        assert step.proposals
        engine.task_manager.bind_action(step.proposals[0].intent_id, step.proposals[0].action_id, clock.monotonic())
        engine.state.task_state = engine.task_manager.snapshot()
        engine.save(repo)
        restored_engine = ContinuityEngine(config_manager, clock=ReplayClock(mono=0, wall=1_800_000_010),
                                           replay_mode=True, action_executor=ReplayActionExecutor())
        assert restored_engine.restore(repo)
        assert restored_engine.task_manager.active_plan.status == TaskStatus.NEEDS_RECONCILIATION
        assert restored_engine.task_manager.candidate(1.0) is None
        restored_engine.close()
    finally:
        engine.close()
        repo.close()


def test_failed_task_exposes_stable_replan_request_and_accepts_replacement(config_manager):
    async def run():
        clock = ReplayClock(mono=0, wall=1_800_000_000)
        executor = ReplayActionExecutor(result_by_action_type={"SPEAK": ActionStatus.FAILED})
        engine = ContinuityEngine(config_manager, clock=clock, action_executor=executor, replay_mode=True)
        try:
            original = {"task_id": "replan-source", "goal": "greet then observe", "priority": 0.88,
                        "steps": [
                            {"step_id": "greet", "action_type": "SPEAK", "args": {"text": "hello"}},
                            {"step_id": "observe", "action_type": "OBSERVE", "args": {"key": "room_activity"}},
                        ]}
            engine.inject(engine.new_event(EventKind.TASK_PLAN_SUBMITTED.value, original, source="planner", priority=90))
            first = engine.step(dt=0)
            await engine.dispatch_action(first.proposals[0])
            failed = engine.step(dt=0)
            request = failed.replan_request
            assert request is not None
            assert request["failed_task_id"] == "replan-source"
            assert request["failed_step_id"] == "greet"
            assert request["remaining_steps"][0]["status"] == "FAILED"
            assert failed.replan_request["request_id"] == request["request_id"]

            replacement = {"task_id": "replan-replacement", "replaces_task_id": "replan-source",
                           "goal": "observe without greeting", "priority": 0.88,
                           "steps": [{"step_id": "observe-only", "action_type": "OBSERVE",
                                      "args": {"key": "room_activity"}}]}
            engine.inject(engine.new_event(EventKind.TASK_PLAN_SUBMITTED.value, replacement,
                                           source="planner", priority=90))
            accepted = engine.step(dt=0)
            assert engine.task_manager.active_plan.task_id == "replan-replacement"
            assert engine.task_manager.active_plan.status == TaskStatus.ACTIVE
            assert any(row["task_id"] == "replan-source" for row in engine.task_manager.history)
            assert accepted.replan_request is None
        finally:
            engine.close()
    asyncio.run(run())


def test_replan_replacement_requires_exact_failed_task_id():
    manager = TaskManager()
    manager.submit({"task_id": "failed-1", "goal": "old goal", "steps": [
        {"step_id": "say", "action_type": "SPEAK", "args": {"text": "hello"}}
    ]}, 0.0)
    candidate = manager.candidate(0.1)
    assert candidate and manager.bind_intent("i1", candidate.goal) and manager.bind_action("i1", "a1", 0.1)
    manager.on_action_result(action_id="a1", intent_id="i1", status="FAILED", now_mono=1.0)
    with pytest.raises(TaskPlanError, match="does not match"):
        manager.submit({"task_id": "new-1", "replaces_task_id": "another-task", "goal": "new goal",
                        "steps": [{"action_type": "WAIT", "args": {"duration_s": 1}}]}, 2.0)


def test_manual_reconciliation_retry_is_limited_to_one():
    manager = TaskManager()
    manager.submit({"task_id": "manual-retry", "goal": "say", "steps": [
        {"step_id": "say", "action_type": "SPEAK", "args": {"text": "hello"}}
    ]}, 0.0)
    candidate = manager.candidate(0.1)
    assert candidate and manager.bind_intent("i1", candidate.goal) and manager.bind_action("i1", "a1", 0.1)
    manager.on_action_result(action_id="a1", intent_id="i1", status="UNKNOWN", now_mono=1.0)
    assert manager.resolve_step("manual-retry", "say", "retry", 2.0, confirmed_not_completed=True).accepted
    assert manager.resume("manual-retry").accepted
    candidate = manager.candidate(2.1)
    assert candidate and manager.bind_intent("i2", candidate.goal) and manager.bind_action("i2", "a2", 2.1)
    manager.on_action_result(action_id="a2", intent_id="i2", status="UNKNOWN", now_mono=3.0)
    result = manager.resolve_step("manual-retry", "say", "retry", 4.0, confirmed_not_completed=True)
    assert not result.accepted
    assert result.code == "MANUAL_RETRY_BUDGET_EXHAUSTED"


def test_task_deadline_expiring_during_running_action_requires_reconciliation():
    manager = TaskManager(maximum_task_deadline_s=20.0)
    manager.submit({"task_id": "deadline-running", "goal": "wait", "deadline_in_s": 1.0, "steps": [
        {"step_id": "wait", "action_type": "WAIT", "args": {"duration_s": 0.5}, "timeout_s": 5.0}
    ]}, 0.0)
    candidate = manager.candidate(0.1)
    assert candidate and manager.bind_intent("i1", candidate.goal) and manager.bind_action("i1", "a1", 0.1)
    assert manager.expire_if_needed(1.0)
    assert manager.active_plan.status == TaskStatus.NEEDS_RECONCILIATION
    assert manager.active_plan.steps[0].status.value == "UNKNOWN"


def test_recovery_rejects_tampered_task_snapshot():
    manager = TaskManager()
    manager.submit({"task_id": "valid", "goal": "wait", "steps": [
        {"step_id": "wait", "action_type": "WAIT", "args": {"duration_s": 1}}
    ]}, 0.0)
    snapshot = manager.snapshot()
    snapshot["active_plan"]["steps"][0]["action_type"] = "SHELL"
    with pytest.raises(TaskPlanError, match="not allowlisted"):
        TaskManager(snapshot=snapshot)


def test_unstarted_action_binding_can_be_rolled_back_without_consuming_attempt():
    manager = TaskManager()
    manager.submit({"task_id": "unbind", "goal": "wait", "steps": [
        {"step_id": "wait", "action_type": "WAIT", "args": {"duration_s": 1}}
    ]}, 0.0)
    candidate = manager.candidate(0.1)
    assert candidate and manager.bind_intent("intent", candidate.goal)
    assert manager.bind_action("intent", "action", 0.1)
    assert manager.active_plan.steps[0].attempts == 1
    assert manager.unbind_action_before_start("intent", "action", "USER_STOP")
    step = manager.active_plan.steps[0]
    assert step.status.value == "PENDING"
    assert step.attempts == 0
    assert step.bound_action_id is None


def test_task_timeout_does_not_create_automatic_replan_request(config_manager):
    async def run():
        clock = ReplayClock(mono=0, wall=1_800_000_000)
        executor = ReplayActionExecutor(latency_s=0.2)
        engine = ContinuityEngine(config_manager, clock=clock, action_executor=executor, replay_mode=True)
        try:
            plan = {"task_id": "timeout-plan", "goal": "wait briefly", "priority": 0.88,
                    "steps": [{"step_id": "wait", "action_type": "WAIT", "args": {"duration_s": 0.1}, "timeout_s": 0.1}]}
            engine.inject(engine.new_event(EventKind.TASK_PLAN_SUBMITTED.value, plan, source="test", priority=90))
            first = engine.step(dt=0)
            assert first.proposals
            _, action_result = await engine.dispatch_action(first.proposals[0])
            assert action_result is not None and action_result.status == ActionStatus.TIMED_OUT
            after = engine.step(dt=0)
            assert engine.task_manager.active_plan.status == TaskStatus.NEEDS_RECONCILIATION
            assert after.replan_request is None
            assert after.state.current_intent is None
            # Resolve the ambiguous outcome explicitly and resume. A fresh policy
            # intent/proposal must be possible; the stale WAITING intent must not block it.
            engine.inject(engine.new_event(EventKind.TASK_RESOLVE_STEP.value, {
                "task_id": "timeout-plan", "step_id": "wait", "outcome": "retry",
                "confirmed_not_completed": True,
            }, source="operator", priority=100))
            engine.inject(engine.new_event(EventKind.TASK_RESUME.value, {"task_id": "timeout-plan"},
                                           source="operator", priority=100))
            resumed = engine.step(dt=0)
            assert engine.task_manager.active_plan.status == TaskStatus.ACTIVE
            assert resumed.state.current_intent is not None
            assert resumed.state.current_intent.intent_type == "task_step"
            assert resumed.proposals
        finally:
            engine.close()
    asyncio.run(run())


def test_successful_observe_action_flows_back_as_allowlisted_world_event(config_manager):
    async def run():
        clock = ReplayClock(mono=0, wall=1_800_000_000)
        executor = ReplayActionExecutor(details_by_action_type={
            "OBSERVE": {"observation": {"value": "person_at_desk", "confidence": 0.96, "ttl_s": 5.0}}
        })
        engine = ContinuityEngine(config_manager, clock=clock, action_executor=executor, replay_mode=True)
        try:
            plan = {"task_id": "observe-feedback", "goal": "observe room", "priority": 0.88,
                    "steps": [{"step_id": "observe", "action_type": "OBSERVE",
                               "args": {"key": "room_activity"}}]}
            engine.inject(engine.new_event(EventKind.TASK_PLAN_SUBMITTED.value, plan, source="test", priority=90))
            first = engine.step(dt=0)
            await engine.dispatch_action(first.proposals[0])
            terminal = engine.step(dt=0)
            assert any(item.startswith("observe_result_queued:room_activity") for item in terminal.diagnostics)
            observed = engine.step(dt=0)
            fact = observed.state.world["room_activity"]
            assert fact.value == "person_at_desk"
            assert fact.confidence == pytest.approx(0.96)
        finally:
            engine.close()
    asyncio.run(run())


def test_observe_action_cannot_return_an_unrequested_world_key(config_manager):
    async def run():
        clock = ReplayClock(mono=0, wall=1_800_000_000)
        executor = ReplayActionExecutor(details_by_action_type={
            "OBSERVE": {"observation": {"key": "private_notes", "value": "secret", "confidence": 0.99, "ttl_s": 5.0}}
        })
        engine = ContinuityEngine(config_manager, clock=clock, action_executor=executor, replay_mode=True)
        try:
            plan = {"task_id": "observe-key-binding", "goal": "observe room", "priority": 0.88,
                    "steps": [{"step_id": "observe", "action_type": "OBSERVE",
                               "args": {"key": "room_activity"}}]}
            engine.inject(engine.new_event(EventKind.TASK_PLAN_SUBMITTED.value, plan, source="test", priority=90))
            first = engine.step(dt=0)
            await engine.dispatch_action(first.proposals[0])
            engine.step(dt=0)
            # The observation object tries to smuggle an additional key; strict result
            # schema validation rejects the whole observation instead of accepting it.
            observed = engine.step(dt=0)
            assert "room_activity" not in observed.state.world
            assert "private_notes" not in observed.state.world
        finally:
            engine.close()
    asyncio.run(run())
