import asyncio
from copy import deepcopy
import pytest

from continuity_engine import (
    ConfigError, ConfigManager, EngineState, LLMGateway, LLMStatus, ReplayActionExecutor,
    ReplayLLMAdapter, ReplayModeError, ReplayNetworkAccessError, ReplayNetworkGuard,
    SimulatedActionExecutor, StateViewBuilder, StubLLMAdapter,
)


def test_config_reload_is_staged_atomic_and_applies_only_allowlist(config_manager):
    original = config_manager.get()
    candidate = deepcopy(original)
    candidate["policy"]["switch_margin"] = 0.20
    assert config_manager.stage_reload(candidate) == 2
    assert config_manager.get()["policy"]["switch_margin"] == original["policy"]["switch_margin"]
    assert config_manager.apply_pending() == 2
    assert config_manager.get()["policy"]["switch_margin"] == 0.20


def test_config_reload_rejects_privacy_change_without_partial_application(config_manager):
    original = config_manager.get()
    candidate = deepcopy(original)
    candidate["product"]["raw_audio_persistent_storage"] = True
    candidate["policy"]["switch_margin"] = 0.25
    with pytest.raises(ConfigError):
        config_manager.stage_reload(candidate)
    assert config_manager.get() == original
    assert config_manager.apply_pending() == 1


def test_llm_gateway_uses_filtered_view_and_validates_output():
    async def run():
        state = EngineState()
        view = StateViewBuilder().build("complex_dialogue", state, user_input="hello")
        adapter = StubLLMAdapter({"complex_dialogue": {"response_text": "你好。"}})
        gateway = LLMGateway(adapter, timeout_s=1)
        gateway.set_generation("chat-1", 1)
        result = await gateway.generate(interaction_id="chat-1", intent_id="i1", generation=1,
                                        purpose="complex_dialogue", view=view)
        assert result.status == LLMStatus.COMPLETED
        assert result.payload["response_text"] == "你好。"
    asyncio.run(run())


def test_llm_gateway_rejects_invalid_schema():
    async def run():
        view = StateViewBuilder().build("simple_response", EngineState(), user_input="hi")
        gateway = LLMGateway(StubLLMAdapter({"simple_response": {"text": "not correct key"}}), timeout_s=1)
        result = await gateway.generate(interaction_id="x", intent_id=None, generation=1,
                                        purpose="simple_response", view=view)
        assert result.status == LLMStatus.REJECTED
        assert result.error_code == "UNEXPECTED_OUTPUT_FIELDS"
    asyncio.run(run())


def test_stale_llm_result_is_rejected_after_new_generation():
    async def run():
        view = StateViewBuilder().build("simple_response", EngineState(), user_input="hi")
        adapter = StubLLMAdapter({"simple_response": {"response_text": "old response"}}, delay_s=0.08)
        gateway = LLMGateway(adapter, timeout_s=1)
        gateway.set_generation("chat", 1)
        task = asyncio.create_task(gateway.generate(interaction_id="chat", intent_id="i1", generation=1,
                                                    purpose="simple_response", view=view))
        await asyncio.sleep(0.01)
        gateway.set_generation("chat", 2)
        result = await task
        assert result.status == LLMStatus.STALE
    asyncio.run(run())


def test_llm_gateway_timeout_is_bounded():
    async def run():
        view = StateViewBuilder().build("simple_response", EngineState(), user_input="hi")
        gateway = LLMGateway(StubLLMAdapter({"simple_response": {"response_text": "late"}}, delay_s=0.1), timeout_s=0.01)
        result = await gateway.generate(interaction_id="chat", intent_id=None, generation=1,
                                        purpose="simple_response", view=view)
        assert result.status == LLMStatus.TIMED_OUT
    asyncio.run(run())


def test_replay_mode_rejects_real_adapters():
    class RealishAdapter:
        is_replay_adapter = False
        async def generate(self, request):
            return {"response_text": ""}
    view = StateViewBuilder().build("simple_response", EngineState(), user_input="hi")
    with pytest.raises(ReplayModeError):
        LLMGateway(RealishAdapter(), replay_mode=True, action_executor=ReplayActionExecutor())
    with pytest.raises(ReplayModeError):
        LLMGateway(StubLLMAdapter({"simple_response": {"response_text": "ok"}}), replay_mode=True,
                   action_executor=SimulatedActionExecutor())


