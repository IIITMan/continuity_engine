# Provider drill report

- run_id: `0dc0f25e-c363-4522-a684-cfeb976fcda3`
- started_at: 2026-10-13 16:27:27   completed_at: 2026-10-13 16:28:49
- duration_s: 82.13
- verdict: **FAIL**

## Provider

| field | value |
| --- | --- |
| vendor | volcano-ark |
| endpoint_host | ark.cn-beijing.volces.com |
| endpoint_path | /api/v3/chat/completions |
| model | doubao-seed-2-1-pro-260915 |
| api_key_configured | True |
| streaming | False |
| thinking_disabled | True |
| reliability_layer | True |
| retry_policy | {"base_delay_s": 0.4, "jitter_ratio": 0.25, "max_attempts": 3, "max_delay_s": 4.0, "max_total_retry_wait_s": 10.0, "respect_retry_after": true, "retry_kinds": ["CONNECT", "RATE_LIMIT", "SERVER", "TIMEOUT"]} |
| gateway_timeout_s | 45.0000 |
| max_concurrent | 2 |
| action_executor | simulated |
| transcript_persistence | enabled (local plaintext SQLite) |

## Verdicts

| check | result |
| --- | --- |
| output_contract_all_purposes | PASS |
| thinking_mode_disabled | PASS |
| long_conversation_no_hard_failures | PASS |
| memory_recall_across_turns | PASS |
| context_continuation | **FAIL** |
| no_fabrication_without_live_data | PASS |
| controlled_retry_recovers_from_transient_faults | PASS |
| authentication_failure_is_not_retried | PASS |
| side_effecting_request_is_never_replayed | PASS |
| circuit_opens_during_outage_and_recovers | PASS |
| client_deadline_is_bounded_and_client_still_usable | PASS |
| cancellation_no_leaked_tasks | PASS |
| cancellation_was_inflight | PASS |
| control_lane_interrupts_inflight | PASS |
| restart_restores_context | PASS |
| outage_failures_were_honest | PASS |
| service_recovers_after_outage | **FAIL** |

## Output-contract compatibility

| purpose | status | contract_ok | samples | violations | latency_s | first_error_code |
| --- | --- | --- | --- | --- | --- | --- |
| demand_understanding | COMPLETED | True | 1 | 0 | 3.301 | None |
| simple_response | COMPLETED | True | 1 | 0 | 3.6301 | None |
| complex_dialogue | COMPLETED | True | 1 | 0 | 5.0951 | None |
| task_plan | COMPLETED | True | 1 | 0 | 2.4708 | None |
| multi_step_plan | COMPLETED | True | 1 | 0 | 4.9905 | None |
| memory_consolidation | COMPLETED | True | 1 | 0 | 1.5715 | None |
| provider_metadata | COMPLETED | True | 1 | 0 | 2.6478 | None |

## Conversation continuity

- turns: 60  completed: 4  degraded: 55  failed: 0
- pass_rate: 0.0833  error_rate: 0.0
- latency p50/p95/max (s): 0.0269 / 4.1114 / 7.9164
- memory_recall_ok: True  context_continuation_ok: False  no_fabrication_ok: True

