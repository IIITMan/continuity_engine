# v0.24 deployment preflight and field evidence

The validation runner reports configuration/readiness plus injected checks. It is deliberately non-destructive: it does not move an actuator, open a microphone, take a photo or send network traffic unless a caller explicitly injects a probe that does so.

```python
from continuity_engine import FieldValidationRunner

runner = FieldValidationRunner(
    engine,
    companion=companion,
    voice_pipeline=voice_pipeline,  # omit when not connected
    llm_runtime=runtime,             # optional; provider probe is opt-in
    probes={"speaker_loopback": speaker_probe},  # host-owned async/sync health probe
)
report = await runner.run(field_mode=True, perform_provider_probe=False)
Path("field-report.json").write_text(report.to_json(), encoding="utf-8")
print(report.to_dict()["field_validated"])
```

A probe may return `{"status":"PASS", "detail":"...", "physical_evidence": true}` only when the host has actually verified a physical device. The report treats this flag as host attestation. A passing simulation, mocked sensor, or fake player is not a physical field validation and should return `physical_evidence: false`.

For live LLM behavior, use `CompanionEvaluationRunner` with authored cases against a disposable service/session. Its JSON report omits raw inputs and outputs and stores only expectation flags, statuses, error codes, response lengths and latencies. It is a behavior smoke test, not a calibrated metric of empathy or emotional quality.

Before production use, independently validate permissions, encrypted local storage, data deletion/backup policy, provider retention terms, actual ASR/TTS codecs, loudness/latency, device failsafe and emergency stop, 24–90 hour stability, process restart, network outage and physical action cancellation on the target hardware.
