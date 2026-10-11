import math
import pytest

from continuity_engine import DriveConfig, DriveSystem, PredictionModel, PredictionObservation, WorldModel, WorldStatus


def test_drive_converges_and_remains_bounded():
    ds = DriveSystem({"social": DriveConfig(tau_s=10, initial_value=0.0, initial_target=1.0)})
    ds.update(10, 10)
    value = ds.states["social"].value
    assert 0 < value < 1
    ds.update(10_000, 10_010)
    assert ds.states["social"].value == pytest.approx(1.0)
    ds.states["social"].validate()


def test_drive_rejects_invalid_dt():
    ds = DriveSystem({"social": DriveConfig(tau_s=10)})
    for dt in [-1, float("nan"), float("inf")]:
        with pytest.raises(ValueError):
            ds.update(dt, 1)


def test_impulse_is_transient_and_target_does_not_change():
    ds = DriveSystem({"exploration": DriveConfig(tau_s=10, initial_value=0.1, initial_target=0.1)})
    ds.impulse("exploration", 0.5)
    assert ds.states["exploration"].value == pytest.approx(0.6)
    assert ds.states["exploration"].target == pytest.approx(0.1)
    ds.update(10, 10)
    assert ds.states["exploration"].value < 0.6


def test_world_model_conflicting_strong_evidence_is_not_averaged():
    model = WorldModel()
    a = model.observe(key="owner_present", value=True, confidence=0.85, source="camera", observed_at_mono=1, ttl_s=10, event_id="e1")
    b = model.observe(key="owner_present", value=False, confidence=0.80, source="presence_sensor", observed_at_mono=2, ttl_s=10, event_id="e2")
    assert a.status == WorldStatus.PROBABLE
    assert b.status == WorldStatus.CONFLICT
    assert b.value is None


def test_world_model_stale_evidence_becomes_stale_not_false():
    model = WorldModel()
    model.observe(key="owner_present", value=True, confidence=0.99, source="camera", observed_at_mono=1, ttl_s=2, event_id="e1")
    fact = model.get("owner_present", 4)
    assert fact.status == WorldStatus.STALE
    assert fact.value is None


def test_world_model_duplicate_event_not_double_counted():
    model = WorldModel()
    model.observe(key="x", value=1, confidence=0.8, source="a", observed_at_mono=1, ttl_s=5, event_id="e1")
    fact = model.observe(key="x", value=0, confidence=0.8, source="b", observed_at_mono=2, ttl_s=5, event_id="e1")
    assert fact.value == 1


def obs(event, memory, key, value, t):
    return PredictionObservation(event, memory, key, value, 1.0, t)


def test_prediction_is_deterministic_and_confidence_weighted():
    model = PredictionModel(alpha=0.1, min_observations=3)
    model.learn(obs("e1", "m1", "return_time", 10.0, 1))
    model.learn(obs("e2", "m2", "return_time", 12.0, 2))
    est = model.learn(obs("e3", "m3", "return_time", 11.0, 3))
    assert est.provisional
    assert est.observation_count == 3
    assert est.expected_value is not None
    assert math.isfinite(est.expected_value)


def test_prediction_memory_delete_rebuilds_or_resets_partition():
    model = PredictionModel(alpha=0.1)
    model.learn(obs("e1", "m1", "k", 1.0, 1))
    model.learn(obs("e2", "m2", "k", 3.0, 2))
    result = model.delete_memory_lineage("m1")
    assert result == {"k": "REBUILT"}
    assert model.estimate("k").expected_value == pytest.approx(3.0)
    result = model.delete_memory_lineage("m2", lineage_complete=False)
    assert result == {"k": "RESET"}
    assert model.estimate("k").expected_value is None


def test_world_model_does_not_roll_back_on_out_of_order_source_observation():
    model = WorldModel()
    model.observe(key="position", value=2, confidence=0.8, source="camera", observed_at_mono=10, ttl_s=20, event_id="e-new")
    fact = model.observe(key="position", value=1, confidence=0.9, source="camera", observed_at_mono=5, ttl_s=20, event_id="e-old")
    assert fact.value == 2


def test_event_and_world_inputs_reject_nonfinite_values():
    from continuity_engine import Event
    with pytest.raises(ValueError):
        Event("bad", "X", "test", 0, 1, {"x": float("nan")})
    model = WorldModel()
    with pytest.raises(ValueError):
        model.observe(key="x", value=float("inf"), confidence=0.9, source="cam", observed_at_mono=1, ttl_s=2, event_id="e1")


def test_world_model_canonicalizes_mapping_order_without_false_conflict():
    model = WorldModel()
    a = {"name": "object", "x": 1, "meta": {"left": 1, "right": 2}}
    b = {"meta": {"right": 2, "left": 1}, "x": 1, "name": "object"}
    model.observe(key="object", value=a, confidence=0.92, source="camera_a", observed_at_mono=1, ttl_s=10, event_id="e1")
    result = model.observe(key="object", value=b, confidence=0.91, source="camera_b", observed_at_mono=2, ttl_s=10, event_id="e2")
    assert result.status == WorldStatus.CONFIRMED
