# Hybrid memory retrieval and correction

`HybridMemoryRetriever` consumes a repository that implements `list_memories`, and optionally `get_memory_embedding` / `set_memory_embedding`. `SQLiteRepository` implements these methods with model-scoped vector storage. Retrieval returns provenance, lexical score, semantic score and the final ranking score.

## Default offline mode

```python
from continuity_engine import HybridMemoryRetriever, FeatureHashEmbedder, CompanionService

retriever = HybridMemoryRetriever(embedder=FeatureHashEmbedder())
companion = CompanionService(engine, memory_retriever=retriever)
```

`FeatureHashEmbedder` offers deterministic feature hashing plus a small curated synonym map. It helps the offline fallback associate some related phrases, but does not have the knowledge or semantic quality of a trained language embedding model.

## Optional local trained embeddings

Install `continuity-engine[semantic]`, make a SentenceTransformers-compatible model available locally, and inject it explicitly:

```python
from continuity_engine import HybridMemoryRetriever, SentenceTransformerEmbedder, CompanionService

encoder = SentenceTransformerEmbedder("YOUR-LOCALLY-AVAILABLE-EMBEDDING-MODEL", device="cpu")
retriever = HybridMemoryRetriever(embedder=encoder, max_scan=1000)
# Run this during controlled startup/offline maintenance, not in a latency-sensitive turn.
index_result = retriever.index_all(repository=engine.memory_repository, limit=1000)
companion = CompanionService(engine, memory_retriever=retriever)
```

The example model name is intentionally a placeholder: select and validate a model appropriate for the languages and privacy policy of the deployment. The optional dependency may download model artifacts unless local-only settings/cache are configured. No model is bundled in this repository.

## Correction and deletion

A correction targets a specific `memory_id`; it retains old provenance, adds the correction source, updates `fact_status`, and invalidates the old vector. Semantic similarity alone never decides which facts conflict. A host UI or explicit tool should show the selected memory summary before it is corrected. `forget_memory()` follows source-lineage deletion and removes the associated vector rows.

## Privacy

The retriever only projects short memory summaries to dialogue context; raw chat history is not promoted to long-term memory automatically. Credential-like strings are rejected by `MemoryLifecycle`. Retrieval scores are ranking signals, not confidence that a remembered fact is true.