| turn | status | passed | latency_s | chars | error_code |
| --- | --- | --- | --- | --- | --- |
| 0 | MEMORY_SAVED | True | 0.0083 | 20 | None |
| 1 | COMPLETED | True | 4.1114 | 76 | None |
| 2 | COMPLETED | True | 6.5834 | 141 | None |
| 3 | COMPLETED | True | 7.9164 | 128 | None |
| 4 | COMPLETED | True | 1.9882 | 7 | None |
| 5 | DEGRADED | False | 6.799 | 52 | DEGRADED_LOCAL_FALLBACK |
| 6 | DEGRADED | False | 0.033 | 52 | DEGRADED_LOCAL_FALLBACK |
| 7 | DEGRADED | False | 0.0377 | 36 | DEGRADED_LOCAL_FALLBACK |
| 8 | DEGRADED | False | 0.0411 | 52 | DEGRADED_LOCAL_FALLBACK |
| 9 | DEGRADED | False | 0.0394 | 36 | DEGRADED_LOCAL_FALLBACK |
| 10 | DEGRADED | False | 0.0339 | 36 | DEGRADED_LOCAL_FALLBACK |
| 11 | DEGRADED | False | 0.0331 | 52 | DEGRADED_LOCAL_FALLBACK |
| 12 | DEGRADED | False | 0.0374 | 52 | DEGRADED_LOCAL_FALLBACK |
| 13 | DEGRADED | False | 0.0335 | 36 | DEGRADED_LOCAL_FALLBACK |
| 14 | DEGRADED | False | 0.0404 | 36 | DEGRADED_LOCAL_FALLBACK |
| 15 | DEGRADED | False | 0.029 | 36 | DEGRADED_LOCAL_FALLBACK |
| 16 | DEGRADED | False | 0.0261 | 52 | DEGRADED_LOCAL_FALLBACK |
| 17 | DEGRADED | False | 0.0262 | 52 | DEGRADED_LOCAL_FALLBACK |
| 18 | DEGRADED | False | 0.0242 | 36 | DEGRADED_LOCAL_FALLBACK |
| 19 | DEGRADED | False | 0.027 | 52 | DEGRADED_LOCAL_FALLBACK |
| 20 | DEGRADED | False | 0.0264 | 52 | DEGRADED_LOCAL_FALLBACK |
| 21 | DEGRADED | False | 0.0259 | 36 | DEGRADED_LOCAL_FALLBACK |
| 22 | DEGRADED | False | 0.0298 | 36 | DEGRADED_LOCAL_FALLBACK |
| 23 | DEGRADED | False | 0.0255 | 52 | DEGRADED_LOCAL_FALLBACK |
| 24 | DEGRADED | False | 0.0339 | 52 | DEGRADED_LOCAL_FALLBACK |
| 25 | DEGRADED | False | 0.0306 | 52 | DEGRADED_LOCAL_FALLBACK |
| 26 | DEGRADED | False | 0.027 | 52 | DEGRADED_LOCAL_FALLBACK |
| 27 | DEGRADED | False | 0.0305 | 36 | DEGRADED_LOCAL_FALLBACK |
| 28 | DEGRADED | False | 0.0241 | 23 | DEGRADED_LOCAL_FALLBACK |
| 29 | DEGRADED | False | 0.0251 | 52 | DEGRADED_LOCAL_FALLBACK |
| 30 | DEGRADED | False | 0.0265 | 36 | DEGRADED_LOCAL_FALLBACK |
| 31 | DEGRADED | False | 0.0256 | 36 | DEGRADED_LOCAL_FALLBACK |
| 32 | DEGRADED | False | 0.0261 | 40 | DEGRADED_LOCAL_FALLBACK |
| 33 | DEGRADED | False | 0.0326 | 52 | DEGRADED_LOCAL_FALLBACK |
| 34 | DEGRADED | False | 0.0293 | 52 | DEGRADED_LOCAL_FALLBACK |
| 35 | DEGRADED | False | 0.0238 | 36 | DEGRADED_LOCAL_FALLBACK |
| 36 | DEGRADED | False | 0.0249 | 52 | DEGRADED_LOCAL_FALLBACK |
| 37 | DEGRADED | False | 0.0241 | 52 | DEGRADED_LOCAL_FALLBACK |
| 38 | DEGRADED | False | 0.0242 | 36 | DEGRADED_LOCAL_FALLBACK |
| 39 | DEGRADED | False | 0.0245 | 52 | DEGRADED_LOCAL_FALLBACK |
| 40 | DEGRADED | False | 0.0264 | 52 | DEGRADED_LOCAL_FALLBACK |
| 41 | DEGRADED | False | 0.0303 | 36 | DEGRADED_LOCAL_FALLBACK |
| 42 | DEGRADED | False | 0.0265 | 36 | DEGRADED_LOCAL_FALLBACK |
| 43 | DEGRADED | False | 0.0244 | 36 | DEGRADED_LOCAL_FALLBACK |
| 44 | DEGRADED | False | 0.0282 | 52 | DEGRADED_LOCAL_FALLBACK |
| 45 | DEGRADED | False | 0.026 | 52 | DEGRADED_LOCAL_FALLBACK |
| 46 | DEGRADED | False | 0.0261 | 36 | DEGRADED_LOCAL_FALLBACK |
| 47 | DEGRADED | False | 0.0257 | 52 | DEGRADED_LOCAL_FALLBACK |
| 48 | DEGRADED | False | 0.0301 | 52 | DEGRADED_LOCAL_FALLBACK |
| 49 | DEGRADED | False | 0.0383 | 36 | DEGRADED_LOCAL_FALLBACK |
| 50 | DEGRADED | False | 0.0352 | 36 | DEGRADED_LOCAL_FALLBACK |
| 51 | DEGRADED | False | 0.0257 | 52 | DEGRADED_LOCAL_FALLBACK |
| 52 | DEGRADED | False | 0.0272 | 52 | DEGRADED_LOCAL_FALLBACK |
| 53 | DEGRADED | False | 0.0306 | 52 | DEGRADED_LOCAL_FALLBACK |
| 54 | DEGRADED | False | 0.0259 | 52 | DEGRADED_LOCAL_FALLBACK |
| 55 | DEGRADED | False | 0.0244 | 36 | DEGRADED_LOCAL_FALLBACK |
| 56 | DEGRADED | False | 0.0242 | 23 | DEGRADED_LOCAL_FALLBACK |
| 57 | DEGRADED | False | 0.0252 | 52 | DEGRADED_LOCAL_FALLBACK |
| 58 | DEGRADED | False | 0.0269 | 36 | DEGRADED_LOCAL_FALLBACK |
| 59 | DEGRADED | False | 0.0257 | 36 | DEGRADED_LOCAL_FALLBACK |

