from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from continuity_engine import (
    CompanionService, CompanionStatus, CompanionMetrics, ConfigManager,
    ContinuityEngine, FeatureHashEmbedder, FieldValidationRunner, HybridMemoryRetriever,
    LLMRuntime, LLMGateway, OpenAICompatibleAdapter, OpenAICompatibleSpeechRecognizer,
    OpenAICompatibleTextToSpeech, ReplayActionExecutor, ReplayClock, ReplayLLMAdapter,
    SQLiteConversationStore, SQLiteRepository, SubprocessAudioSink,
    VoiceProviderResponseError,
)


def make_engine(config_manager, adapter=None, *, replay_mode=True):
    gateway = None
    if adapter is not None:
        gateway = LLMGateway(adapter, timeout_s=1.0,
                             context_token_budgets=config_manager.get()["llm"]["context_token_budgets"])
    return ContinuityEngine(config_manager, clock=ReplayClock(mono=5.0, wall=1_800_000_000),
                            action_executor=ReplayActionExecutor(), llm_gateway=gateway,
                            replay_mode=replay_mode)


def test_hybrid_memory_retrieval_uses_semantic_fallback_and_persists_vectors(tmp_path):
    repo = SQLiteRepository(tmp_path / "memories.db")
    try:
        repo.put_memory("m1", "ongoing_interest", {"summary": "下周我有一场重要考试，最近压力很大。"}, ["turn-1"], 1000.0)
        repo.put_memory("m2", "user_preference", {"summary": "我喜欢喝乌龙茶。"}, ["turn-2"], 1001.0)
        retriever = HybridMemoryRetriever(repo, embedder=FeatureHashEmbedder(), clock=lambda: 1002.0)
        rows = retriever.search("我准备的测验怎么样了", limit=5)
        assert rows
        assert rows[0]["memory_id"] == "m1"
        assert rows[0]["retrieval_method"] == "hybrid"
        assert rows[0]["semantic_score"] > 0
        cached = repo.get_memory_embedding("m1", model_id="feature-hash-v1")
        assert cached and len(cached["embedding"]) == 384
    finally:
        repo.close()


def test_memory_correction_preserves_lineage_and_invalidates_old_embedding(tmp_path):
    repo = SQLiteRepository(tmp_path / "memory-correction.db")
    class EngineStub:
        memory_repository = repo
        clock = type("Clock", (), {"wall_time": staticmethod(lambda: 1000.0)})()
        def remember(self, summary, **kwargs):
            self.memory_repository.put_memory("memory-1", kwargs.get("kind", "user_fact"),
                                              dict(kwargs.get("payload") or {"summary": summary}),
                                              kwargs.get("source_ids", []), 1000.0)
            return "memory-1"
        def forget_memory(self, memory_id):
            return self.memory_repository.delete_memory(memory_id, 1000.0)
    engine = EngineStub()
    from continuity_engine import MemoryLifecycle
    memory = MemoryLifecycle(engine, clock=lambda: 1000.0)
    try:
        repo.put_memory("memory-1", "user_preference", {"summary": "我喜欢咖啡"}, ["turn-original"], 900.0)
        repo.set_memory_embedding("memory-1", "feature-hash-v1", FeatureHashEmbedder().embed("我喜欢咖啡"), 900.0)
        result = memory.correct("memory-1", "我更喜欢茶", source_id="turn-correction")
        assert result["status"] == "CORRECTED"
        row = repo.list_memories(limit=10)[0]
        assert row["payload"]["summary"] == "我更喜欢茶"
        assert row["payload"]["fact_status"] == "user_corrected"
        assert set(row["source_ids"]) == {"turn-original", "turn-correction"}
        assert repo.get_memory_embedding("memory-1") is None
    finally:
        repo.close()


