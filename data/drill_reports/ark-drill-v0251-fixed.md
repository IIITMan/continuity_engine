# Provider drill report

- run_id: `c7cdb9a9-b000-4712-8018-81fe0d6abb2d`
- started_at: 2026-10-13 18:46:57   completed_at: 2026-10-13 18:54:17
- duration_s: 440.343
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
| preflight_latency_s | 1.5632 |
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
| demand_understanding | COMPLETED | True | 1 | 0 | 3.0818 | None |
| simple_response | COMPLETED | True | 1 | 0 | 3.8828 | None |
| complex_dialogue | COMPLETED | True | 1 | 0 | 6.7202 | None |
| task_plan | COMPLETED | True | 1 | 0 | 1.5412 | None |
| multi_step_plan | COMPLETED | True | 1 | 0 | 5.6094 | None |
| memory_consolidation | COMPLETED | True | 1 | 0 | 0.9322 | None |
| provider_metadata | COMPLETED | True | 1 | 0 | 2.0367 | None |

## Conversation continuity

- turns: 60  completed: 59  degraded: 0  failed: 0
- pass_rate: 1.0  error_rate: 0.0
- latency p50/p95/max (s): 5.8834 / 12.15 / 14.8889
- memory_recall_ok: True  context_continuation_ok: True  no_fabrication_ok: True

| turn | status | passed | latency_s | chars | error_code |
| --- | --- | --- | --- | --- | --- |
| 0 | MEMORY_SAVED | True | 0.0051 | 20 | None |
| 1 | COMPLETED | True | 4.8247 | 68 | None |
| 2 | COMPLETED | True | 6.5807 | 131 | None |
| 3 | COMPLETED | True | 5.332 | 98 | None |
| 4 | COMPLETED | True | 2.7767 | 7 | None |
| 5 | COMPLETED | True | 5.2379 | 131 | None |
| 6 | COMPLETED | True | 4.3559 | 107 | None |
| 7 | COMPLETED | True | 7.2055 | 107 | None |
| 8 | COMPLETED | True | 7.5295 | 137 | None |
| 9 | COMPLETED | True | 11.9837 | 245 | None |
| 10 | COMPLETED | True | 7.4872 | 218 | None |
| 11 | COMPLETED | True | 5.693 | 177 | None |
| 12 | COMPLETED | True | 4.2025 | 64 | None |
| 13 | COMPLETED | True | 3.4518 | 52 | None |
| 14 | COMPLETED | True | 5.4563 | 104 | None |
| 15 | COMPLETED | True | 5.2551 | 80 | None |
| 16 | COMPLETED | True | 7.7734 | 131 | None |
| 17 | COMPLETED | True | 4.7734 | 76 | None |
| 18 | COMPLETED | True | 3.7664 | 63 | None |
| 19 | COMPLETED | True | 4.7373 | 82 | None |
| 20 | COMPLETED | True | 3.6835 | 68 | None |
| 21 | COMPLETED | True | 3.8647 | 96 | None |
| 22 | COMPLETED | True | 6.0351 | 147 | None |
| 23 | COMPLETED | True | 5.772 | 140 | None |
| 24 | COMPLETED | True | 7.7732 | 164 | None |
| 25 | COMPLETED | True | 7.5293 | 203 | None |
| 26 | COMPLETED | True | 14.8889 | 240 | None |
| 27 | COMPLETED | True | 4.9166 | 78 | None |
| 28 | COMPLETED | True | 3.8134 | 45 | None |
| 29 | COMPLETED | True | 3.8909 | 65 | None |
| 30 | COMPLETED | True | 6.5628 | 127 | None |
| 31 | COMPLETED | True | 5.1963 | 115 | None |
| 32 | COMPLETED | True | 4.412 | 78 | None |
| 33 | COMPLETED | True | 9.3916 | 280 | None |
| 34 | COMPLETED | True | 5.237 | 94 | None |
| 35 | COMPLETED | True | 13.3729 | 100 | None |
| 36 | COMPLETED | True | 7.5842 | 146 | None |
| 37 | COMPLETED | True | 8.0343 | 56 | None |
| 38 | COMPLETED | True | 12.15 | 370 | None |
| 39 | COMPLETED | True | 13.9779 | 283 | None |
| 40 | COMPLETED | True | 11.4045 | 278 | None |
| 41 | COMPLETED | True | 9.0971 | 89 | None |
| 42 | COMPLETED | True | 6.1653 | 188 | None |
| 43 | COMPLETED | True | 7.227 | 111 | None |
| 44 | COMPLETED | True | 6.9408 | 180 | None |
| 45 | COMPLETED | True | 5.8834 | 90 | None |
| 46 | COMPLETED | True | 3.5629 | 81 | None |
| 47 | COMPLETED | True | 6.6867 | 141 | None |
| 48 | COMPLETED | True | 6.2539 | 87 | None |
| 49 | COMPLETED | True | 6.7641 | 104 | None |
| 50 | COMPLETED | True | 7.2487 | 259 | None |
| 51 | COMPLETED | True | 6.1835 | 212 | None |
| 52 | COMPLETED | True | 10.8576 | 292 | None |
| 53 | COMPLETED | True | 7.253 | 166 | None |
| 54 | COMPLETED | True | 7.8246 | 346 | None |
| 55 | COMPLETED | True | 4.7222 | 80 | None |
| 56 | COMPLETED | True | 3.5549 | 43 | None |
| 57 | COMPLETED | True | 5.7495 | 65 | None |
| 58 | COMPLETED | True | 4.8566 | 135 | None |
| 59 | COMPLETED | True | 3.9556 | 99 | None |

