from __future__ import annotations

import asyncio

from continuity_engine import LLMGateway, LLMDemandInterpreter, LLMStatus, StubLLMAdapter


def valid_payload():
    return {
        "schema_version": "1.0",
        "request_type": "emotional_sharing",
        "intent": "用户表达今天很累，希望得到情感回应",
        "entities": [{"type": "time", "value": "今天"}],
        "emotion_assessment": {"emotion": "fatigue", "confidence": 0.82, "evidence": ["我很累"]},
        "clarification_needed": False,
        "response_candidate": "今天辛苦啦，先歇一会儿吧。",
    }


def test_demand_interpreter_returns_structured_contract_and_context():
    async def run():
        adapter = StubLLMAdapter({"demand_understanding": valid_payload()})
        interpreter = LLMDemandInterpreter(LLMGateway(adapter, timeout_s=1))
        result = await interpreter.understand(
            "我今天好累", request_context={"conversation_id": "c1"},
            personality_snapshot={"traits": {"warmth": 0.8}},
            affect_guidance="温暖自然", interaction_state_snapshot={"current_strategy": None},
            voice_signals={"source": "sensevoice_asr", "emotion": "sad"},
        )
        assert result == valid_payload()
        assert adapter.calls[0].purpose == "demand_understanding"
        assert '"request_context":{"conversation_id":"c1"}' in adapter.calls[0].context_json
        assert '"affect_guidance":"温暖自然"' in adapter.calls[0].context_json
    asyncio.run(run())


def test_demand_output_schema_rejects_route_directives_and_extra_fields():
    async def run():
        payload = valid_payload()
        payload["route"] = "device_control"
        adapter = StubLLMAdapter({"demand_understanding": payload})
        result = await LLMGateway(adapter, timeout_s=1, max_output_retries=0).generate(
            interaction_id="x", intent_id=None, generation=1, purpose="demand_understanding",
            view=__import__("continuity_engine").StateView(
                purpose="demand_understanding", schema_version="1.0",
                _fields={"purpose": "demand_understanding", "user_input": "hello", "request_context": {},
                         "personality_snapshot": {}, "affect_guidance": "", "interaction_state_snapshot": {}, "voice_signals": {}}),
        )
        assert result.status == LLMStatus.REJECTED
        assert result.error_code == "INVALID_DEMAND_FIELDS"
    asyncio.run(run())


def test_invalid_emotion_confidence_is_rejected():
    async def run():
        payload = valid_payload()
        payload["emotion_assessment"]["confidence"] = 1.2
        adapter = StubLLMAdapter({"demand_understanding": payload})
        from continuity_engine import StateView
        view = StateView(purpose="demand_understanding", schema_version="1.0", _fields={
            "purpose": "demand_understanding", "user_input": "hello", "request_context": {},
            "personality_snapshot": {}, "affect_guidance": "", "interaction_state_snapshot": {}, "voice_signals": {},
        })
        result = await LLMGateway(adapter, timeout_s=1, max_output_retries=0).generate(
            interaction_id="x", intent_id=None, generation=1, purpose="demand_understanding", view=view)
        assert result.status == LLMStatus.REJECTED
        assert result.error_code == "INVALID_EMOTION_CONFIDENCE"
    asyncio.run(run())


def test_engine_demand_api_injects_current_personality_and_strategy(tmp_path):
    from pathlib import Path
    import yaml
    from continuity_engine import ConfigManager, ContinuityEngine

    async def run():
        root = Path(__file__).resolve().parents[1]
        config_data = yaml.safe_load((root / "config" / "default.yaml").read_text(encoding="utf-8"))
        config_data["affect_personality"]["persistence_path"] = str(tmp_path / "persona.json")
        adapter = StubLLMAdapter({"demand_understanding": valid_payload()})
        engine = ContinuityEngine(ConfigManager(config_data), llm_gateway=LLMGateway(adapter, timeout_s=1))
        engine.set_interaction_strategy("comfort", "先接住情绪，不主动给建议")
        result = await engine.understand_demand(
            user_input="我今天好累", interaction_id="conversation-1", generation=1,
            voice_signals={"source": "sensevoice_asr", "emotion": "tired"},
        )
        assert result["request_type"] == "emotional_sharing"
        assert '"current_strategy"' in adapter.calls[0].context_json
        assert '"affect_guidance"' in adapter.calls[0].context_json
        engine.close()
    asyncio.run(run())


def test_engine_router_chooses_route_from_broad_type_not_llm_route_field():
    from continuity_engine import RequestRouter, RouteStatus, build_default_registry
    router = RequestRouter(build_default_registry())
    demand = valid_payload()
    decision = router.route_understanding("我今天好累", demand)
    assert decision.status == RouteStatus.READY
    assert decision.response_text == demand["response_candidate"]
    assert decision.intent.name.value == "SMALL_TALK"

    malicious = valid_payload()
    malicious["route"] = "device_control"
    try:
        router.route_understanding("我今天好累", malicious)
    except ValueError as exc:
        assert "INVALID_DEMAND_FIELDS" in str(exc)
    else:
        raise AssertionError("route directives are not part of the demand contract")


def test_engine_router_does_not_turn_memory_update_into_an_action():
    from continuity_engine import RequestRouter, RouteStatus, build_default_registry
    router = RequestRouter(build_default_registry())
    demand = valid_payload()
    demand["request_type"] = "memory_update"
    demand["response_candidate"] = None
    decision = router.route_understanding("记住我喜欢茶", demand)
    assert decision.intent.name.value == "UNKNOWN"
    assert decision.status == RouteStatus.NEEDS_CLARIFICATION


def test_canonical_engine_request_path_uses_structured_demand_then_router(tmp_path):
    from pathlib import Path
    import yaml
    from continuity_engine import ConfigManager, ContinuityEngine, RequestRouter, RouteStatus, build_default_registry

    async def run():
        root = Path(__file__).resolve().parents[1]
        config_data = yaml.safe_load((root / "config" / "default.yaml").read_text(encoding="utf-8"))
        config_data["affect_personality"]["persistence_path"] = str(tmp_path / "persona.json")
        adapter = StubLLMAdapter({"demand_understanding": valid_payload()})
        engine = ContinuityEngine(ConfigManager(config_data), llm_gateway=LLMGateway(adapter, timeout_s=1))
        router = RequestRouter(build_default_registry())
        demand, decision = await engine.process_user_request(
            user_input="我今天好累", interaction_id="conversation-canonical", generation=1,
            router=router,
        )
        assert demand["request_type"] == "emotional_sharing"
        assert decision.status == RouteStatus.READY
        assert [call.purpose for call in adapter.calls] == ["demand_understanding"]
        engine.close()
    asyncio.run(run())
