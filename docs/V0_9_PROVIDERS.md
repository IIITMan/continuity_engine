# v0.9 Capability Providers: Reminders, Weather and Knowledge

This document describes the v0.9 host integration. The providers are real adapters, but their external resources are only used when the host constructs and starts them; weather and knowledge HTTP are explicitly opt-in.

## 1. Reminder service

`ReminderScheduler` stores reminders in SQLite and runs an async background worker. A create request returns `OK` only after its `PENDING` row is committed. The worker claims due reminders atomically (`PENDING` -> `FIRING`), invokes the delivery adapter, then stores a terminal status.

Supported operations:

- `reminder.create`: parse a relative or supported absolute time, persist a reminder.
- `reminder.list`: list pending reminders plus `UNKNOWN` / `FAILED` records that may need attention.
- `reminder.cancel`: cancel a uniquely matched pending reminder by ID or text. Ambiguous matches are not cancelled.

Time expressions include forms such as `一分钟后`, `2秒后`, `半小时后`, `明天下午三点`, and ISO-like date/time values. Ambiguous time expressions are rejected rather than guessed. Default horizon is 30 days and the timezone comes from the request context / configured default.

`PlatformReminderDelivery` uses the existing OS desktop notification driver. The scheduler is a host-managed service: call `await bundle.start()` during application startup and `await bundle.aclose()` during orderly shutdown. A reminder's creation response means it was stored, not that the future notification was delivered already.

Delivery semantics are deliberately conservative. The scheduler writes `FIRING` before attempting an external notification. If the process restarts while a reminder is `FIRING`, the record becomes `UNKNOWN`, and is not automatically re-fired. External notification delivery cannot be made exactly-once across arbitrary process crashes. The UI should show `UNKNOWN` and allow the user/host to reconcile it.

The scheduler's background task requires the host's asyncio loop to keep running. Do not call `asyncio.run()` separately per user turn; run a long-lived application event loop. Run notifications only after checking the target desktop session and OS backend. On Linux, `notify-send` and a functioning desktop notification session are required; on macOS `osascript` is used; on Windows the adapter uses PowerShell/Windows Forms.

## 2. Open-Meteo weather provider

`OpenMeteoWeatherProvider` uses public Open-Meteo geocoding and forecast endpoints. It returns current conditions or a selected day/period forecast, including the source URL and provider timestamp. The request is resolved to coordinates; if a place is missing, not found, or ambiguous, the provider asks for clarification or reports a failure instead of guessing.

Set `capabilities.weather_enabled: true` or run the demo with `--enable-weather` to instantiate and use this provider. When disabled, it makes no network calls and weather routes fail closed. The provider uses bounded HTTP timeouts and maps timeout, HTTP, and malformed payload failures to explicit result statuses.

Before commercial or redistributed deployments, review Open-Meteo's current usage terms, attribution and rate limits. This implementation includes a provider source URL but is not a legal compliance certification.

## 3. Wikipedia knowledge provider

`WikipediaKnowledgeProvider` calls the MediaWiki API for a search and short plain-text extracts, returning the article title and canonical URL. Chinese text selects Chinese Wikipedia; otherwise the English edition is queried. Results are size-bounded and cached for a configurable TTL.

Enable `capabilities.knowledge_search_enabled: true` or pass `--enable-knowledge` to construct the provider. When wired, `KNOWLEDGE_QUERY` prefers `knowledge.search`; the provider returns source-backed extracts rather than silently treating model-generated text as retrieved fact. A host may optionally send the extracted content to an LLM for synthesis, while retaining and displaying the real source links.

Wikipedia results are not exhaustive or automatically authoritative. For high-stakes, current, or domain-specific questions, configure an authoritative retrieval source and use source-specific validation. The integration test suite uses `httpx.MockTransport`; it doesn't query Wikipedia live.

## 4. Wiring both providers

```python
config = ConfigManager.from_yaml("config/default.yaml").get()
# Optionally opt in to network services only after reviewing privacy / terms.
config["capabilities"]["weather_enabled"] = True
config["capabilities"]["knowledge_search_enabled"] = True

bundle = build_v09_provider_bundle(config)
await bundle.start()
try:
    router = build_request_router_from_config(config, providers=bundle.providers)
    dispatcher = RequestDispatcher(router.registry)
    # route/execute requests in the host's long-running asyncio loop
finally:
    await bundle.aclose()
```

The config bundle registers a provider only when the relevant feature flag is enabled. Reminder availability is dynamic and becomes true only after the scheduler starts with a healthy delivery backend. The app must surface `bundle.start_errors` and never tell the user a missing provider ran successfully.

## 5. Test policy

Unit and integration tests use fake reminder delivery and `httpx.MockTransport`. They verify data flow, persistence, routing and failure contracts without calling real HTTP endpoints or showing real notifications. Manual smoke tests on the target desktop are still required to verify an actual notification appears, time zone settings are correct, and provider terms/network policy are appropriate.
