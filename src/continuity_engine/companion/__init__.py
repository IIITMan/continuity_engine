"""High-level companion conversation orchestration."""
from .emotion import EmotionContinuity, EmotionEstimate
from .memory import MemoryCandidate, MemoryLifecycle
from .retrieval import EmbeddingProvider, FeatureHashEmbedder, HybridMemoryRetriever, SentenceTransformerEmbedder, cosine_similarity
from .session_store import SQLiteConversationStore, SessionStoreError, TranscriptCipher, FernetTranscriptCipher
from .observability import CompanionMetrics
from .context_budget import ContextBudgetResult, DialogueContextBudgeter, estimate_tokens
from .evaluation import EvaluationCase, EvaluationReport, EvaluationTurn, CompanionEvaluationRunner
from .service import CompanionService, CompanionResult, CompanionStatus

__all__ = ["EmotionContinuity", "EmotionEstimate", "MemoryCandidate", "MemoryLifecycle",
           "EmbeddingProvider", "FeatureHashEmbedder", "HybridMemoryRetriever", "SentenceTransformerEmbedder",
           "cosine_similarity", "SQLiteConversationStore", "SessionStoreError", "TranscriptCipher", "FernetTranscriptCipher", "CompanionMetrics", "ContextBudgetResult", "DialogueContextBudgeter", "estimate_tokens",
           "EvaluationCase", "EvaluationReport", "EvaluationTurn", "CompanionEvaluationRunner",
           "CompanionService", "CompanionResult", "CompanionStatus"]
