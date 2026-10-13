# Provider drill report

- run_id: `b31cfa24-b60f-41f6-aa70-6a810d00cb58`
- started_at: 2026-10-13 16:35:09   completed_at: 2026-10-13 16:43:41
- duration_s: 512.559
- verdict: **PASS**

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
| reachable_at_start | True |
| preflight_latency_s | 1.6783 |
| preflight_status | COMPLETED |

## Verdicts

| check | result |
| --- | --- |
| output_contract_all_purposes | PASS |
| thinking_mode_disabled | PASS |
| long_conversation_no_hard_failures | PASS |
| memory_recall_across_turns | PASS |
| context_continuation | PASS |
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
| service_recovers_after_outage | PASS |

## Output-contract compatibility

| purpose | status | contract_ok | samples | violations | latency_s | first_error_code |
| --- | --- | --- | --- | --- | --- | --- |
| demand_understanding | COMPLETED | True | 1 | 0 | 4.3003 | None |
| simple_response | COMPLETED | True | 1 | 0 | 3.7942 | None |
| complex_dialogue | COMPLETED | True | 1 | 0 | 5.5506 | None |
| task_plan | COMPLETED | True | 1 | 0 | 2.5799 | None |
| multi_step_plan | COMPLETED | True | 1 | 0 | 6.8583 | None |
| memory_consolidation | COMPLETED | True | 1 | 0 | 1.7403 | None |
| provider_metadata | COMPLETED | True | 1 | 0 | 2.4261 | None |

## Conversation continuity

- turns: 60  completed: 59  degraded: 0  failed: 0
- pass_rate: 1.0  error_rate: 0.0
- latency p50/p95/max (s): 6.5895 / 14.4356 / 17.5581
- memory_recall_ok: True  context_continuation_ok: True  no_fabrication_ok: True

| turn | status | passed | latency_s | chars | error_code |
| --- | --- | --- | --- | --- | --- |
| 0 | MEMORY_SAVED | True | 0.0041 | 20 | None |
| 1 | COMPLETED | True | 3.0521 | 53 | None |
| 2 | COMPLETED | True | 6.9241 | 121 | None |
| 3 | COMPLETED | True | 4.7531 | 137 | None |
| 4 | COMPLETED | True | 2.1314 | 7 | None |
| 5 | COMPLETED | True | 9.2159 | 301 | None |
| 6 | COMPLETED | True | 3.8138 | 80 | None |
| 7 | COMPLETED | True | 6.384 | 140 | None |
| 8 | COMPLETED | True | 6.5895 | 201 | None |
| 9 | COMPLETED | True | 15.7673 | 626 | None |
| 10 | COMPLETED | True | 15.7708 | 417 | None |
| 11 | COMPLETED | True | 14.4356 | 405 | None |
| 12 | COMPLETED | True | 3.8483 | 82 | None |
| 13 | COMPLETED | True | 3.8148 | 68 | None |
| 14 | COMPLETED | True | 7.0568 | 155 | None |
| 15 | COMPLETED | True | 4.6379 | 71 | None |
| 16 | COMPLETED | True | 11.2715 | 229 | None |
| 17 | COMPLETED | True | 8.1888 | 98 | None |
| 18 | COMPLETED | True | 5.2231 | 76 | None |
| 19 | COMPLETED | True | 5.0231 | 114 | None |
| 20 | COMPLETED | True | 3.8965 | 76 | None |
| 21 | COMPLETED | True | 5.7441 | 155 | None |
| 22 | COMPLETED | True | 9.7904 | 251 | None |
| 23 | COMPLETED | True | 7.0168 | 179 | None |
| 24 | COMPLETED | True | 7.7977 | 280 | None |
| 25 | COMPLETED | True | 9.4462 | 198 | None |
| 26 | COMPLETED | True | 8.49 | 302 | None |
| 27 | COMPLETED | True | 4.9886 | 54 | None |
| 28 | COMPLETED | True | 3.5602 | 45 | None |
| 29 | COMPLETED | True | 3.9148 | 90 | None |
| 30 | COMPLETED | True | 8.9897 | 258 | None |
| 31 | COMPLETED | True | 9.5332 | 241 | None |
| 32 | COMPLETED | True | 6.1363 | 73 | None |
| 33 | COMPLETED | True | 9.2772 | 242 | None |
| 34 | COMPLETED | True | 6.2358 | 100 | None |
| 35 | COMPLETED | True | 5.997 | 155 | None |
| 36 | COMPLETED | True | 9.2335 | 190 | None |
| 37 | COMPLETED | True | 6.4085 | 102 | None |
| 38 | COMPLETED | True | 17.5581 | 437 | None |
| 39 | COMPLETED | True | 11.5745 | 313 | None |
| 40 | COMPLETED | True | 8.9442 | 247 | None |
| 41 | COMPLETED | True | 6.2516 | 88 | None |
| 42 | COMPLETED | True | 4.8971 | 121 | None |
| 43 | COMPLETED | True | 11.1729 | 79 | None |
| 44 | COMPLETED | True | 8.1161 | 176 | None |
| 45 | COMPLETED | True | 6.5517 | 124 | None |
| 46 | COMPLETED | True | 3.4735 | 50 | None |
| 47 | COMPLETED | True | 5.915 | 107 | None |
| 48 | COMPLETED | True | 4.5362 | 68 | None |
| 49 | COMPLETED | True | 6.8948 | 133 | None |
| 50 | COMPLETED | True | 9.7716 | 280 | None |
| 51 | COMPLETED | True | 12.7399 | 255 | None |
| 52 | COMPLETED | True | 12.4937 | 294 | None |
| 53 | COMPLETED | True | 9.991 | 210 | None |
| 54 | COMPLETED | True | 11.6184 | 323 | None |
| 55 | COMPLETED | True | 4.8682 | 54 | None |
| 56 | COMPLETED | True | 3.8263 | 45 | None |
| 57 | COMPLETED | True | 4.1899 | 82 | None |
| 58 | COMPLETED | True | 13.0567 | 165 | None |
| 59 | COMPLETED | True | 9.824 | 165 | None |