def test_replay_network_guard_blocks_socket_api_and_restores():
    import socket
    original = socket.create_connection
    with ReplayNetworkGuard():
        with pytest.raises(ReplayNetworkAccessError):
            socket.create_connection(("127.0.0.1", 1), timeout=0.01)
    assert socket.create_connection is original


def test_invalidate_intent_cancels_provider_worker_not_calling_workflow():
    async def run():
        view = StateViewBuilder().build("simple_response", EngineState(), user_input="hi")
        gateway = LLMGateway(StubLLMAdapter({"simple_response": {"response_text": "late"}}, delay_s=0.2), timeout_s=1)
        gateway.set_generation("chat", 1)
        marker = {"workflow_continued": False}
        task = asyncio.create_task(gateway.generate(interaction_id="chat", intent_id="i1", generation=1,
                                                    purpose="simple_response", view=view))
        await asyncio.sleep(0.02)
        gateway.invalidate_intent("i1")
        result = await task
        marker["workflow_continued"] = True
        assert result.status == LLMStatus.CANCELLED
        assert marker["workflow_continued"] is True
    asyncio.run(run())


def test_llm_plan_cannot_propose_tool_not_in_filtered_context():
    async def run():
        state = EngineState()
        view = StateViewBuilder().build("multi_step_plan", state, goal="do task", allowed_tools=["safe_tool"])
        adapter = StubLLMAdapter({"multi_step_plan": {"steps": [{"description": "do it", "tool": "admin_delete", "arguments": {}}]}})
        result = await LLMGateway(adapter, timeout_s=1).generate(interaction_id="x", intent_id=None, generation=1,
                                                                purpose="multi_step_plan", view=view)
        assert result.status == LLMStatus.REJECTED
        assert result.error_code == "DISALLOWED_TOOL_PROPOSAL"
    asyncio.run(run())


def test_runtime_event_rejects_noncurrent_schema_before_injection():
    from continuity_engine import Event
    with pytest.raises(ValueError):
        Event("legacy", "USER_MESSAGE", "test", 0.0, 1.0, {"text": "hello"}, event_schema_version="0.9")


def test_nested_replay_network_guards_keep_network_blocked_until_last_exit():
    import socket
    original = socket.create_connection
    outer = ReplayNetworkGuard()
    inner = ReplayNetworkGuard()
    with outer:
        with inner:
            with pytest.raises(ReplayNetworkAccessError):
                socket.create_connection(("127.0.0.1", 1), timeout=0.01)
        with pytest.raises(ReplayNetworkAccessError):
            socket.create_connection(("127.0.0.1", 1), timeout=0.01)
    assert socket.create_connection is original


def test_replay_mode_rejects_subclasses_that_could_override_safe_adapters():
    class UnsafeReplaySubclass(ReplayLLMAdapter):
        async def generate(self, request):
            # An actual adapter could attempt network I/O here; replay rejects subclassing
            # before any call can happen.
            return {"response_text": "unsafe"}

    with pytest.raises(ReplayModeError):
        LLMGateway(UnsafeReplaySubclass(), replay_mode=True, action_executor=ReplayActionExecutor())
    with pytest.raises(ReplayModeError):
        from continuity_engine.llm_gateway import ReplayGuard
        ReplayGuard.validate(replay_mode=True, llm_adapter=None, action_executor=type("DerivedExecutor", (ReplayActionExecutor,), {})())


def test_config_rejects_nonfinite_timeouts_and_drive_parameters(config_manager):
    from continuity_engine import ConfigError
    import math
    bad_timeout = config_manager.get()
    bad_timeout["llm"]["default_timeout_s"] = math.nan
    with pytest.raises(ConfigError):
        ConfigManager(bad_timeout)
    bad_tau = config_manager.get()
    bad_tau["drives"]["default"]["exploration"]["tau_s"] = math.inf
    with pytest.raises(ConfigError):
        ConfigManager(bad_tau)
