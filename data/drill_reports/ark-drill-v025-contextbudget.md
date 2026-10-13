# Provider drill report

- run_id: `c11a70ed-9596-40ab-af22-4377c6abc373`
- started_at: 2026-10-13 18:03:02   completed_at: 2026-10-13 18:10:00
- duration_s: 418.346
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
| reachable_at_start | True |
| preflight_latency_s | 2.2797 |
| preflight_status | COMPLETED |

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
| service_recovers_after_outage | PASS |

## Output-contract compatibility

| purpose | status | contract_ok | samples | violations | latency_s | first_error_code |
| --- | --- | --- | --- | --- | --- | --- |
| demand_understanding | COMPLETED | True | 1 | 0 | 3.4198 | None |
| simple_response | COMPLETED | True | 1 | 0 | 3.6649 | None |
| complex_dialogue | COMPLETED | True | 1 | 0 | 7.9596 | None |
| task_plan | COMPLETED | True | 1 | 0 | 2.8061 | None |
| multi_step_plan | COMPLETED | True | 1 | 0 | 6.4613 | None |
| memory_consolidation | COMPLETED | True | 1 | 0 | 0.8845 | None |
| provider_metadata | COMPLETED | True | 1 | 0 | 6.3162 | None |

## Conversation continuity

- turns: 60  completed: 59  degraded: 0  failed: 0
- pass_rate: 0.9833  error_rate: 0.0
- latency p50/p95/max (s): 5.171 / 11.1931 / 17.2581
- memory_recall_ok: True  context_continuation_ok: False  no_fabrication_ok: True

| turn | status | passed | latency_s | chars | error_code |
| --- | --- | --- | --- | --- | --- |
| 0 | MEMORY_SAVED | True | 0.0073 | 20 | None |
| 1 | COMPLETED | True | 4.7576 | 62 | None |
| 2 | COMPLETED | True | 6.7823 | 112 | None |
| 3 | COMPLETED | True | 3.7537 | 88 | None |
| 4 | COMPLETED | True | 1.9456 | 7 | None |
| 5 | COMPLETED | True | 5.7542 | 127 | None |
| 6 | COMPLETED | True | 4.667 | 102 | None |
| 7 | COMPLETED | True | 4.8541 | 116 | None |
| 8 | COMPLETED | True | 4.4608 | 126 | None |
| 9 | COMPLETED | True | 12.9225 | 241 | None |
| 10 | COMPLETED | True | 11.3905 | 317 | None |
| 11 | COMPLETED | True | 11.1931 | 371 | None |
| 12 | COMPLETED | False | 3.8157 | 62 | None |
| 13 | COMPLETED | True | 3.8539 | 69 | None |
| 14 | COMPLETED | True | 4.8571 | 92 | None |
| 15 | COMPLETED | True | 5.1054 | 42 | None |
| 16 | COMPLETED | True | 6.3214 | 122 | None |
| 17 | COMPLETED | True | 4.6551 | 71 | None |
| 18 | COMPLETED | True | 3.3037 | 59 | None |
| 19 | COMPLETED | True | 5.9016 | 75 | None |
| 20 | COMPLETED | True | 4.3805 | 57 | None |
| 21 | COMPLETED | True | 4.6448 | 90 | None |
| 22 | COMPLETED | True | 5.5119 | 141 | None |
| 23 | COMPLETED | True | 6.6489 | 127 | None |
| 24 | COMPLETED | True | 4.7758 | 110 | None |
| 25 | COMPLETED | True | 6.2357 | 158 | None |
| 26 | COMPLETED | True | 8.6616 | 170 | None |
| 27 | COMPLETED | True | 5.0504 | 53 | None |
| 28 | COMPLETED | True | 3.4059 | 44 | None |
| 29 | COMPLETED | True | 4.5375 | 64 | None |
| 30 | COMPLETED | True | 6.9705 | 129 | None |
| 31 | COMPLETED | True | 5.171 | 103 | None |
| 32 | COMPLETED | True | 3.721 | 65 | None |
| 33 | COMPLETED | True | 5.9171 | 128 | None |
| 34 | COMPLETED | True | 4.7364 | 144 | None |
| 35 | COMPLETED | True | 4.0602 | 105 | None |
| 36 | COMPLETED | True | 5.4613 | 134 | None |
| 37 | COMPLETED | True | 6.0461 | 71 | None |
| 38 | COMPLETED | True | 7.5635 | 196 | None |
| 39 | COMPLETED | True | 8.6065 | 194 | None |
| 40 | COMPLETED | True | 7.7523 | 178 | None |
| 41 | COMPLETED | True | 6.7782 | 94 | None |
| 42 | COMPLETED | True | 5.1634 | 123 | None |
| 43 | COMPLETED | True | 17.2581 | 112 | None |
| 44 | COMPLETED | True | 6.0254 | 135 | None |
| 45 | COMPLETED | True | 5.6659 | 78 | None |
| 46 | COMPLETED | True | 3.2925 | 66 | None |
| 47 | COMPLETED | True | 4.4465 | 85 | None |
| 48 | COMPLETED | True | 3.71 | 53 | None |
| 49 | COMPLETED | True | 4.5938 | 92 | None |
| 50 | COMPLETED | True | 5.9749 | 153 | None |
| 51 | COMPLETED | True | 7.5915 | 147 | None |
| 52 | COMPLETED | True | 7.5605 | 142 | None |
| 53 | COMPLETED | True | 5.7454 | 125 | None |
| 54 | COMPLETED | True | 9.1148 | 166 | None |
| 55 | COMPLETED | True | 4.2591 | 55 | None |
| 56 | COMPLETED | True | 3.12 | 44 | None |
| 57 | COMPLETED | True | 5.7588 | 66 | None |
| 58 | COMPLETED | True | 6.1844 | 153 | None |
| 59 | COMPLETED | True | 5.3637 | 105 | None |

