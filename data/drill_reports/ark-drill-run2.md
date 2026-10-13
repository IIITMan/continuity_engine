# Provider drill report

- run_id: `457b25f7-dc6f-43cf-bdb7-5d2a951bb598`
- started_at: 2026-10-13 16:12:18   completed_at: 2026-10-13 16:20:31
- duration_s: 492.514
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
| long_conversation_no_hard_failures | **FAIL** |
| memory_recall_across_turns | PASS |
| context_continuation | PASS |
| no_fabrication_without_live_data | PASS |
| controlled_retry_recovers_from_transient_faults | PASS |
| authentication_failure_is_not_retried | PASS |
| side_effecting_request_is_never_replayed | PASS |
| circuit_opens_during_outage_and_recovers | PASS |
| client_deadline_is_bounded_and_client_still_usable | PASS |
| cancellation_no_leaked_tasks | PASS |
| control_lane_interrupts_inflight | **FAIL** |
| restart_restores_context | **FAIL** |
| outage_failures_were_honest | PASS |
| service_recovers_after_outage | **FAIL** |

## Output-contract compatibility

| purpose | status | contract_ok | samples | violations | latency_s | first_error_code |
| --- | --- | --- | --- | --- | --- | --- |
| demand_understanding | COMPLETED | True | 1 | 0 | 4.0667 | None |
| simple_response | COMPLETED | True | 1 | 0 | 6.4305 | None |
| complex_dialogue | COMPLETED | True | 1 | 0 | 7.2208 | None |
| task_plan | COMPLETED | True | 1 | 0 | 2.8355 | None |
| multi_step_plan | COMPLETED | True | 1 | 0 | 6.7014 | None |
| memory_consolidation | COMPLETED | True | 1 | 0 | 1.4358 | None |
| provider_metadata | COMPLETED | True | 1 | 0 | 7.5104 | None |

## Conversation continuity

- turns: 60  completed: 57  degraded: 2  failed: 2
- pass_rate: 0.9667  error_rate: 0.0333
- latency p50/p95/max (s): 6.2618 / 13.3684 / 14.9415
- memory_recall_ok: True  context_continuation_ok: True  no_fabrication_ok: True