## Failure drills

### controlled_retry_recovers_from_transient_faults

- passed: True
- observed: {"attempts": 4, "error": null, "injected_faults": ["RATE_LIMIT(retry_after=1s)", "SERVER(503)", "CONNECT"], "rate_limit_waits_s": 1.0, "retries": 3, "retries_by_kind": {"CONNECT": 1, "RATE_LIMIT": 1, "SERVER": 1}, "retry_after_observed_s": 1.0, "retry_wait_s": 3.589847, "succeeded": true, "wall_s": 5.6793}

### authentication_failure_is_not_retried

- passed: True
- observed: {"attempts": 1, "failures_by_kind": {"AUTH": 1}, "injected_fault": "AUTH(401)", "no_fabricated_success": true, "raised": "ProviderAuthError", "retries": 0, "wall_s": 0.0002}

### side_effecting_request_is_never_replayed

- passed: True
- observed: {"attempts": 1, "injected_fault": "RATE_LIMIT(retry_after=0.2s)", "raised": "ProviderRateLimitError", "request_replay_safe": false, "retries": 0, "retries_suppressed_no_replay": 1}

### circuit_opens_during_outage_and_recovers

- passed: True
- observed: {"circuit_rejections": 1, "fail_fast_latency_s": 0.0012, "half_open_probe_succeeded": true, "network_fail_latency_s": 0.164, "outage_requests": 4, "probe_latency_s": 1.9054, "state_after_outage": "OPEN", "state_after_recovery": "CLOSED", "waited_for_recovery_s": 2.0583}

### client_deadline_is_bounded_and_client_still_usable

- passed: True
- observed: {"attempts": 2, "deadline_s": 1.0, "followup_call_succeeded": true, "followup_latency_s": 1.3401, "observed_timeout_s": 1.0039, "raised_timeout": true}

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
- attempts: 1
- turns_before_close: 4
- turns_after_restart: 6
- context_restored: True

## Provider outage and recovery

- outage_turns: 2
- outage_statuses: ["DEGRADED", "DEGRADED"]
- outage_used_disclosed_degradation: [true, true]
- failures_were_honest: True
- waited_for_circuit_recovery_s: 2.0685
- recovery_attempts: 1
- recovered: True
- recovery_status: COMPLETED
- recovery_latency_s: 1.7341

## Reliability layer totals

```json
{
  "session_gateway": {
    "circuit": {
      "consecutive_failures": 0,
      "failure_threshold": 3,
      "opened_events": 1,
      "recovery_timeout_s": 2.0,
      "rejected_while_open": 1,
      "state": "CLOSED"
    },
    "delegate": "FaultInjectingAdapter",
    "metrics": {
      "attempts": 71,
      "circuit_rejections": 1,
      "failures": 3,
      "failures_by_kind": {
        "SERVER": 3
      },
      "privacy": "counters only; no prompts, responses, or credentials",
      "rate_limit_waits_s": 0.0,
      "requests": 70,
      "retries": 2,
      "retries_by_kind": {
        "SERVER": 2
      },
      "retries_suppressed_no_replay": 0,
      "retry_after_observed_s": 0.0,
      "retry_wait_s": 1.367351,
      "successes": 68
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
    "retry_wait_s": 3.589847,
    "successes": 3
  }
}
```

## Provider token usage (no text retained)

```json
{
  "completion_tokens": 10187,
  "last": {
    "completion_tokens": 39,
    "model": "doubao-seed-2-1-pro-260915",
    "prompt_tokens": 943,
    "reasoning_tokens": 0,
    "total_tokens": 982
  },
  "prompt_tokens": 111493,
  "reasoning_tokens": 0,
  "responses": 68,
  "total_tokens": 121680
}
```

## Companion metrics

```json
{
  "channels": {
    "text": 64
  },
  "conversation_count": 64,
  "errors": {
    "DEGRADED_LOCAL_FALLBACK": 2
  },
  "latency_p50_s": 6.408452000003308,
  "latency_p95_s": 14.43556179990992,
  "latency_samples": 64,
  "privacy": "no raw text, prompts, credentials, or audio stored",
  "statuses": {
    "COMPLETED": 61,
    "DEGRADED": 2,
    "MEMORY_SAVED": 1
  },
  "uptime_s": 499.43296750006266
}
```

## Resource usage

```json
{
  "asyncio_tasks": {
    "last": 1.0,
    "max": 4.0,
    "mean": 2.9620187304890737,
    "min": 1.0
  },
  "note": "RSS is null when neither psutil nor a platform memory API is available.",
  "py_traced_bytes": {
    "last": 8143700.0,
    "max": 8912530.0,
    "mean": 7999439.020811655,
    "min": 9859.0
  },
  "py_traced_peak_bytes": 8967284,
  "rss_bytes": {
    "last": 74178560.0,
    "max": 78221312.0,
    "mean": 65994622.00208116,
    "min": 37416960.0
  },
  "sample_count": 1922,
  "threads": {
    "last": 1.0,
    "max": 1.0,
    "mean": 1.0,
    "min": 1.0
  }
}
```

_Privacy: this report contains statuses, timings, counters and boolean results only; no prompts, responses, audio or credentials._