def test_sqlite_conversation_store_is_opt_in_bounded_and_restorable(tmp_path):
    path = tmp_path / "session.db"
    disabled = SQLiteConversationStore(path, enabled=False)
    assert disabled.save_turn("s", "user", "do not persist", 0) is False
    assert disabled.load_session("s") == []
    disabled.close()
    store = SQLiteConversationStore(path, enabled=True, max_messages_per_session=4, clock=lambda: 1000.0)
    try:
        for i in range(6):
            store.save_turn("s", "user" if i % 2 == 0 else "assistant", f"turn {i}", i, 1000.0 + i)
        rows = store.load_session("s", limit=4)
        assert [row["content"] for row in rows] == ["turn 2", "turn 3", "turn 4", "turn 5"]
        assert store.clear_session("s") == 4
        assert store.load_session("s") == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_companion_restores_recent_context_after_service_restart(config_manager, tmp_path):
    adapter = ReplayLLMAdapter({
        "simple_response": {"response_text": "明白了，我会记住你正在准备考试。"},
        "complex_dialogue": {"response_text": "我记得你之前提到考试，最近准备得怎么样？"},
    })
    engine = make_engine(config_manager, adapter)
    repo = SQLiteRepository(tmp_path / "mem.db")
    store = SQLiteConversationStore(tmp_path / "dialogue.db", enabled=True, clock=lambda: 1_800_000_000.0)
    engine.attach_memory_repository(repo)
    try:
        first = CompanionService(engine, session_store=store)
        result1 = await first.chat("我最近在准备考试", interaction_id="s")
        assert result1.status in {CompanionStatus.COMPLETED, CompanionStatus.DEGRADED}
        second = CompanionService(engine, session_store=store)
        result2 = await second.chat("你还记得我之前说的事吗", interaction_id="s")
        assert result2.status in {CompanionStatus.COMPLETED, CompanionStatus.DEGRADED}
        payload = json.loads(adapter.calls[-1].context_json)
        recent = payload.get("recent_dialogue") or []
        assert any("我最近在准备考试" in str(item) for item in recent)
        assert len(second.history("s")) >= 3
    finally:
        engine.close(); repo.close(); store.close()


@pytest.mark.asyncio
async def test_real_llm_runtime_attach_and_explicit_probe_with_mock_transport(config_manager, tmp_path):
    seen = []
    async def handler(request: httpx.Request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"response_text": "probe ok"})}}]})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = tmp_path / "provider.yaml"
    config.write_text("provider:\n  endpoint_url: https://mock.invalid/v1/chat/completions\n  model: test-model\n", encoding="utf-8")
    engine = make_engine(config_manager, None, replay_mode=False)
    runtime = LLMRuntime.attach(engine, config_file=config, env_prefix=None, client=client)
    try:
        assert engine.llm_gateway is runtime.gateway
        configured = await runtime.probe()
        assert configured.configured is True and configured.reachable is None
        live = await runtime.probe(perform_inference=True)
        assert live.reachable is True
        assert len(seen) == 1 and seen[0]["model"] == "test-model"
    finally:
        await runtime.aclose(); await client.aclose(); engine.close()


@pytest.mark.asyncio
async def test_openai_compatible_asr_and_tts_http_adapters_with_mock_transport():
    seen = {"asr": False, "tts": False}
    async def handler(request: httpx.Request):
        if request.url.path.endswith("/transcriptions"):
            seen["asr"] = True
            assert request.headers.get("content-type", "").startswith("multipart/form-data")
            return httpx.Response(200, json={"text": "你好", "language": "zh"})
        seen["tts"] = True
        payload = json.loads(request.content)
        assert payload["input"] == "你好"
        return httpx.Response(200, content=b"fake-audio-content", headers={"content-type": "audio/wav"})
    class Sink:
        def __init__(self): self.played = []; self.stopped = False
        async def play(self, audio, *, audio_format):
            self.played.append((audio, audio_format)); return {"status": "SUCCEEDED"}
        async def stop(self): self.stopped = True
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink = Sink()
    asr = OpenAICompatibleSpeechRecognizer(endpoint_url="https://mock.invalid/v1/audio/transcriptions", model="whisper-test", client=client)
    tts = OpenAICompatibleTextToSpeech(endpoint_url="https://mock.invalid/v1/audio/speech", model="tts-test", sink=sink, client=client)
    try:
        transcript = await asr.transcribe(b"RIFF" + b"x" * 100)
        assert transcript["text"] == "你好" and transcript["language"] == "zh"
        speech = await tts.speak("你好")
        assert speech["status"] == "SUCCEEDED"
        assert sink.played == [(b"fake-audio-content", "wav")]
        await tts.stop()
        assert sink.stopped and seen == {"asr": True, "tts": True}
    finally:
        await asr.aclose(); await tts.aclose(); await client.aclose()


