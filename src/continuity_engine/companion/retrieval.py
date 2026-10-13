"""Hybrid lexical/vector memory retrieval with durable, optional embedding caches.

The built-in FeatureHashEmbedder is a deterministic, offline fallback with a small
cross-language synonym map; it is not a substitute for a trained semantic model. Hosts
can inject a local embedding model implementing `embed(text) -> Sequence[float]` and a
stable `model_id`. Stored vectors are model-scoped and discarded on correction/deletion.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import math
import re
import time
from typing import Any, Protocol, Sequence


class EmbeddingProvider(Protocol):
    model_id: str
    def embed(self, text: str) -> Sequence[float]: ...


_SYNONYMS: dict[str, tuple[str, ...]] = {
    "考试": ("测验", "考核", "test", "exam", "study"),
    "测验": ("考试", "考核", "test", "exam"),
    "累": ("疲惫", "疲劳", "困", "exhausted", "tired"),
    "疲惫": ("累", "疲劳", "困", "exhausted", "tired"),
    "压力": ("焦虑", "紧张", "stress", "anxiety"),
    "焦虑": ("压力", "紧张", "anxiety", "stress"),
    "喜欢": ("偏好", "爱好", "prefer", "like"),
    "偏好": ("喜欢", "爱好", "prefer", "like"),
    "工作": ("上班", "项目", "work", "job"),
    "项目": ("工作", "计划", "project", "work"),
    "猫": ("宠物", "小猫", "cat", "pet"),
    "狗": ("宠物", "小狗", "dog", "pet"),
    "搬家": ("新家", "住处", "move", "relocation"),
    "家人": ("父母", "家庭", "family", "parents"),
}


def _tokens(text: str) -> list[str]:
    lowered = text.casefold()
    tokens = re.findall(r"[a-z0-9_]{2,}", lowered)
    cjk = "".join(ch for ch in lowered if "\u3400" <= ch <= "\u9fff")
    tokens.extend(cjk[i:i + 2] for i in range(max(0, len(cjk) - 1)))
    tokens.extend(cjk)
    # One-way phrase cues are expanded deterministically; all expansions are still data,
    # never an instruction to update or overwrite a memory.
    for key, values in _SYNONYMS.items():
        if key in lowered:
            tokens.extend(values)
    return tokens


class FeatureHashEmbedder:
    """Dependency-free feature hashing encoder for offline baseline retrieval.

    Shared dimensions come from lexical features and an explicit small synonym map. It
    is useful for a smoke-test/degraded mode, not an assertion of human-level semantics.
    """
    model_id = "feature-hash-v1"

    def __init__(self, dimensions: int = 384):
        if isinstance(dimensions, bool) or not isinstance(dimensions, int) or not 64 <= dimensions <= 4096:
            raise ValueError("dimensions must be in [64,4096]")
        self.dimensions = dimensions

    def embed(self, text: str) -> list[float]:
        if not isinstance(text, str) or len(text) > 12000:
            raise ValueError("text must be a string up to 12000 characters")
        vector = [0.0] * self.dimensions
        tokens = _tokens(text)
        if not tokens:
            return vector
        counts = Counter(tokens)
        for token, count in counts.items():
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            weight = 1.0 + math.log1p(count)
            vector[index] += sign * weight
        norm = math.sqrt(sum(value * value for value in vector))
        if norm:
            vector = [value / norm for value in vector]
        return vector



class SentenceTransformerEmbedder:
    """Optional local semantic encoder backed by sentence-transformers.

    The model must already be available in the local cache when offline operation is
    required. No model is downloaded unless the underlying library is configured to do so.
    """
    def __init__(self, model_name: str, *, device: str = "cpu"):
        if not isinstance(model_name, str) or not model_name.strip() or len(model_name) > 300:
            raise ValueError("model_name must be non-empty and <= 300 characters")
        if device not in {"cpu", "cuda", "mps"}:
            raise ValueError("device must be cpu, cuda or mps")
        self.model_name = model_name.strip()
        self.device = device
        self.model_id = "sentence-transformers:" + self.model_name
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError("Install optional semantic dependencies: pip install 'continuity-engine[semantic]'") from exc
            self._model = SentenceTransformer(self.model_name, device=self.device)
        return self._model

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        if not isinstance(texts, (list, tuple)) or len(texts) > 10000:
            raise ValueError("texts must be a list/tuple with at most 10000 items")
        if any(not isinstance(text, str) or len(text) > 12000 for text in texts):
            raise ValueError("each text must be a string up to 12000 characters")
        vectors = self._load().encode(list(texts), normalize_embeddings=True, convert_to_numpy=True)
        results = [[float(value) for value in vector.tolist()] for vector in vectors]
        if any(not result or any(not math.isfinite(value) for value in result) for result in results):
            raise ValueError("semantic model produced an invalid vector")
        return results

def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(float(a) * float(b) for a, b in zip(left, right))
    norm_a = math.sqrt(sum(float(a) * float(a) for a in left))
    norm_b = math.sqrt(sum(float(b) * float(b) for b in right))
    if norm_a <= 0 or norm_b <= 0:
        return 0.0
    return max(-1.0, min(1.0, dot / (norm_a * norm_b)))


class HybridMemoryRetriever:
    """Bounded lexical + vector + recency ranking over a SQLiteRepository-like store.

    Cached vectors are loaded in bulk when the repository supports it. Cold-cache search
    only computes a bounded shortlist; call ``index_all`` during controlled maintenance
    to support semantic-only retrieval across the entire repository without per-turn
    embedding every memory. ``last_search_stats`` contains counts only, never content.
    """
    def __init__(self, repository: Any | None = None, *, embedder: EmbeddingProvider | None = None,
                 lexical_weight: float = 0.48, vector_weight: float = 0.45,
                 recency_weight: float = 0.07, clock=time.time, max_scan: int = 1000,
                 max_query_embeddings: int = 24):
        weights = (lexical_weight, vector_weight, recency_weight)
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)) or v < 0 for v in weights):
            raise ValueError("retrieval weights must be finite and non-negative")
        if sum(weights) <= 0:
            raise ValueError("at least one retrieval weight must be positive")
        if isinstance(max_scan, bool) or not isinstance(max_scan, int) or not 1 <= max_scan <= 10000:
            raise ValueError("max_scan must be in [1,10000]")
        if isinstance(max_query_embeddings, bool) or not isinstance(max_query_embeddings, int) or not 1 <= max_query_embeddings <= 256:
            raise ValueError("max_query_embeddings must be in [1,256]")
        self.repository = repository
        self.embedder = embedder or FeatureHashEmbedder()
        self.lexical_weight, self.vector_weight, self.recency_weight = map(float, weights)
        total = sum(weights)
        self.lexical_weight /= total; self.vector_weight /= total; self.recency_weight /= total
        self.clock = clock
        self.max_scan = max_scan
        self.max_query_embeddings = max_query_embeddings
        self.last_search_stats: dict[str, int | str] = {"rows_scanned": 0, "vectors_cached": 0, "vectors_computed": 0, "retrieval_mode": "not_run"}

    @staticmethod
    def _summary(row: dict[str, Any]) -> str:
        payload = row.get("payload", {})
        if isinstance(payload, dict):
            parts = [payload.get("summary"), payload.get("fact"), payload.get("topic"), payload.get("details")]
            return " ".join(x.strip() for x in parts if isinstance(x, str) and x.strip())[:4000]
        return ""

    @staticmethod
    def _lexical_score(query_tokens: set[str], text: str) -> float:
        document_tokens = set(_tokens(text))
        if not query_tokens or not document_tokens:
            return 0.0
        overlap = query_tokens & document_tokens
        # Precision/recall mix avoids lengthy JSON payloads winning by token count.
        precision = len(overlap) / len(document_tokens)
        recall = len(overlap) / len(query_tokens)
        if precision + recall == 0:
            return 0.0
        f1 = 2 * precision * recall / (precision + recall)
        return min(1.0, recall * 0.75 + f1 * 0.25)

    def _compute_vectors(self, candidates: list[tuple[dict[str, Any], str]], repository: Any) -> dict[str, list[float]]:
        """Compute only a bounded lexical/recent shortlist; bulk indexing handles the rest."""
        if not candidates:
            return {}
        model_id = str(getattr(self.embedder, "model_id", type(self.embedder).__name__))[:200]
        batcher = getattr(self.embedder, "embed_many", None)
        output: dict[str, list[float]] = {}
        try:
            raw_vectors = batcher([summary for _row, summary in candidates]) if callable(batcher) else [
                self.embedder.embed(summary) for _row, summary in candidates
            ]
            if len(raw_vectors) != len(candidates):
                raise ValueError("embedder returned mismatched vector count")
        except Exception:
            return output
        setter = getattr(repository, "set_memory_embedding", None)
        for (row, _summary), raw in zip(candidates, raw_vectors):
            try:
                vector = [float(value) for value in raw]
                if not 8 <= len(vector) <= 16384 or any(not math.isfinite(value) for value in vector):
                    continue
                memory_id = str(row.get("memory_id", ""))
                if not memory_id:
                    continue
                output[memory_id] = vector
                if callable(setter):
                    try:
                        setter(memory_id, model_id, vector, float(self.clock()))
                    except Exception:
                        pass
            except (TypeError, ValueError, OverflowError):
                continue
        return output

    def search(self, query: str, *, limit: int = 5, kinds: list[str] | None = None,
               repository: Any | None = None) -> list[dict[str, Any]]:
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            self.last_search_stats = {"rows_scanned": 0, "vectors_cached": 0, "vectors_computed": 0, "retrieval_mode": "empty_query"}
            return []
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit must be in [1,20]")
        repo = repository or self.repository
        if repo is None:
            return []
        list_method = getattr(repo, "list_memories", None)
        if not callable(list_method):
            search_method = getattr(repo, "search_memories", None)
            result = list(search_method(query, kinds=kinds, limit=limit)) if callable(search_method) else []
            self.last_search_stats = {"rows_scanned": len(result), "vectors_cached": 0, "vectors_computed": 0, "retrieval_mode": "repository_fallback"}
            return result
        rows = list_method(limit=self.max_scan)
        rows = [row for row in rows if isinstance(row, dict) and (not kinds or row.get("kind") in kinds)]
        qtokens = set(_tokens(query))
        qvector = None
        try:
            candidate_vector = [float(v) for v in self.embedder.embed(query)]
            if 8 <= len(candidate_vector) <= 16384 and all(math.isfinite(v) for v in candidate_vector):
                qvector = candidate_vector
        except Exception:
            pass
        now = float(self.clock())
        if not math.isfinite(now):
            now = time.time()

        prepared: list[dict[str, Any]] = []
        for row in rows:
            summary = self._summary(row)
            if not summary:
                continue
            age_days = max(0.0, now - float(row.get("created_at", now))) / 86400.0
            prepared.append({"row": row, "summary": summary,
                             "lexical": self._lexical_score(qtokens, summary),
                             "recency": math.exp(-age_days / 180.0)})

        model_id = str(getattr(self.embedder, "model_id", type(self.embedder).__name__))[:200]
        ids = [str(item["row"].get("memory_id", "")) for item in prepared if item["row"].get("memory_id")]
        cached_vectors: dict[str, list[float]] = {}
        batch_getter = getattr(repo, "get_memory_embeddings", None)
        if qvector is not None and callable(batch_getter):
            try:
                batch = batch_getter(ids, model_id=model_id)
                if isinstance(batch, dict):
                    for memory_id, value in batch.items():
                        vector = value.get("embedding") if isinstance(value, dict) else None
                        if isinstance(vector, list) and 8 <= len(vector) <= 16384 and all(
                                isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)) for v in vector):
                            cached_vectors[str(memory_id)] = [float(v) for v in vector]
            except Exception:
                cached_vectors = {}
        elif qvector is not None:
            getter = getattr(repo, "get_memory_embedding", None)
            if callable(getter):
                for memory_id in ids:
                    try:
                        cached = getter(memory_id, model_id=model_id)
                        vector = cached.get("embedding") if isinstance(cached, dict) else None
                        if isinstance(vector, list) and 8 <= len(vector) <= 16384 and all(
                                isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)) for v in vector):
                            cached_vectors[memory_id] = [float(v) for v in vector]
                    except Exception:
                        continue

        computed_vectors: dict[str, list[float]] = {}
        if qvector is not None:
            missing = [item for item in prepared if str(item["row"].get("memory_id", "")) not in cached_vectors]
            # Embedding every uncached memory on every chat turn made retrieval scale
            # linearly in expensive model calls. Search uses a bounded shortlist; hosts
            # can use index_all() to pre-index the complete memory set offline.
            missing.sort(key=lambda item: (-item["lexical"], -item["recency"], str(item["row"].get("memory_id", ""))))
            lexical_candidates = [item for item in missing if item["lexical"] >= 0.025]
            if not lexical_candidates:
                lexical_candidates = sorted(missing, key=lambda item: (-item["recency"], str(item["row"].get("memory_id", ""))))[:min(8, self.max_query_embeddings)]
            shortlist = lexical_candidates[:self.max_query_embeddings]
            computed_vectors = self._compute_vectors([(item["row"], item["summary"]) for item in shortlist], repo)

        scored: list[tuple[float, float, dict[str, Any]]] = []
        for item in prepared:
            row, summary = item["row"], item["summary"]
            lexical, recency = item["lexical"], item["recency"]
            memory_id = str(row.get("memory_id", ""))
            vector = cached_vectors.get(memory_id) or computed_vectors.get(memory_id)
            semantic = 0.0
            if qvector is not None and vector is not None and len(vector) == len(qvector):
                semantic = max(0.0, cosine_similarity(qvector, vector))
            score = self.lexical_weight * lexical + self.vector_weight * semantic + self.recency_weight * recency
            if lexical < 0.025 and semantic < 0.12:
                continue
            enriched = dict(row)
            enriched["retrieval_score"] = round(min(1.0, max(0.0, score)), 6)
            enriched["lexical_score"] = round(lexical, 6)
            enriched["semantic_score"] = round(semantic, 6)
            enriched["retrieval_method"] = "hybrid" if qvector is not None else "lexical_fallback"
            scored.append((score, float(row.get("created_at", 0.0)), enriched))
        scored.sort(key=lambda item: (-item[0], -item[1], str(item[2].get("memory_id", ""))))
        self.last_search_stats = {"rows_scanned": len(prepared), "vectors_cached": len(cached_vectors),
                                  "vectors_computed": len(computed_vectors), "retrieval_mode": "hybrid" if qvector is not None else "lexical_fallback"}
        return [row for _score, _created, row in scored[:limit]]

    def index_all(self, *, repository: Any | None = None, limit: int | None = None) -> dict[str, int]:
        """Build missing durable vectors in batches; intended for startup/offline jobs."""
        repo = repository or self.repository
        if repo is None or not callable(getattr(repo, "list_memories", None)):
            return {"indexed": 0, "skipped": 0, "failed": 0}
        max_items = self.max_scan if limit is None else limit
        if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 10000:
            raise ValueError("limit must be in [1,10000]")
        rows = repo.list_memories(limit=max_items)
        model_id = str(getattr(self.embedder, "model_id", type(self.embedder).__name__))[:200]
        getter = getattr(repo, "get_memory_embedding", None)
        setter = getattr(repo, "set_memory_embedding", None)
        missing: list[tuple[dict[str, Any], str]] = []
        skipped = 0
        for row in rows:
            summary = self._summary(row)
            mid = str(row.get("memory_id", ""))
            if not mid or not summary:
                skipped += 1
                continue
            cached = getter(mid, model_id=model_id) if callable(getter) else None
            if cached:
                skipped += 1
            else:
                missing.append((row, summary))
        indexed = failed = 0
        batcher = getattr(self.embedder, "embed_many", None)
        batch_size = 64 if callable(batcher) else 1
        for start in range(0, len(missing), batch_size):
            batch = missing[start:start + batch_size]
            try:
                vectors = batcher([summary for _row, summary in batch]) if callable(batcher) else [self.embedder.embed(batch[0][1])]
                if len(vectors) != len(batch):
                    raise ValueError("embedder returned mismatched vector count")
                for (row, _summary), vector in zip(batch, vectors):
                    try:
                        if callable(setter):
                            setter(str(row["memory_id"]), model_id, [float(v) for v in vector], float(self.clock()))
                        indexed += 1
                    except Exception:
                        failed += 1
            except Exception:
                failed += len(batch)
        return {"indexed": indexed, "skipped": skipped, "failed": failed}

    def invalidate(self, memory_id: str, *, repository: Any | None = None) -> None:
        repo = repository or self.repository
        delete = getattr(repo, "delete_memory_embedding", None)
        if callable(delete):
            delete(memory_id)