## Failure drills

### controlled_retry_recovers_from_transient_faults

- passed: True
- observed: {"attempts": 4, "error": null, "injected_faults": ["RATE_LIMIT(retry_after=1s)", "SERVER(503)", "CONNECT"], "rate_limit_waits_s": 1.0, "retries": 3, "retries_by_kind": {"CONNECT": 1, "RATE_LIMIT": 1, "SERVER": 1}, "retry_after_observed_s": 1.0, "retry_wait_s": 3.502108, "succeeded": true, "wall_s": 5.0912}

### authentication_failure_is_not_retried

- passed: True
- observed: {"attempts": 1, "failures_by_kind": {"AUTH": 1}, "injected_fault": "AUTH(401)", "no_fabricated_success": true, "raised": "ProviderAuthError", "retries": 0, "wall_s": 0.0002}

### side_effecting_request_is_never_replayed

- passed: True
- observed: {"attempts": 1, "injected_fault": "RATE_LIMIT(retry_after=0.2s)", "raised": "ProviderRateLimitError", "request_replay_safe": false, "retries": 0, "retries_suppressed_no_replay": 1}

### circuit_opens_during_outage_and_recovers

- passed: True
- observed: {"circuit_rejections": 1, "fail_fast_latency_s": 0.0006, "half_open_probe_succeeded": true, "network_fail_latency_s": 0.1594, "outage_requests": 4, "probe_latency_s": 3.7051, "state_after_outage": "OPEN", "state_after_recovery": "CLOSED", "waited_for_recovery_s": 2.0668}

### client_deadline_is_bounded_and_client_still_usable

- passed: True
- observed: {"attempts": 2, "deadline_s": 1.0, "followup_call_succeeded": true, "followup_latency_s": 1.6502, "observed_timeout_s": 1.0057, "raised_timeout": true}

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
- waited_for_circuit_recovery_s: 1.9619
- recovery_attempts: 1
- recovered: True
- recovery_status: COMPLETED
- recovery_latency_s: 1.5857

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
      "retry_wait_s": 1.286577,
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
    "retry_wait_s": 3.502108,
    "successes": 3
  }
}
```

## Provider token usage (no text retained)

```json
{
  "completion_tokens": 8766,
  "last": {
    "completion_tokens": 20,
    "model": "doubao-seed-2-1-pro-260915",
    "prompt_tokens": 926,
    "reasoning_tokens": 0,
    "total_tokens": 946
  },
  "prompt_tokens": 94282,
  "reasoning_tokens": 0,
  "responses": 69,
  "total_tokens": 103048
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
  "latency_p50_s": 5.749443900072947,
  "latency_p95_s": 12.149987700162455,
  "latency_samples": 64,
  "privacy": "no raw text, prompts, credentials, or audio stored",
  "statuses": {
    "COMPLETED": 61,
    "DEGRADED": 2,
    "MEMORY_SAVED": 1
  },
  "token_estimates_are_approximate": true,
  "uptime_s": 433.205699800048,
  "variable_context_estimated_tokens_p50": 523,
  "variable_context_estimated_tokens_p95": 632,
  "variable_context_token_samples": 63
}
```

## Resource usage

```json
{
  "asyncio_tasks": {
    "last": 1.0,
    "max": 4.0,
    "mean": 2.9528985507246377,
    "min": 1.0
  },
  "note": "RSS is null when neither psutil nor a platform memory API is available.",
  "py_traced_bytes": {
    "last": 8039413.0,
    "max": 8816994.0,
    "mean": 7937960.264492754,
    "min": 10070.0
  },
  "py_traced_peak_bytes": 8904514,
  "rss_bytes": {
    "last": 73699328.0,
    "max": 78319616.0,
    "mean": 66678168.11594203,
    "min": 38043648.0
  },
  "sample_count": 1656,
  "threads": {
    "last": 1.0,
    "max": 1.0,
    "mean": 1.0,
    "min": 1.0
  }
}
```

_Privacy: this report contains statuses, timings, counters and boolean results only; no prompts, responses, audio or credentials._
