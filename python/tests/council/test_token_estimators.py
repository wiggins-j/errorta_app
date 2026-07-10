from __future__ import annotations

import json

from errorta_council.context.tokens import (
    CalibrationSample,
    CalibratedEstimator,
    HeuristicEstimator,
    TokenCalibrationStore,
    WhitespaceEstimator,
    calibration_ratio,
    clamp_factor,
    update_ema,
)


def test_whitespace_estimator_preserves_legacy_behavior() -> None:
    estimator = WhitespaceEstimator()
    assert estimator.method == "whitespace"
    assert estimator.estimate("one two three") == 3
    assert estimator.estimate("") == 1


def test_heuristic_estimator_counts_code_higher_than_prose() -> None:
    estimator = HeuristicEstimator()
    text = "for item in values: print({'id': item, 'ok': True})"
    assert estimator.estimate(text, content_kind="code") > estimator.estimate(
        text, content_kind="prose"
    )


def test_calibrated_estimator_applies_clamped_factor() -> None:
    base = WhitespaceEstimator()
    estimator = CalibratedEstimator(base, factor=2.5)
    assert estimator.method == "calibrated_heuristic_v1"
    assert estimator.calibration_factor == 2.5
    assert estimator.estimate("one two") == 5
    assert CalibratedEstimator(base, factor=99).calibration_factor == 3.0
    assert CalibratedEstimator(base, factor=0.01).calibration_factor == 0.7


def test_calibration_ratio_and_ema_are_clamped() -> None:
    assert calibration_ratio(reported_input_tokens=150, estimated_input_tokens=100) == 1.5
    assert calibration_ratio(reported_input_tokens=0, estimated_input_tokens=100) is None
    assert calibration_ratio(reported_input_tokens=100, estimated_input_tokens=0) is None
    assert clamp_factor(float("nan")) == 1.0
    assert update_ema(1.0, 2.0, alpha=0.25) == 1.25
    assert update_ema(None, 5.0) == 3.0


def test_token_calibration_store_round_trip_and_corrupt_fallback(tmp_path) -> None:
    path = tmp_path / "token_calibration.json"
    store = TokenCalibrationStore(path)
    assert store.read_factor("local", "llama") == 1.0

    factor = store.record(CalibrationSample(provider="local", model="llama", ratio=1.5))
    assert factor == 1.5
    assert store.read_factor("local", "llama") == 1.5

    store.record(CalibrationSample(provider="local", model="llama", ratio=2.0), alpha=0.5)
    assert store.read_factor("local", "llama") == 1.75
    payload = json.loads(path.read_text())
    assert payload["format"] == "errorta.token_calibration.v1"
    assert payload["factors"]["local/llama"] == 1.75

    path.write_text("{not-json")
    assert store.read_all() == {}
    assert store.read_factor("local", "llama") == 1.0