## Failure drills

### controlled_retry_recovers_from_transient_faults

- passed: True
- observed: {"attempts": 4, "error": null, "injected_faults": ["RATE_LIMIT(retry_after=1s)", "SERVER(503)", "CONNECT"], "rate_limit_waits_s": 1.0, "retries": 3, "retries_by_kind": {"CONNECT": 1, "RATE_LIMIT": 1, "SERVER": 1}, "retry_after_observed_s": 1.0, "retry_wait_s": 3.367681, "succeeded": true, "wall_s": 4.978}

### authentication_failure_is_not_retried

- passed: True
- observed: {"attempts": 1, "failures_by_kind": {"AUTH": 1}, "injected_fault": "AUTH(401)", "no_fabricated_success": true, "raised": "ProviderAuthError", "retries": 0, "wall_s": 0.0002}

### side_effecting_request_is_never_replayed

- passed: True
- observed: {"attempts": 1, "injected_fault": "RATE_LIMIT(retry_after=0.2s)", "raised": "ProviderRateLimitError", "request_replay_safe": false, "retries": 0, "retries_suppressed_no_replay": 1}

### circuit_opens_during_outage_and_recovers

- passed: True
- observed: {"circuit_rejections": 1, "fail_fast_latency_s": 0.0008, "half_open_probe_succeeded": true, "network_fail_latency_s": 0.1739, "outage_requests": 4, "probe_latency_s": 2.319, "state_after_outage": "OPEN", "state_after_recovery": "CLOSED", "waited_for_recovery_s": 2.0666}

### client_deadline_is_bounded_and_client_still_usable

- passed: True
- observed: {"attempts": 2, "deadline_s": 1.0, "followup_call_succeeded": true, "followup_latency_s": 1.5555, "observed_timeout_s": 1.0102, "raised_timeout": true}

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
- waited_for_circuit_recovery_s: 1.9725
- recovery_attempts: 1
- recovered: True
- recovery_status: COMPLETED
- recovery_latency_s: 2.9458

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
      "attempts": 72,
      "circuit_rejections": 1,
      "failures": 3,
      "failures_by_kind": {
        "SERVER": 3
      },
      "privacy": "counters only; no prompts, responses, or credentials",
      "rate_limit_waits_s": 0.0,
      "requests": 71,
      "retries": 2,
      "retries_by_kind": {
        "SERVER": 2
      },
      "retries_suppressed_no_replay": 0,
      "retry_after_observed_s": 0.0,
      "retry_wait_s": 1.054329,
      "successes": 69
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
    "retry_wait_s": 3.367681,
    "successes": 3
  }
}
```

## Provider token usage (no text retained)

```json
{
  "completion_tokens": 7529,
  "last": {
    "completion_tokens": 55,
    "model": "doubao-seed-2-1-pro-260915",
    "prompt_tokens": 925,
    "reasoning_tokens": 0,
    "total_tokens": 980
  },
  "prompt_tokens": 92465,
  "reasoning_tokens": 0,
  "responses": 69,
  "total_tokens": 99994
}
```

## Companion metrics

```json
{
  "channels": {
    "text": 66
  },
  "conversation_count": 66,
  "errors": {
    "DEGRADED_LOCAL_FALLBACK": 2
  },
  "latency_p50_s": 5.105325399897993,
  "latency_p95_s": 11.193071500165388,
  "latency_samples": 66,
  "privacy": "no raw text, prompts, credentials, or audio stored",
  "statuses": {
    "COMPLETED": 63,
    "DEGRADED": 2,
    "MEMORY_SAVED": 1
  },
  "token_estimates_are_approximate": true,
  "uptime_s": 410.09394699987024,
  "variable_context_estimated_tokens_p50": 489,
  "variable_context_estimated_tokens_p95": 730,
  "variable_context_token_samples": 65
}
```

## Resource usage

```json
{
  "asyncio_tasks": {
    "last": 1.0,
    "max": 4.0,
    "mean": 2.941363926067559,
    "min": 1.0
  },
  "note": "RSS is null when neither psutil nor a platform memory API is available.",
  "py_traced_bytes": {
    "last": 8072498.0,
    "max": 8627780.0,
    "mean": 7904687.646908859,
    "min": 10070.0
  },
  "py_traced_peak_bytes": 8727667,
  "rss_bytes": {
    "last": 70299648.0,
    "max": 75116544.0,
    "mean": 65846024.64754621,
    "min": 37937152.0
  },
  "sample_count": 1569,
  "threads": {
    "last": 1.0,
    "max": 1.0,
    "mean": 1.0,
    "min": 1.0
  }
}
```

## Notes

- conversation probe 'context_continuation' did not verify within 3 attempts (status COMPLETED)

_Privacy: this report contains statuses, timings, counters and boolean results only; no prompts, responses, audio or credentials._