def test_field_validation_does_not_claim_field_pass_without_explicit_probe(config_manager):
    engine = make_engine(config_manager, ReplayLLMAdapter({"simple_response": {"response_text": "ok"}}))
    repo = SQLiteRepository(":memory:")
    engine.attach_memory_repository(repo)
    try:
        report = asyncio.run(FieldValidationRunner(engine).run(field_mode=True))
        assert report.mode == "FIELD"
        assert report.field_validated is False
        assert report.to_dict()["field_validated"] is False
        mock_report = asyncio.run(FieldValidationRunner(engine, probes={"mock_sensor": lambda: {"status": "PASS", "detail": "simulated sensor only", "physical_evidence": False}}).run(field_mode=True))
        assert mock_report.field_validated is False
        attested_report = asyncio.run(FieldValidationRunner(engine, probes={"device_health": lambda: {"status": "PASS", "detail": "host verified device probe", "physical_evidence": True}}).run(field_mode=True))
        assert attested_report.field_validated is True
    finally:
        engine.close(); repo.close()


def test_companion_metrics_exclude_content():
    metrics = CompanionMetrics(clock=lambda: 10.0)
    metrics.record(status="COMPLETED", latency_s=0.25, channel="text")
    metrics.record(status="FAILED", latency_s=0.5, channel="voice", error_code="ASR_TIMEOUT:private details")
    snapshot = metrics.snapshot()
    rendered = json.dumps(snapshot)
    assert snapshot["conversation_count"] == 2
    assert snapshot["latency_p50_s"] == 0.25
    assert "private details" not in rendered
    assert "raw text" in snapshot["privacy"]


@pytest.mark.asyncio
async def test_companion_full_loop_uses_real_openai_compatible_gateway(config_manager, tmp_path):
    requests = []
    async def handler(request: httpx.Request):
        body = json.loads(request.content)
        requests.append(body)
        context = body["messages"][1]["content"]
        user_input = ""
        try:
            user_input = json.loads(context).get("user_input", "")
        except Exception:
            pass
        answer = "你好，我在听。" if len(requests) == 1 else "你之前提到过这件事，我们可以接着聊。"
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"response_text": answer}, ensure_ascii=False)}}]})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    path = tmp_path / "real-provider.yaml"
    path.write_text("provider:\n  endpoint_url: https://mock.invalid/v1/chat/completions\n  model: companion-model\n  timeout_s: 2\n", encoding="utf-8")
    engine = make_engine(config_manager, None, replay_mode=False)
    runtime = LLMRuntime.attach(engine, config_file=path, env_prefix=None, client=client)
    repo = SQLiteRepository(tmp_path / "companion-memory.db")
    engine.attach_memory_repository(repo)
    service = CompanionService(engine)
    try:
        first = await service.chat("你好", interaction_id="live")
        second = await service.chat("我们继续聊吧", interaction_id="live")
        assert first.status == CompanionStatus.COMPLETED
        assert second.status == CompanionStatus.COMPLETED
        assert second.response_text == "你之前提到过这件事，我们可以接着聊。"
        assert len(requests) == 2
        context_json = requests[1]["messages"][1]["content"].split("\n", 1)[1]
        assert json.loads(context_json)["purpose"] == "complex_dialogue"
        metrics = service.metrics.snapshot()
        assert metrics["conversation_count"] == 2
    finally:
        engine.close(); repo.close(); await runtime.aclose(); await client.aclose()