## Failure drills

### controlled_retry_recovers_from_transient_faults

- passed: True
- observed: {"attempts": 4, "error": null, "injected_faults": ["RATE_LIMIT(retry_after=1s)", "SERVER(503)", "CONNECT"], "rate_limit_waits_s": 1.0, "retries": 3, "retries_by_kind": {"CONNECT": 1, "RATE_LIMIT": 1, "SERVER": 1}, "retry_after_observed_s": 1.0, "retry_wait_s": 3.396774, "succeeded": true, "wall_s": 5.1947}

### authentication_failure_is_not_retried

- passed: True
- observed: {"attempts": 1, "failures_by_kind": {"AUTH": 1}, "injected_fault": "AUTH(401)", "no_fabricated_success": true, "raised": "ProviderAuthError", "retries": 0, "wall_s": 0.0004}

### side_effecting_request_is_never_replayed

- passed: True
- observed: {"attempts": 1, "injected_fault": "RATE_LIMIT(retry_after=0.2s)", "raised": "ProviderRateLimitError", "request_replay_safe": false, "retries": 0, "retries_suppressed_no_replay": 1}

### circuit_opens_during_outage_and_recovers

- passed: True
- observed: {"circuit_rejections": 1, "fail_fast_latency_s": 0.0006, "half_open_probe_succeeded": true, "network_fail_latency_s": 0.1651, "outage_requests": 4, "probe_latency_s": 2.0159, "state_after_outage": "OPEN", "state_after_recovery": "CLOSED", "waited_for_recovery_s": 2.099}

### client_deadline_is_bounded_and_client_still_usable

- passed: True
- observed: {"attempts": 2, "deadline_s": 1.0, "followup_call_succeeded": true, "followup_latency_s": 2.1233, "observed_timeout_s": 1.0126, "raised_timeout": true}

## Cancellation and interruption

- mode: asyncio task cancellation mid-flight plus control-lane STOP (injected slow fault keeps both requests in flight)
- request_inflight_when_cancelled: True
- control_request_inflight_when_stopped: True
- tasks_before: 2
- tasks_after_cancel: 2
- tasks_final: 2
- no_leaked_tasks: True
- cancel_raised: CancelledError
- control_stop_status: CONTROL_ACCEPTED
- control_outcome_status: STALE
- control_interrupted: True

## Restart and recovery

- recall_after_restart_ok: True
- status: COMPLETED
- attempts: 2
- turns_before_close: 4
- turns_after_restart: 8
- context_restored: True

## Provider outage and recovery

- outage_turns: 2
- outage_statuses: ["DEGRADED", "DEGRADED"]
- outage_used_disclosed_degradation: [true, true]
- failures_were_honest: True
- waited_for_circuit_recovery_s: 1.5388
- recovery_attempts: 3
- recovered: False
- recovery_status: DEGRADED
- recovery_latency_s: 2.5448