| turn | status | passed | latency_s | chars | error_code |
| --- | --- | --- | --- | --- | --- |
| 0 | MEMORY_SAVED | True | 0.008 | 20 | None |
| 1 | COMPLETED | True | 3.9173 | 57 | None |
| 2 | COMPLETED | True | 5.9927 | 114 | None |
| 3 | COMPLETED | True | 13.4653 | 146 | None |
| 4 | COMPLETED | True | 1.7895 | 7 | None |
| 5 | COMPLETED | True | 12.0927 | 306 | None |
| 6 | COMPLETED | True | 5.2879 | 97 | None |
| 7 | COMPLETED | True | 7.1025 | 192 | None |
| 8 | COMPLETED | True | 9.661 | 189 | None |
| 9 | COMPLETED | True | 13.3684 | 475 | None |
| 10 | COMPLETED | True | 11.7829 | 452 | None |
| 11 | COMPLETED | True | 12.4647 | 402 | None |
| 12 | COMPLETED | True | 3.6493 | 72 | None |
| 13 | COMPLETED | True | 3.4944 | 49 | None |
| 14 | COMPLETED | True | 6.1988 | 141 | None |
| 15 | COMPLETED | True | 4.3067 | 64 | None |
| 16 | COMPLETED | True | 6.9268 | 158 | None |
| 17 | COMPLETED | True | 6.2422 | 91 | None |
| 18 | COMPLETED | True | 10.3509 | 72 | None |
| 19 | COMPLETED | True | 4.8301 | 109 | None |
| 20 | COMPLETED | True | 4.4468 | 75 | None |
| 21 | COMPLETED | True | 5.4054 | 122 | None |
| 22 | COMPLETED | True | 10.0017 | 254 | None |
| 23 | COMPLETED | True | 9.6224 | 206 | None |
| 24 | COMPLETED | True | 6.2061 | 179 | None |
| 25 | COMPLETED | True | 8.3013 | 165 | None |
| 26 | COMPLETED | True | 14.9415 | 255 | None |
| 27 | COMPLETED | True | 4.3932 | 51 | None |
| 28 | COMPLETED | True | 4.0972 | 44 | None |
| 29 | COMPLETED | True | 4.6512 | 73 | None |
| 30 | COMPLETED | True | 8.3029 | 174 | None |
| 31 | COMPLETED | True | 6.4736 | 153 | None |
| 32 | COMPLETED | True | 6.4685 | 80 | None |
| 33 | COMPLETED | True | 7.3905 | 178 | None |
| 34 | COMPLETED | True | 6.2194 | 126 | None |
| 35 | COMPLETED | True | 6.1688 | 107 | None |
| 36 | COMPLETED | True | 5.7049 | 173 | None |
| 37 | COMPLETED | True | 4.6595 | 64 | None |
| 38 | COMPLETED | True | 7.8926 | 217 | None |
| 39 | COMPLETED | True | 9.7499 | 305 | None |
| 40 | COMPLETED | True | 8.3316 | 264 | None |
| 41 | COMPLETED | True | 4.3116 | 90 | None |
| 42 | COMPLETED | True | 6.2536 | 157 | None |
| 43 | COMPLETED | True | 12.3041 | 79 | None |
| 44 | COMPLETED | True | 7.5226 | 184 | None |
| 45 | COMPLETED | True | 6.2362 | 99 | None |
| 46 | COMPLETED | True | 4.0285 | 79 | None |
| 47 | COMPLETED | True | 9.3553 | 112 | None |
| 48 | COMPLETED | True | 5.4674 | 71 | None |
| 49 | COMPLETED | True | 10.1116 | 193 | None |
| 50 | COMPLETED | True | 13.6724 | 338 | None |
| 51 | COMPLETED | True | 6.2618 | 196 | None |
| 52 | COMPLETED | True | 7.5338 | 250 | None |
| 53 | COMPLETED | True | 6.9958 | 184 | None |
| 54 | COMPLETED | True | 11.6149 | 383 | None |
| 55 | COMPLETED | True | 7.3344 | 152 | None |
| 56 | COMPLETED | True | 3.7385 | 46 | None |
| 57 | COMPLETED | True | 5.1992 | 88 | None |
| 58 | DEGRADED | False | 10.6986 | 36 | DEGRADED_LOCAL_FALLBACK |
| 59 | DEGRADED | False | 5.2161 | 36 | DEGRADED_LOCAL_FALLBACK |

## Failure drills

### controlled_retry_recovers_from_transient_faults

- passed: True
- observed: {"attempts": 4, "error": null, "injected_faults": ["RATE_LIMIT(retry_after=1s)", "SERVER(503)", "CONNECT"], "rate_limit_waits_s": 1.0, "retries": 3, "retries_by_kind": {"CONNECT": 1, "RATE_LIMIT": 1, "SERVER": 1}, "retry_after_observed_s": 1.0, "retry_wait_s": 3.628748, "succeeded": true, "wall_s": 5.3163}

### authentication_failure_is_not_retried

- passed: True
- observed: {"attempts": 1, "failures_by_kind": {"AUTH": 1}, "injected_fault": "AUTH(401)", "no_fabricated_success": true, "raised": "ProviderAuthError", "retries": 0, "wall_s": 0.0002}

### side_effecting_request_is_never_replayed

- passed: True
- observed: {"attempts": 1, "injected_fault": "RATE_LIMIT(retry_after=0.2s)", "raised": "ProviderRateLimitError", "request_replay_safe": false, "retries": 0, "retries_suppressed_no_replay": 1}

### circuit_opens_during_outage_and_recovers

- passed: True
- observed: {"circuit_rejections": 1, "fail_fast_latency_s": 0.0002, "half_open_probe_succeeded": true, "network_fail_latency_s": 0.1593, "outage_requests": 4, "probe_latency_s": 1.7084, "state_after_outage": "OPEN", "state_after_recovery": "CLOSED", "waited_for_recovery_s": 2.097}

