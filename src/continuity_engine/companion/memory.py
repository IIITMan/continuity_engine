"""Memory lifecycle helpers: explicit requests save; inferred memories stay candidates."""
from __future__ import annotations

from dataclasses import dataclass
import re
import time
import uuid
from typing import Any, Mapping

from .retrieval import HybridMemoryRetriever

_SECRET_MARKERS = (
    "密码", "password", "passcode", "验证码", "verification code", "one-time code",
    "api key", "api_key", "access token", "bearer token", "private key", "secret key", "私钥",
    "信用卡号", "credit card", "card number", "cvv", "ssn", "social security number", "银行账号", "bank account",
)

@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    candidate_id: str
    kind: str
    summary: str
    source_id: str
    confidence: float
    reason_code: str
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {"candidate_id": self.candidate_id, "kind": self.kind, "summary": self.summary,
                "source_id": self.source_id, "confidence": self.confidence,
                "reason_code": self.reason_code, "created_at": self.created_at}

class MemoryLifecycle:
    """Bounded in-memory candidate queue backed by the engine's existing repository.

    Raw conversation is not persisted. Only explicit "remember this" requests are
    written automatically; heuristic candidates require a caller/user approval.
    """
    def __init__(self, engine: Any, *, max_candidates: int = 100, clock=time.time,
                 retriever: HybridMemoryRetriever | None = None):
        if not 1 <= max_candidates <= 1000:
            raise ValueError("max_candidates must be in [1,1000]")
        self.engine = engine
        self.max_candidates = max_candidates
        self.clock = clock
        self.retriever = retriever or HybridMemoryRetriever(clock=clock)
        self._candidates: dict[str, MemoryCandidate] = {}

    @staticmethod
    def _sensitive(text: str) -> bool:
        lowered = text.casefold()
        return any(marker in lowered for marker in _SECRET_MARKERS)

    def explicit_request(self, text: str, source_id: str) -> str | None:
        match = re.match(r"^\s*(?:请)?(?:帮我)?(?:记住|记一下|记下来)[：:，,、\s]*(.{2,1800})\s*$", text)
        if not match:
            return None
        summary = match.group(1).strip()
        if self._sensitive(summary):
            return None
        # Return persistent ID only after engine/repository confirms the write.
        return self.engine.remember(summary, kind="user_explicit_note", source_ids=[source_id],
                                    payload={"summary": summary, "consent": "explicit_user_request",
                                             "confidence": 1.0, "updated_at": float(self.clock())})

    def propose(self, summary: str, *, source_id: str, kind: str = "user_fact",
                confidence: float = 0.65, reason_code: str = "explicit_statement") -> MemoryCandidate | None:
        summary = summary.strip() if isinstance(summary, str) else ""
        if not summary or len(summary) > 500 or self._sensitive(summary):
            return None
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be in [0,1]")
        candidate = MemoryCandidate(str(uuid.uuid4()), kind, summary, source_id, confidence,
                                    reason_code[:100], float(self.clock()))
        self._candidates[candidate.candidate_id] = candidate
        while len(self._candidates) > self.max_candidates:
            self._candidates.pop(next(iter(self._candidates)))
        return candidate

    def propose_from_utterance(self, text: str, *, source_id: str) -> MemoryCandidate | None:
        """Conservative extraction of explicit preference/ongoing-project wording.

        This does not call an LLM and never persists by itself.
        """
        if not isinstance(text, str) or len(text) > 12000:
            return None
        patterns = (
            (r"(?:我喜欢|我很喜欢|我最喜欢)([^。！!?？]{1,100})", "user_preference"),
            (r"(?:以后|今后)(?:请|都)?([^。！!?？]{2,100})", "interaction_preference"),
            (r"(?:我正在准备|我最近在做|我目前在做)([^。！!?？]{2,100})", "ongoing_interest"),
        )
        for pattern, kind in patterns:
            m = re.search(pattern, text)
            if m:
                candidate = self.propose(m.group(0).strip(), source_id=source_id, kind=kind,
                                         confidence=0.68, reason_code="explicit_language_pattern")
                if candidate:
                    return candidate
        return None

    def list_candidates(self) -> list[dict[str, Any]]:
        return [c.to_dict() for c in self._candidates.values()]

    def approve(self, candidate_id: str) -> str:
        candidate = self._candidates.get(candidate_id)
        if candidate is None:
            raise KeyError(candidate_id)
        if self.engine.memory_repository is None:
            raise RuntimeError("memory_repository_not_attached")
        memory_id = self.engine.remember(
            candidate.summary, kind=candidate.kind, source_ids=[candidate.source_id],
            payload={"summary": candidate.summary, "confidence": candidate.confidence,
                     "reason_code": candidate.reason_code, "consent": "approved_candidate",
                     "updated_at": candidate.created_at},
        )
        del self._candidates[candidate_id]
        return memory_id

    def reject(self, candidate_id: str) -> bool:
        return self._candidates.pop(candidate_id, None) is not None

    def recall(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        if self.engine.memory_repository is None:
            return []
        rows = self.retriever.search(query, limit=limit, repository=self.engine.memory_repository)
        safe: list[dict[str, Any]] = []
        for row in rows:
            payload = row.get("payload", {})
            summary = payload.get("summary", payload.get("fact", "")) if isinstance(payload, Mapping) else ""
            if not isinstance(summary, str) or not summary.strip():
                continue
            # Return only fields accepted by StateViewBuilder. Ranking scores remain
            # internal to the retriever; leaking the extra key caused strict JSON-view
            # validation failures whenever an actual stored memory was recalled.
            safe.append({"memory_id": str(row.get("memory_id", "")), "kind": str(row.get("kind", "unknown")),
                         "summary": summary[:1200], "source_refs": [str(x) for x in row.get("source_ids", [])[:10]]})
        return safe

    def correct(self, memory_id: str, corrected_summary: str, *, source_id: str,
                kind: str | None = None) -> dict[str, Any]:
        """Apply a user-directed correction to a specific memory ID.

        This operation is intentionally explicit: semantic similarity alone never
        decides that two user facts contradict each other.
        """
        if self.engine.memory_repository is None:
            raise RuntimeError("memory_repository_not_attached")
        summary = corrected_summary.strip() if isinstance(corrected_summary, str) else ""
        if not summary or len(summary) > 1800:
            raise ValueError("corrected_summary must contain 1..1800 characters")
        if self._sensitive(summary):
            raise ValueError("sensitive_credential_memory_rejected")
        if not isinstance(source_id, str) or not source_id or len(source_id) > 200:
            raise ValueError("source_id must contain 1..200 characters")
        rows = self.engine.memory_repository.list_memories(limit=1000)
        current = next((row for row in rows if row.get("memory_id") == memory_id), None)
        if current is None:
            raise KeyError(memory_id)
        payload = dict(current.get("payload", {}))
        payload["summary"] = summary
        payload["fact_status"] = "user_corrected"
        payload["corrected_at"] = float(self.clock())
        payload["correction_count"] = min(1000, int(payload.get("correction_count", 0) or 0) + 1)
        correction = getattr(self.engine.memory_repository, "correct_memory", None)
        if not callable(correction):
            raise RuntimeError("memory_repository_does_not_support_correction")
        correction(memory_id, kind or str(current.get("kind", "user_fact")), payload,
                   source_id, float(self.clock()))
        self.retriever.invalidate(memory_id, repository=self.engine.memory_repository)
        return {"status": "CORRECTED", "memory_id": memory_id, "source_id": source_id,
                "fact_status": "user_corrected"}

    def forget(self, memory_id: str) -> dict[str, Any]:
        result = self.engine.forget_memory(memory_id)
        for deleted_id in result.get("deleted_memory_ids", []) if isinstance(result, Mapping) else []:
            self.retriever.invalidate(str(deleted_id), repository=self.engine.memory_repository)
        return result