@pytest.mark.asyncio
async def test_evaluation_runner_reports_only_behavior_flags_not_transcripts(config_manager):
    engine = make_engine(config_manager, ReplayLLMAdapter({
        "simple_response": {"response_text": "我在听，你可以继续说。"},
    }))
    repo = SQLiteRepository(":memory:")
    engine.attach_memory_repository(repo)
    service = CompanionService(engine)
    from continuity_engine import CompanionEvaluationRunner, EvaluationCase, EvaluationTurn
    try:
        suite = [EvaluationCase("listen-first", (
            EvaluationTurn("我只想说说，不用建议", required_any=("听", "说"), forbidden_any=("第一步", "解决方案")),
        ), "Listening-first behavior")]
        report = await CompanionEvaluationRunner(service).run(suite, run_id="test-run")
        data = report.to_dict()
        assert data["turn_count"] == 1
        assert data["pass_rate"] == 1.0
        rendered = json.dumps(data, ensure_ascii=False)
        assert "我只想说说" not in rendered
        assert "我在听" not in rendered
    finally:
        engine.close(); repo.close()


def test_memory_index_batching_and_delete_remove_vector_rows(tmp_path):
    repo = SQLiteRepository(tmp_path / "index.db")
    try:
        for index in range(5):
            repo.put_memory(f"m{index}", "user_fact", {"summary": f"这是第 {index} 条测试记忆"}, [f"turn-{index}"], 1000 + index)
        retriever = HybridMemoryRetriever(repo, embedder=FeatureHashEmbedder(), clock=lambda: 2000.0)
        result = retriever.index_all(limit=20)
        assert result == {"indexed": 5, "skipped": 0, "failed": 0}
        repo.update_memory("m0", "user_fact", {"summary": "修正后的事实"}, 3000.0)
        assert repo.get_memory_embedding("m0") is None
        repo.set_memory_embedding("m0", "feature-hash-v1", FeatureHashEmbedder().embed("修正后的事实"), 3000.0)
        deleted = repo.delete_memory("m0", 4000.0)
        assert deleted["status"] == "DELETED"
        assert repo.get_memory_embedding("m0") is None
    finally:
        repo.close()


def test_conversation_retention_physically_deletes_expired_rows(tmp_path):
    now = [1000.0]
    store = SQLiteConversationStore(tmp_path / "retention.db", enabled=True, retention_days=1,
                                    clock=lambda: now[0])
    try:
        store.save_turn("s", "user", "old turn", 0, timestamp=1000.0)
        assert len(store.load_session("s")) == 1
        now[0] = 1000.0 + 2 * 86400.0
        assert store.load_session("s") == []
        with store._lock:
            count = store._conn.execute("SELECT COUNT(*) AS n FROM companion_turns").fetchone()["n"]
        assert count == 0
    finally:
        store.close()


def test_runtime_refuses_provider_replacement_without_explicit_rebuild(config_manager, tmp_path):
    engine = make_engine(config_manager, ReplayLLMAdapter({"simple_response": {"response_text": "ok"}}), replay_mode=False)
    path = tmp_path / "provider.yaml"
    path.write_text("provider:\n  endpoint_url: https://mock.invalid/v1/chat/completions\n  model: test\n", encoding="utf-8")
    try:
        with pytest.raises(RuntimeError, match="already has an LLM gateway"):
            LLMRuntime.attach(engine, config_file=path, env_prefix=None)
    finally:
        engine.close()


def test_release_version_is_exposed():
    import continuity_engine
    assert continuity_engine.__version__ == "0.25.1.dev0"
