# Provider drill report

- run_id: `4cb21cb6-ca6a-467a-8007-f408bba66b40`
- started_at: 2026-10-13 15:59:24   completed_at: 2026-10-13 16:07:15
- duration_s: 471.248
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
| output_contract_all_purposes | **FAIL** |
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
| control_lane_interrupts_inflight | PASS |
| restart_restores_context | PASS |
| outage_failures_were_honest | PASS |
| service_recovers_after_outage | PASS |

## Output-contract compatibility

| purpose | status | contract_ok | samples | violations | latency_s | first_error_code |
| --- | --- | --- | --- | --- | --- | --- |
| demand_understanding | COMPLETED | True | 1 | 0 | 2.9991 | None |
| simple_response | COMPLETED | True | 1 | 0 | 3.6103 | None |
| complex_dialogue | COMPLETED | True | 1 | 0 | 5.2536 | None |
| task_plan | REJECTED | False | 3 | 3 | 1.9418 | INVALID_TASK_ACTION_ARGS |
| multi_step_plan | COMPLETED | True | 1 | 0 | 5.0489 | None |
| memory_consolidation | COMPLETED | True | 1 | 0 | 1.1681 | None |
| provider_metadata | COMPLETED | True | 1 | 0 | 2.2879 | None |

## Conversation continuity

- turns: 60  completed: 59  degraded: 0  failed: 0
- pass_rate: 1.0  error_rate: 0.0
- latency p50/p95/max (s): 6.6644 / 10.8696 / 13.6349
- memory_recall_ok: True  context_continuation_ok: True  no_fabrication_ok: True

| turn | status | passed | latency_s | chars | error_code |
| --- | --- | --- | --- | --- | --- |
| 0 | MEMORY_SAVED | True | 0.0104 | 20 | None |
| 1 | COMPLETED | True | 3.0643 | 62 | None |
| 2 | COMPLETED | True | 8.5216 | 122 | None |
| 3 | COMPLETED | True | 5.3178 | 119 | None |
| 4 | COMPLETED | True | 2.4774 | 7 | None |
| 5 | COMPLETED | True | 7.2602 | 168 | None |
| 6 | COMPLETED | True | 4.4269 | 97 | None |
| 7 | COMPLETED | True | 9.6948 | 139 | None |
| 8 | COMPLETED | True | 9.1586 | 158 | None |
| 9 | COMPLETED | True | 10.218 | 276 | None |
| 10 | COMPLETED | True | 10.2449 | 216 | None |
| 11 | COMPLETED | True | 9.7786 | 295 | None |
| 12 | COMPLETED | True | 3.9085 | 97 | None |
| 13 | COMPLETED | True | 5.8976 | 81 | None |
| 14 | COMPLETED | True | 6.7446 | 128 | None |
| 15 | COMPLETED | True | 10.6439 | 107 | None |
| 16 | COMPLETED | True | 7.9071 | 167 | None |
| 17 | COMPLETED | True | 6.0985 | 85 | None |
| 18 | COMPLETED | True | 4.4967 | 76 | None |
| 19 | COMPLETED | True | 5.2186 | 102 | None |
| 20 | COMPLETED | True | 4.4624 | 68 | None |
| 21 | COMPLETED | True | 7.479 | 119 | None |
| 22 | COMPLETED | True | 10.2281 | 156 | None |
| 23 | COMPLETED | True | 9.3535 | 196 | None |
| 24 | COMPLETED | True | 11.3687 | 320 | None |
| 25 | COMPLETED | True | 7.2741 | 193 | None |
| 26 | COMPLETED | True | 10.546 | 416 | None |
| 27 | COMPLETED | True | 4.1526 | 54 | None |
| 28 | COMPLETED | True | 6.1552 | 45 | None |
| 29 | COMPLETED | True | 6.6644 | 82 | None |
| 30 | COMPLETED | True | 7.8762 | 176 | None |
| 31 | COMPLETED | True | 5.8438 | 139 | None |
| 32 | COMPLETED | True | 4.7281 | 75 | None |
| 33 | COMPLETED | True | 5.7823 | 152 | None |
| 34 | COMPLETED | True | 5.2551 | 111 | None |
| 35 | COMPLETED | True | 9.3229 | 132 | None |
| 36 | COMPLETED | True | 5.3181 | 188 | None |
| 37 | COMPLETED | True | 5.3715 | 71 | None |
| 38 | COMPLETED | True | 13.6349 | 402 | None |
| 39 | COMPLETED | True | 10.7594 | 371 | None |
| 40 | COMPLETED | True | 9.3828 | 304 | None |
| 41 | COMPLETED | True | 4.057 | 69 | None |
| 42 | COMPLETED | True | 12.6544 | 103 | None |
| 43 | COMPLETED | True | 6.0551 | 98 | None |
| 44 | COMPLETED | True | 8.5473 | 177 | None |
| 45 | COMPLETED | True | 5.229 | 69 | None |
| 46 | COMPLETED | True | 4.1796 | 54 | None |
| 47 | COMPLETED | True | 4.7226 | 102 | None |
| 48 | COMPLETED | True | 4.6181 | 64 | None |
| 49 | COMPLETED | True | 7.3519 | 129 | None |
| 50 | COMPLETED | True | 5.7943 | 161 | None |
| 51 | COMPLETED | True | 8.5282 | 192 | None |
| 52 | COMPLETED | True | 8.3101 | 202 | None |
| 53 | COMPLETED | True | 7.2795 | 190 | None |
| 54 | COMPLETED | True | 10.8696 | 275 | None |
| 55 | COMPLETED | True | 3.9758 | 57 | None |
| 56 | COMPLETED | True | 3.0765 | 44 | None |
| 57 | COMPLETED | True | 5.1555 | 91 | None |
| 58 | COMPLETED | True | 7.7084 | 199 | None |
| 59 | COMPLETED | True | 7.482 | 219 | None |

