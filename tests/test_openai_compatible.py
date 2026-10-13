"""Provider-adapter tests use httpx.MockTransport and never access the network."""
from __future__ import annotations

import json
import time

import httpx
import pytest

from continuity_engine import (
    LLMRequest, OpenAICompatibleAdapter, ProviderConfigurationError, ProviderResponseError,
)


def request_for(purpose="task_plan", context=None):
    return LLMRequest(
        request_id="request-1", interaction_id="interaction-1", intent_id=None,
        generation=0, purpose=purpose, created_at_mono=time.monotonic(),
        deadline_mono=time.monotonic() + 5,
        context_json=json.dumps(context or {
            "purpose": "task_plan", "goal": "wait briefly", "allowed_action_types": ["WAIT"],
            "max_steps": 2, "constraints": [], "task_progress": {}, "failed_plan": {},
            "confirmed_world_facts": [],
        }), output_token_budget=300,
    )


@pytest.mark.asyncio
async def test_openai_compatible_adapter_builds_schema_constrained_request_without_network():
    seen = {}
    content = json.dumps({"steps": [{"description": "Wait", "action_type": "WAIT", "args": {"duration_s": 1}}]})

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OpenAICompatibleAdapter(
        endpoint_url="https://provider.invalid/v1/chat/completions", model="test-model",
        api_key="test-secret", timeout_s=1, client=client,
    )
    try:
        output = await adapter.generate(request_for())
        assert output == {"steps": [{"description": "Wait", "action_type": "WAIT", "args": {"duration_s": 1}}]}
        assert seen["url"] == "https://provider.invalid/v1/chat/completions"
        assert seen["headers"]["authorization"] == "Bearer test-secret"
        assert seen["body"]["model"] == "test-model"
        assert seen["body"]["response_format"] == {"type": "json_object"}
        assert seen["body"]["messages"][0]["role"] == "system"
        assert "allowed_action_types" in seen["body"]["messages"][1]["content"]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_adapter_rejects_non_json_provider_content():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "not-json"}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OpenAICompatibleAdapter(endpoint_url="http://localhost/v1/chat/completions", model="local", client=client)
    try:
        with pytest.raises(ProviderResponseError, match="not_json"):
            await adapter.generate(request_for())
    finally:
        await client.aclose()


def test_adapter_rejects_url_embedded_credentials_and_invalid_timeout():
    with pytest.raises(ProviderConfigurationError):
        OpenAICompatibleAdapter(endpoint_url="https://user:password@host/v1/chat/completions", model="m")
    with pytest.raises(ProviderConfigurationError, match="HTTPS"):
        OpenAICompatibleAdapter(endpoint_url="http://remote-provider/v1/chat/completions", model="m")
    with pytest.raises(ProviderConfigurationError):
        OpenAICompatibleAdapter(endpoint_url="https://host/v1/chat/completions", model="m", timeout_s=float("inf"))


def test_adapter_from_env_requires_endpoint_and_model(monkeypatch):
    monkeypatch.delenv("TEST_PROVIDER_ENDPOINT", raising=False)
    monkeypatch.delenv("TEST_PROVIDER_MODEL", raising=False)
    with pytest.raises(ProviderConfigurationError, match="TEST_PROVIDER_ENDPOINT"):
        OpenAICompatibleAdapter.from_env(prefix="TEST_PROVIDER_")


def test_adapter_from_file_loads_provider_config_without_logging_secrets(tmp_path, monkeypatch):
    from continuity_engine import OpenAICompatibleAdapter

    monkeypatch.setenv("TEST_PROVIDER_KEY", "test-secret-from-env")
    path = tmp_path / "llm.local.yaml"
    path.write_text(
        "provider:\n"
        "  endpoint_url: https://ark.example.invalid/api/v3/chat/completions\n"
        "  model: test-model\n"
        "  api_key_env: TEST_PROVIDER_KEY\n"
        "  timeout_s: 17\n"
        "  temperature: 0.2\n",
        encoding="utf-8",
    )
    adapter = OpenAICompatibleAdapter.from_file(path)
    try:
        assert adapter.endpoint_url == "https://ark.example.invalid/api/v3/chat/completions"
        assert adapter.model == "test-model"
        assert adapter.api_key == "test-secret-from-env"
        assert adapter.timeout_s == 17
    finally:
        import asyncio
        asyncio.run(adapter.aclose())


def test_adapter_from_file_rejects_unknown_keys(tmp_path):
    path = tmp_path / "provider.yaml"
    path.write_text("endpoint_url: https://example.invalid/chat\nmodel: m\nsecret_key: typo\n", encoding="utf-8")
    with pytest.raises(ProviderConfigurationError, match="unknown provider config keys"):
        OpenAICompatibleAdapter.from_file(path, client=object())


@pytest.mark.asyncio
async def test_openai_compatible_adapter_integrates_with_task_planner_without_external_network(config_manager):
    from continuity_engine import (
        ContinuityEngine, LLMGateway, LLMTaskPlanner, PlanningStatus,
        ReplayClock, SimulatedActionExecutor,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "mock-model"
        assert "task_plan" in body["messages"][1]["content"]
        content = json.dumps({"steps": [
            {"description": "Wait one second", "action_type": "WAIT", "args": {"duration_s": 1.0}},
        ]})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OpenAICompatibleAdapter(endpoint_url="https://mock.provider/v1/chat/completions",
                                      model="mock-model", client=client)
    executor = SimulatedActionExecutor()
    gateway = LLMGateway(adapter, timeout_s=1.0, context_token_budgets=config_manager.get()["llm"]["context_token_budgets"])
    engine = ContinuityEngine(config_manager, clock=ReplayClock(mono=1.0, wall=1_800_000_000),
                              action_executor=executor, llm_gateway=gateway, replay_mode=False)
    try:
        result = await LLMTaskPlanner().plan_goal(engine, "wait one second", request_id="mock-provider-plan")
        assert result.status == PlanningStatus.QUEUED
        step = engine.step(dt=0.0)
        assert any(x.startswith("task_plan:PLAN_ACCEPTED:") for x in step.diagnostics)
        assert engine.task_manager.active_plan is not None
        assert engine.task_manager.active_plan.task_id == result.task_id
    finally:
        engine.close()
        await client.aclose()

@pytest.mark.asyncio
async def test_dialogue_adapter_applies_host_approved_current_strategy_as_system_constraint():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"response_text": "我在听。"}, ensure_ascii=False)}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OpenAICompatibleAdapter(endpoint_url="https://provider.invalid/v1/chat/completions", model="test", client=client)
    context = {
        "purpose": "complex_dialogue", "user_input": "我今天有点累", "interaction_rules": {
            "current_interaction_strategy": {"name": "倾听优先", "instruction": "以倾听为主，减少追问。", "source": "engine_decision"}
        }, "current_task": None, "relevant_memories": [], "recent_dialogue": [], "world_summary": []
    }
    try:
        await adapter.generate(request_for("complex_dialogue", context))
        system_prompt = seen["body"]["messages"][0]["content"]
        assert "Host-approved current interaction strategy" in system_prompt
        assert "以倾听为主，减少追问" in system_prompt
        assert "Do not violate safety, privacy" in system_prompt
    finally:
        await client.aclose()