### client_deadline_is_bounded_and_client_still_usable

- passed: True
- observed: {"attempts": 2, "deadline_s": 1.0, "followup_call_succeeded": true, "followup_latency_s": 1.5702, "observed_timeout_s": 1.0025, "raised_timeout": true}

## Cancellation and interruption

- mode: asyncio task cancellation mid-flight plus control-lane STOP
- tasks_before: 2
- tasks_after_cancel: 2
- tasks_final: 2
- no_leaked_tasks: True
- cancel_raised: None
- control_stop_status: CONTROL_ACCEPTED
- control_interrupted: False

## Restart and recovery

- recall_after_restart_ok: False
- status: DEGRADED
- turns_before_close: 4
- turns_after_restart: 6
- context_restored: False

## Provider outage and recovery

- outage_turns: 2
- outage_statuses: ["DEGRADED", "DEGRADED"]
- outage_used_disclosed_degradation: [true, true]
- failures_were_honest: True
- waited_for_circuit_recovery_s: 1.5236
- recovered: False
- recovery_status: DEGRADED
- recovery_latency_s: 2.4930

## Reliability layer totals

```json
{
  "session_gateway": {
    "circuit": {
      "consecutive_failures": 5,
      "failure_threshold": 3,
      "opened_events": 3,
      "recovery_timeout_s": 2.0,
      "rejected_while_open": 4,
      "state": "OPEN"
    },
    "delegate": "FaultInjectingAdapter",
    "metrics": {
      "attempts": 71,
      "circuit_rejections": 4,
      "failures": 5,
      "failures_by_kind": {
        "CONNECT": 3,
        "SERVER": 1,
        "UNKNOWN": 1
      },
      "privacy": "counters only; no prompts, responses, or credentials",
      "rate_limit_waits_s": 0.0,
      "requests": 71,
      "retries": 4,
      "retries_by_kind": {
        "CONNECT": 3,
        "SERVER": 1
      },
      "retries_suppressed_no_replay": 0,
      "retry_after_observed_s": 0.0,
      "retry_wait_s": 1.976329,
      "successes": 66
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
    "retry_wait_s": 3.628748,
    "successes": 3
  }
}
```

## Provider token usage (no text retained)

```json
{
  "completion_tokens": 9505,
  "last": {
    "completion_tokens": 101,
    "model": "doubao-seed-2-1-pro-260915",
    "prompt_tokens": 1843,
    "reasoning_tokens": 0,
    "total_tokens": 1944
  },
  "prompt_tokens": 105541,
  "reasoning_tokens": 0,
  "responses": 66,
  "total_tokens": 115046
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
    "DEGRADED_LOCAL_FALLBACK": 5
  },
  "latency_p50_s": 6.253587400075048,
  "latency_p95_s": 13.368379899999127,
  "latency_samples": 63,
  "privacy": "no raw text, prompts, credentials, or audio stored",
  "statuses": {
    "COMPLETED": 57,
    "DEGRADED": 5,
    "MEMORY_SAVED": 1
  },
  "uptime_s": 489.2350465001073
}
```

## Resource usage

```json
{
  "asyncio_tasks": {
    "last": 1.0,
    "max": 4.0,
    "mean": 2.958852192744992,
    "min": 1.0
  },
  "note": "RSS is null when neither psutil nor a platform memory API is available.",
  "py_traced_bytes": {
    "last": 8096079.0,
    "max": 8932921.0,
    "mean": 7978049.460205739,
    "min": 9910.0
  },
  "py_traced_peak_bytes": 8934553,
  "rss_bytes": {
    "last": 71557120.0,
    "max": 72499200.0,
    "mean": 66243503.61017867,
    "min": 38002688.0
  },
  "sample_count": 1847,
  "threads": {
    "last": 2.0,
    "max": 2.0,
    "mean": 1.032485110990796,
    "min": 1.0
  }
}
```

_Privacy: this report contains statuses, timings, counters and boolean results only; no prompts, responses, audio or credentials._
