# v0.25.1-dev — Context continuation regression fix

This iteration acts on the real Volcano Ark/Doubao acceptance observations, but it does
not claim a new live-provider latency result. Run the provider drill again with the same
model, account, host and authored conversation set to measure actual billed token and
latency changes.

## 1. Context budget

`CompanionService` now accepts an injectable `DialogueContextBudgeter`. Defaults:

- at most 700 estimated tokens for prior dialogue;
- at most 300 estimated tokens for retrieved memories;
- at most 8 prior messages; user messages have a 220-token ceiling and assistant messages have a tighter 120-token ceiling so assistant prose cannot evict user-stated goals;
- at most 3 memory summaries, capped at 240 characters each.

The current user input is provided separately as `user_input` and is no longer duplicated
inside `recent_dialogue`. The fitter walks newest-first and middle-trims any oversized message rather than stopping at the first oversized older message. It continues scanning older rows when a trimmed item still cannot fit. Assistant messages have their own lower per-message ceiling (120 estimated tokens by default), preserving budget for earlier user statements and goals. This fixes the live-drill regression where a long assistant reply caused all earlier context to be discarded.
Memory ranking scores are internal and are not forwarded into the strict model-view schema.

`estimate_tokens()` is a deterministic, conservative mixed CJK/Latin estimator. It is
not the provider tokenizer. Each successful turn reports `variable_context_estimated_tokens`
and trimming counters in `CompanionResult.metadata`; `CompanionMetrics.snapshot()` reports
P50/P95 of these approximate values without retaining text. Provider-reported token usage,
when available, is the authoritative billing measure.

Run an offline fixture comparison:

```bash
PYTHONPATH=src python examples/context_budget_benchmark.py
```

The checked-in authored fixture reduces estimated variable context from 1,389 to 862 tokens
(37.94%), while limiting prior dialogue from 12 to 8 messages and memories from 5 to 3. The
baseline duplicates the current utterance in its history; the optimized path passes the current
utterance only once in `user_input`.
This is a synthetic estimator result, not a live-provider token/latency result.

## 2. Retrieval work bounded per turn

`HybridMemoryRetriever` first scores lexical relevance, then bulk-loads cached vectors for
the active embedding model in a bounded number of SQLite queries. On a cold cache, it only
embeds up to `max_query_embeddings` (default 24) of the strongest lexical candidates; if
there are no lexical candidates, it bootstraps from a small recent shortlist. This prevents
every chat turn from computing and writing a vector for every uncached memory.

Use `index_all()` during controlled startup/maintenance to build complete semantic indexes.
Cold, not-yet-indexed memories still participate through lexical retrieval, but semantic-only
matches in older uncached memories may wait until offline indexing. The built-in feature-hash
embedder remains a deterministic fallback, not a trained semantic model.

`SQLiteRepository.get_memory_embeddings()` returns model-scoped vectors with a bounded batch
query. Explicit correction and deletion continue to invalidate stored vectors.

## 3. Transcript protection

`SQLiteConversationStore` still keeps transcript persistence disabled unless explicitly
enabled. When enabled:

- SQLite `secure_delete` is enabled;
- on POSIX systems, database, WAL and SHM files receive best-effort owner-only `0600` mode;
- a host-provided authenticated encryption cipher can be injected;
- `FernetTranscriptCipher` is available with `pip install 'continuity-engine[crypto]'`;
- an encryption mode/key ID is stored, and reopening with a different mode or key fails closed;
- existing plaintext stores are not silently converted in place. Migrate into a fresh encrypted
  store using a reviewed one-way export/import procedure.

Example:

```powershell
$env:CONTINUITY_TRANSCRIPT_FERNET_KEY = "<key from your OS secret store>"
python examples/real_provider_companion.py --persist-recent-dialogue --encrypt-transcripts
```

Do not store the key in source control, the SQLite file, or the report. Rotating a key requires
an explicit migration to a new store. File permissions are not encryption. The engine's separate
long-term memory/event database is owner-restricted on POSIX as a best effort, but is not
application-level encrypted by this release; use an encrypted OS volume or a separately reviewed
encrypted database backend for sensitive deployment data. WAL/backups and third-party provider
retention must be addressed by the host's privacy policy.

## 4. Real-provider acceptance follow-up

The previous live acceptance report remains evidence for its recorded model/account/host only.
After upgrading, repeat at least:

1. identical 60-turn authored run (compare provider `prompt_tokens`, P50/P95 and hard failures);
2. memory facts present in memory storage but absent from recent transcript, then recall them;
3. retrieval with a cold and warm vector cache, including `index_all()` maintenance;
4. authentication outage, rate-limit retry, 503, deadline, cancellation and circuit-recovery drills;
5. session persistence both disabled and enabled-with-cipher, including wrong-key/mode refusal;
6. quality review for facts retained after truncation and for ambiguous references older than the retained window.

Avoid embedding any raw prompt, response, API key, audio, or decrypted transcript in benchmark
reports. Report estimated context tokens separately from provider billed tokens.