## Failure drills

### controlled_retry_recovers_from_transient_faults

- passed: True
- observed: {"attempts": 4, "error": null, "injected_faults": ["RATE_LIMIT(retry_after=1s)", "SERVER(503)", "CONNECT"], "rate_limit_waits_s": 1.0, "retries": 3, "retries_by_kind": {"CONNECT": 1, "RATE_LIMIT": 1, "SERVER": 1}, "retry_after_observed_s": 1.0, "retry_wait_s": 3.184544, "succeeded": true, "wall_s": 4.8229}

### authentication_failure_is_not_retried

- passed: True
- observed: {"attempts": 1, "failures_by_kind": {"AUTH": 1}, "injected_fault": "AUTH(401)", "no_fabricated_success": true, "raised": "ProviderAuthError", "retries": 0, "wall_s": 0.0003}

### side_effecting_request_is_never_replayed

- passed: True
- observed: {"attempts": 1, "injected_fault": "RATE_LIMIT(retry_after=0.2s)", "raised": "ProviderRateLimitError", "request_replay_safe": false, "retries": 0, "retries_suppressed_no_replay": 1}

### circuit_opens_during_outage_and_recovers

- passed: True
- observed: {"circuit_rejections": 1, "fail_fast_latency_s": 0.0005, "half_open_probe_succeeded": true, "network_fail_latency_s": 0.1592, "outage_requests": 4, "probe_latency_s": 1.6331, "state_after_outage": "OPEN", "state_after_recovery": "CLOSED", "waited_for_recovery_s": 2.0929}

### client_deadline_is_bounded_and_client_still_usable

- passed: True
- observed: {"attempts": 2, "deadline_s": 1.0, "followup_call_succeeded": true, "followup_latency_s": 2.4571, "observed_timeout_s": 1.0072, "raised_timeout": true}

## Cancellation and interruption

- mode: asyncio task cancellation mid-flight plus control-lane STOP
- tasks_before: 2
- tasks_after_cancel: 2
- tasks_final: 2
- no_leaked_tasks: True
- cancel_raised: CancelledError
- control_stop_status: CONTROL_ACCEPTED
- control_interrupted: True

## Restart and recovery

- recall_after_restart_ok: True
- status: COMPLETED
- turns_before_close: 4
- turns_after_restart: 6
- context_restored: True

## Provider outage and recovery

- outage_turns: 2
- outage_statuses: ["DEGRADED", "DEGRADED"]
- outage_used_disclosed_degradation: [true, true]
- failures_were_honest: True
- waited_for_circuit_recovery_s: 1.9909
- recovered: True
- recovery_status: COMPLETED
- recovery_latency_s: 1.7226

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
      "retry_wait_s": 1.200809,
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
    "retry_wait_s": 3.184544,
    "successes": 3
  }
}
```

## Provider token usage (no text retained)

```json
{
  "completion_tokens": 8731,
  "last": {
    "completion_tokens": 39,
    "model": "doubao-seed-2-1-pro-260915",
    "prompt_tokens": 943,
    "reasoning_tokens": 0,
    "total_tokens": 982
  },
  "prompt_tokens": 102701,
  "reasoning_tokens": 0,
  "responses": 68,
  "total_tokens": 111432
}
```

## Companion metrics

```json
{
  "channels": {
    "text": 63
  },
  "conversation_count": 63,
  "errors": {
    "DEGRADED_LOCAL_FALLBACK": 2
  },
  "latency_p50_s": 6.1551640999969095,
  "latency_p95_s": 10.86957719991915,
  "latency_samples": 63,
  "privacy": "no raw text, prompts, credentials, or audio stored",
  "statuses": {
    "COMPLETED": 60,
    "DEGRADED": 2,
    "MEMORY_SAVED": 1
  },
  "uptime_s": 462.6034630998038
}
```

## Resource usage

```json
{
  "asyncio_tasks": {
    "last": 1.0,
    "max": 4.0,
    "mean": 2.956891661939875,
    "min": 1.0
  },
  "note": "RSS is null when neither psutil nor a platform memory API is available.",
  "py_traced_bytes": {
    "last": 7956592.0,
    "max": 8495973.0,
    "mean": 7804867.7526942715,
    "min": 10291.0
  },
  "py_traced_peak_bytes": 8643099,
  "rss_bytes": null,
  "sample_count": 1763,
  "threads": {
    "last": 1.0,
    "max": 1.0,
    "mean": 1.0,
    "min": 1.0
  }
}
```

_Privacy: this report contains statuses, timings, counters and boolean results only; no prompts, responses, audio or credentials._