## Reliability layer totals

```json
{
  "session_gateway": {
    "circuit": {
      "consecutive_failures": 6,
      "failure_threshold": 3,
      "opened_events": 4,
      "recovery_timeout_s": 2.0,
      "rejected_while_open": 59,
      "state": "OPEN"
    },
    "delegate": "FaultInjectingAdapter",
    "metrics": {
      "attempts": 16,
      "circuit_rejections": 59,
      "failures": 6,
      "failures_by_kind": {
        "CONNECT": 5,
        "SERVER": 1
      },
      "privacy": "counters only; no prompts, responses, or credentials",
      "rate_limit_waits_s": 0.0,
      "requests": 70,
      "retries": 5,
      "retries_by_kind": {
        "CONNECT": 4,
        "SERVER": 1
      },
      "retries_suppressed_no_replay": 0,
      "retry_after_observed_s": 0.0,
      "retry_wait_s": 2.507926,
      "successes": 10
    },
    "policy": {
      "base_delay_s": 0.4,
      "jitter_ratio": 0.25,
      "max_attempts": 3,
      "max_delay_s": 4.0,
      "max_total_retry_wait_s": 10.0,
      "respect_retry_after": true,
      "retry_kinds": [
        "CONNECT",
        "RATE_LIMIT",
        "SERVER",
        "TIMEOUT"
      ]
    }
  },
  "transport_drills": {
    "attempts": 12,
    "circuit_rejections": 1,
    "failures": 8,
    "failures_by_kind": {
      "AUTH": 1,
      "CONNECT": 4,
      "RATE_LIMIT": 2,
      "SERVER": 1
    },
    "privacy": "counters only; no prompts, responses, or credentials",
    "rate_limit_waits_s": 1.0,
    "requests": 10,
    "retries": 3,
    "retries_by_kind": {
      "CONNECT": 1,
      "RATE_LIMIT": 1,
      "SERVER": 1
    },
    "retries_suppressed_no_replay": 1,
    "retry_after_observed_s": 1.0,
    "retry_wait_s": 3.396774,
    "successes": 3
  }
}
```

## Provider token usage (no text retained)

```json
{
  "completion_tokens": 900,
  "last": {
    "completion_tokens": 10,
    "model": "doubao-seed-2-1-pro-260915",
    "prompt_tokens": 1227,
    "reasoning_tokens": 0,
    "total_tokens": 1237
  },
  "prompt_tokens": 8232,
  "reasoning_tokens": 0,
  "responses": 10,
  "total_tokens": 9132
}
```

## Companion metrics

```json
{
  "channels": {
    "text": 65
  },
  "conversation_count": 65,
  "errors": {
    "DEGRADED_LOCAL_FALLBACK": 60
  },
  "latency_p50_s": 0.02692499989643693,
  "latency_p95_s": 4.111348900012672,
  "latency_samples": 65,
  "privacy": "no raw text, prompts, credentials, or audio stored",
  "statuses": {
    "COMPLETED": 4,
    "DEGRADED": 60,
    "MEMORY_SAVED": 1
  },
  "uptime_s": 75.28888060012832
}
```

## Resource usage

```json
{
  "asyncio_tasks": {
    "last": 1.0,
    "max": 4.0,
    "mean": 2.788273615635179,
    "min": 1.0
  },
  "note": "RSS is null when neither psutil nor a platform memory API is available.",
  "py_traced_bytes": {
    "last": 7713290.0,
    "max": 8069887.0,
    "mean": 7497667.674267101,
    "min": 9910.0
  },
  "py_traced_peak_bytes": 8145643,
  "rss_bytes": {
    "last": 69357568.0,
    "max": 69423104.0,
    "mean": 65274189.5504886,
    "min": 38035456.0
  },
  "sample_count": 307,
  "threads": {
    "last": 2.0,
    "max": 2.0,
    "mean": 1.3127035830618892,
    "min": 1.0
  }
}
```

## Notes

- the provider was still not answering after the injected outage ended; the engine degraded honestly, so 'recovery' could not be observed in this run

_Privacy: this report contains statuses, timings, counters and boolean results only; no prompts, responses, audio or credentials._
