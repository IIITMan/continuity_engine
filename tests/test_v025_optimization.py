from __future__ import annotations

import asyncio
import base64
import json
import os
import sqlite3
import stat

import pytest

from continuity_engine import (
    CompanionService, ConfigManager, ContinuityEngine, DialogueContextBudgeter,
    FeatureHashEmbedder, HybridMemoryRetriever, ReplayActionExecutor, ReplayClock,
    ReplayLLMAdapter, SQLiteConversationStore, SQLiteRepository, SessionStoreError,
    estimate_tokens,
)


class CountingEmbedder(FeatureHashEmbedder):
    model_id = "counting-feature-hash-v1"

    def __init__(self):
        super().__init__(dimensions=128)
        self.calls = 0

    def embed(self, text):
        self.calls += 1
        return super().embed(text)


class TestCipher:
    key_id = "test-key-id"

    def encrypt(self, plaintext: str) -> str:
        return "testcipher:v1:" + base64.urlsafe_b64encode(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        if not ciphertext.startswith("testcipher:v1:"):
            raise ValueError("wrong format")
        return base64.urlsafe_b64decode(ciphertext.split(":", 2)[2].encode()).decode()


def _engine(config_manager, adapter=None):
    from continuity_engine import LLMGateway
    gateway = LLMGateway(adapter or ReplayLLMAdapter({
        "simple_response": {"response_text": "我在听。"},
        "complex_dialogue": {"response_text": "我们可以接着聊。"},
    }), timeout_s=1.0, context_token_budgets=config_manager.get()["llm"]["context_token_budgets"])
    return ContinuityEngine(config_manager, clock=ReplayClock(mono=10.0, wall=1_800_000_000),
                            action_executor=ReplayActionExecutor(), llm_gateway=gateway, replay_mode=True)


def test_context_budget_is_bounded_and_keeps_most_recent_messages():
    budgeter = DialogueContextBudgeter(max_dialogue_tokens=260, max_memory_tokens=160,
                                       max_dialogue_messages=8, max_message_tokens=100,
                                       max_memory_items=3, max_memory_summary_chars=120)
    dialogue = [{"role": "user" if i % 2 == 0 else "assistant", "text": f"轮次{i}：" + ("今天我们讨论机器人记忆和考试准备。" * 12)}
                for i in range(12)]
    memories = [{"memory_id": f"m{i}", "kind": "user_fact", "summary": f"记忆{i}：" + ("用户正在准备考试并喜欢天文学。" * 10),
                 "source_refs": [f"turn-{i}"], "retrieval_score": .9} for i in range(8)]
    result = budgeter.prepare(dialogue, memories)
    assert result.dialogue
    assert result.dialogue[-1]["text"].startswith("轮次11")
    assert result.dialogue_tokens <= 260
    assert result.memory_tokens <= 160
    assert len(result.dialogue) <= 8
    assert len(result.memories) <= 3
    assert all(set(item) == {"memory_id", "kind", "summary", "source_refs"} for item in result.memories)
    assert result.dropped_messages > 0 and result.dropped_memories > 0
    assert estimate_tokens("我喜欢机器人") >= 5


def test_long_assistant_reply_does_not_drop_older_context_needed_for_continuation():
    """Regression from the live Ark drill: a long reply must not sever earlier turns."""
    budgeter = DialogueContextBudgeter(
        max_dialogue_tokens=700,
        max_memory_tokens=300,
        max_dialogue_messages=8,
        max_message_tokens=220,
        max_memory_items=3,
    )
    dialogue = [
        {"role": "user", "text": "我们之前讨论过近期要做的计划。"},
        {"role": "assistant", "text": "我记得，我们可以继续梳理计划。"},
        {"role": "user", "text": "我们这周的目标是准备马拉松。"},
        {"role": "assistant", "text": "这个目标可以拆成训练、恢复、装备和时间安排几个部分。" * 8},
        {"role": "user", "text": "我目前主要担心训练节奏和时间安排。"},
        {"role": "assistant", "text": "我们可以先从每周可用时间、当前跑量和恢复情况入手，再安排逐步增加的训练内容。" * 5},
        {"role": "user", "text": "另外我希望计划可以灵活调整。"},
        {"role": "assistant", "text": "可以，我们把训练安排作为可调整的建议，而不是固定不变的要求。" * 5},
        {"role": "user", "text": "能不能帮我总结一下目前讨论的重点？"},
        {"role": "assistant", "text": "我们讨论了目标拆分、训练节奏、可用时间、恢复情况，以及在现实安排变化时灵活调整计划。" * 4},
    ]

    fitted, tokens, dropped, truncated = budgeter.fit_dialogue(dialogue)
    assert tokens <= 700
    assert len(fitted) <= 8
    assert fitted[-1]["text"].startswith("我们讨论了目标拆分")
    assert any("我们这周的目标是准备马拉松" in item["text"] for item in fitted)
    assert truncated >= 1
    assert dropped == len(dialogue) - len(fitted)


def test_over_budget_old_message_is_trimmed_and_scan_continues_to_older_short_fact():
    budgeter = DialogueContextBudgeter(
        max_dialogue_tokens=700, max_memory_tokens=300,
        max_dialogue_messages=8, max_message_tokens=220,
    )
    fact = {"role": "user", "text": "本周目标是完成马拉松准备。"}
    dialogue = [fact] + [
        {"role": "assistant", "text": "今天的计划包括逐步训练、休息安排和饮食调整。" * 7},
        {"role": "user", "text": "我还要考虑工作日的时间。"},
        {"role": "assistant", "text": "我们可以根据你实际可用的时间逐日调整训练强度和训练量。" * 7},
        {"role": "user", "text": "请帮我概括一下。"},
    ]
    fitted, tokens, _, truncated = budgeter.fit_dialogue(dialogue)
    assert tokens <= 700
    assert any(item["text"] == fact["text"] for item in fitted)
    assert truncated >= 1


def test_assistant_prose_cannot_evict_user_goal_from_recent_context():
    budgeter = DialogueContextBudgeter(
        max_dialogue_tokens=700,
        max_memory_tokens=300,
        max_dialogue_messages=8,
        max_message_tokens=220,
        max_assistant_message_tokens=120,
    )
    dialogue = [
        {"role": "user", "text": "我们这周的目标是准备马拉松。"},
        {"role": "assistant", "text": "我们可以拆分训练、恢复、装备和安排。" * 12},
        {"role": "user", "text": "我担心工作日时间不够。"},
        {"role": "assistant", "text": "我们先看每天空闲时间，再安排训练强度和恢复日。" * 12},
        {"role": "user", "text": "计划还需要灵活调整。"},
        {"role": "assistant", "text": "可以按你每周的状态调整，并保留恢复时间。" * 12},
        {"role": "user", "text": "先把这些讨论记在上下文里。"},
        {"role": "assistant", "text": "好的，我们可以继续完善计划。"},
    ]
    fitted, tokens, _, truncated = budgeter.fit_dialogue(dialogue)

    assert tokens <= 700
    assert any(row["text"] == "我们这周的目标是准备马拉松。" for row in fitted)
    assert sum(1 for row in fitted if row["role"] == "assistant") >= 2
    assert truncated >= 1


@pytest.mark.asyncio
async def test_current_user_input_is_not_duplicated_in_dialogue_context(config_manager):
    adapter = ReplayLLMAdapter({
        "simple_response": {"response_text": "你好。"},
        "complex_dialogue": {"response_text": "我记得我们刚才聊过。"},
    })
    engine = _engine(config_manager, adapter)
    service = CompanionService(engine)
    try:
        await service.chat("之前讨论考试准备", interaction_id="ctx")
        result = await service.chat("这条新消息不应重复注入历史", interaction_id="ctx")
        payload = json.loads(adapter.calls[-1].context_json)
        rows = payload["recent_dialogue"]
        assert not any(row.get("text") == "这条新消息不应重复注入历史" for row in rows)
        assert any(row.get("text") == "之前讨论考试准备" for row in rows)
        assert result.metadata["variable_context_estimated_tokens"] >= 0
        assert result.metadata["dialogue_messages_in_context"] == len(rows)
    finally:
        engine.close()


def test_memory_search_computes_only_a_bounded_number_of_uncached_vectors(tmp_path):
    repo = SQLiteRepository(tmp_path / "mem.db")
    embedder = CountingEmbedder()
    try:
        for index in range(120):
            summary = ("用户正在准备一场重要考试，最近压力比较大。" if index == 72
                       else f"用户偏好内容类别{index}，偶尔阅读普通书籍。")
            repo.put_memory(f"m{index}", "user_fact", {"summary": summary}, [f"turn-{index}"], 1000.0 + index)
        retriever = HybridMemoryRetriever(repo, embedder=embedder, clock=lambda: 1200.0,
                                          max_scan=200, max_query_embeddings=8)
        result = retriever.search("我在准备的测验进展如何", limit=5)
        assert result and result[0]["memory_id"] == "m72"
        assert retriever.last_search_stats["rows_scanned"] == 120
        assert retriever.last_search_stats["vectors_computed"] <= 8
        # One query embedding plus a bounded shortlist, not one embedding per stored fact.
        assert embedder.calls <= 9
        assert repo.get_memory_embedding("m72", model_id=embedder.model_id) is not None
    finally:
        repo.close()


def test_bulk_memory_embedding_fetch_is_model_scoped(tmp_path):
    repo = SQLiteRepository(tmp_path / "embeddings.db")
    try:
        repo.put_memory("a", "fact", {"summary": "事实A"}, ["s1"], 1.0)
        repo.put_memory("b", "fact", {"summary": "事实B"}, ["s2"], 2.0)
        repo.set_memory_embedding("a", "model-a", [1.0] * 8, 3.0)
        repo.set_memory_embedding("b", "model-b", [2.0] * 8, 3.0)
        result = repo.get_memory_embeddings(["a", "b", "missing"], model_id="model-a")
        assert set(result) == {"a"}
        assert result["a"]["embedding"] == [1.0] * 8
    finally:
        repo.close()


def test_encrypted_transcript_store_hides_plaintext_and_restores(tmp_path):
    path = tmp_path / "encrypted.sqlite3"
    phrase = "这是绝对不能以明文写入数据库的私人内容"
    cipher = TestCipher()
    store = SQLiteConversationStore(path, enabled=True, cipher=cipher)
    try:
        assert store.save_turn("session-a", "user", phrase, 1)
        assert store.load_session("session-a")[0]["content"] == phrase
    finally:
        store.close()
    raw = path.read_bytes()
    assert phrase.encode("utf-8") not in raw
    conn = sqlite3.connect(path)
    try:
        stored = conn.execute("SELECT content FROM companion_turns").fetchone()[0]
        assert stored.startswith("testcipher:v1:")
    finally:
        conn.close()
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    reopened = SQLiteConversationStore(path, enabled=True, cipher=TestCipher())
    try:
        assert reopened.load_session("session-a")[0]["content"] == phrase
    finally:
        reopened.close()
    with pytest.raises(SessionStoreError, match="store_mode_mismatch"):
        SQLiteConversationStore(path, enabled=True)


def test_encryption_cannot_be_enabled_on_existing_legacy_plaintext_db(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    old = SQLiteConversationStore(path, enabled=True)
    old.save_turn("s", "user", "legacy plaintext", 1)
    old.close()
    with pytest.raises(SessionStoreError, match="store_mode_mismatch"):
        SQLiteConversationStore(path, enabled=True, cipher=TestCipher())
    # Existing data remains readable in its original explicitly plaintext mode.
    reopened = SQLiteConversationStore(path, enabled=True)
    try:
        assert reopened.load_session("s")[0]["content"] == "legacy plaintext"
    finally:
        reopened.close()


def test_companion_metrics_track_approximate_context_tokens_without_content():
    from continuity_engine import CompanionMetrics
    metrics = CompanionMetrics(clock=lambda: 10.0)
    metrics.record(status="COMPLETED", latency_s=1.0, channel="text", context_tokens=1400)
    metrics.record(status="COMPLETED", latency_s=1.5, channel="text", context_tokens=800)
    snapshot = metrics.snapshot()
    assert snapshot["variable_context_token_samples"] == 2
    assert snapshot["variable_context_estimated_tokens_p50"] == 800
    assert snapshot["variable_context_estimated_tokens_p95"] == 1400
    assert "我" not in json.dumps(snapshot, ensure_ascii=False)


@pytest.mark.asyncio
async def test_recalled_memories_satisfy_strict_state_view_schema(config_manager):
    adapter = ReplayLLMAdapter({
        "simple_response": {"response_text": "我会记住这件事。"},
        "complex_dialogue": {"response_text": "你之前说过正在准备考试。"},
    })
    engine = _engine(config_manager, adapter)
    repo = SQLiteRepository(":memory:")
    engine.attach_memory_repository(repo)
    service = CompanionService(engine)
    try:
        saved = await service.chat("记住我正在准备考试", interaction_id="memory-context")
        assert saved.status.value == "MEMORY_SAVED"
        recalled = await service.chat("考试准备怎么样了？", interaction_id="memory-context")
        assert recalled.status.value == "COMPLETED"
        payload = json.loads(adapter.calls[-1].context_json)
        for row in payload["relevant_memories"]:
            assert set(row).issubset({"memory_id", "summary", "source_refs", "kind"})
        assert recalled.metadata["memory_count"] >= 1
    finally:
        engine.close()
        repo.close()


def test_memory_repository_file_permissions_and_secure_delete(tmp_path):
    path = tmp_path / "personal-memories.sqlite3"
    repo = SQLiteRepository(path)
    try:
        assert int(repo._conn.execute("PRAGMA secure_delete").fetchone()[0]) == 1
        repo.put_memory("private-fact", "user_fact", {"summary": "我喜欢天文学"}, ["turn-1"], 1000.0)
    finally:
        repo.close()
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_fernet_transcript_cipher_round_trip_when_optional_dependency_is_available(tmp_path):
    cryptography = pytest.importorskip("cryptography")
    from cryptography.fernet import Fernet
    from continuity_engine import FernetTranscriptCipher
    key = Fernet.generate_key()
    cipher = FernetTranscriptCipher(key)
    path = tmp_path / "fernet.sqlite3"
    phrase = "隐私对话—不要明文写入 SQLite"
    store = SQLiteConversationStore(path, enabled=True, cipher=cipher)
    try:
        store.save_turn("private-session", "user", phrase, 1)
        assert store.load_session("private-session")[0]["content"] == phrase
    finally:
        store.close()
    assert phrase.encode("utf-8") not in path.read_bytes()


def test_real_provider_planner_prompts_encode_reported_hard_bounds_and_types():
    from continuity_engine.llm.openai_compatible import _PURPOSE_SYSTEM_PROMPTS
    task_prompt = _PURPOSE_SYSTEM_PROMPTS["task_plan"]
    multi_prompt = _PURPOSE_SYSTEM_PROMPTS["multi_step_plan"]
    assert "Numeric minimum and maximum values are hard bounds" in task_prompt
    assert "multiple sequential steps" in task_prompt
    assert "JSON list of strings" in multi_prompt
    assert "arguments must be an object" in multi_prompt
