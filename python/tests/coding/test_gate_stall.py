"""Spec 04 — gate-repeat stall detection.

Unit tests for the PURE detector ``_account_gate_stall`` and its signal
``_gate_fingerprint``, driven with a stub ledger exposing ``list_test_runs()``
and ``get_run_state()``. The detector keys on the ACCEPTANCE RESULT (test-run
pass count / delivery verdict), NOT the PR head, so a run stuck at 6/12 while its
head churns finally trips — the regression this fix targets.
"""
from __future__ import annotations

from typing import Any, Optional

from errorta_council.coding.autonomy import (
    GATE_NOT_IMPROVING,
    CodingAutonomyPolicy,
    LoopCounters,
    _account_gate_stall,
    _gate_fingerprint,
    policy_from_dict,
    policy_to_dict,
)


class FakeLedger:
    """Minimal stand-in exposing only what ``_gate_fingerprint`` reads."""

    def __init__(self, test_runs: Optional[list] = None,
                 run_state: Optional[dict] = None) -> None:
        self._test_runs = list(test_runs or [])
        self._run_state = dict(run_state or {})

    def list_test_runs(self) -> list[dict[str, Any]]:
        return list(self._test_runs)

    def get_run_state(self) -> dict[str, Any]:
        return dict(self._run_state)


def _test_record(passing: int, total: int = 12, *, head: str = "",
                 fail_from: int = 0) -> dict[str, Any]:
    """A test-run record with ``passing`` of ``total`` commands green. ``fail_from``
    shifts WHICH commands fail (to change the fingerprint while keeping the pass
    count fixed)."""
    results = []
    for i in range(total):
        idx = (i + fail_from) % total
        exit_code = 0 if idx < passing else 1
        results.append({
            "command_id": f"cmd-{i}",
            "exit_code": exit_code,
            "status": "completed" if exit_code == 0 else "failed",
        })
    return {"results": results, "passed": passing == total, "head": head}


def _set_latest(ledger: FakeLedger, record: dict[str, Any]) -> None:
    ledger._test_runs = [record]


# --- _gate_fingerprint ------------------------------------------------------

def test_fingerprint_sentinel_when_no_signal() -> None:
    fp, score = _gate_fingerprint(FakeLedger())
    assert fp == () and score == -1


def test_fingerprint_score_is_pass_count() -> None:
    led = FakeLedger(test_runs=[_test_record(6, 12)])
    _fp, score = _gate_fingerprint(led)
    assert score == 6


def test_fingerprint_ignores_head() -> None:
    fp_a, score_a = _gate_fingerprint(FakeLedger(test_runs=[_test_record(6, head="aaa")]))
    fp_b, score_b = _gate_fingerprint(FakeLedger(test_runs=[_test_record(6, head="bbb")]))
    assert fp_a == fp_b and score_a == score_b == 6


def test_fingerprint_delivery_passed_dominates() -> None:
    led = FakeLedger(test_runs=[_test_record(6, 12)],
                     run_state={"delivery_review_passed": True,
                                "delivery_reviewed_head": "h9"})
    _fp, score = _gate_fingerprint(led)
    assert score == 10_000


def test_fingerprint_run_level_passed_without_results() -> None:
    led = FakeLedger(test_runs=[{"results": [], "passed": True, "head": "h"}])
    _fp, score = _gate_fingerprint(led)
    assert score == 1
    led2 = FakeLedger(test_runs=[{"results": [], "passed": False, "head": "h"}])
    _fp2, score2 = _gate_fingerprint(led2)
    assert score2 == 0


def test_fingerprint_missing_methods_returns_sentinel() -> None:
    class Bare:
        pass
    fp, score = _gate_fingerprint(Bare())
    assert fp == () and score == -1


# --- _account_gate_stall ----------------------------------------------------

def test_identical_result_trips_after_limit() -> None:
    limit = 4
    led = FakeLedger(test_runs=[_test_record(6, 12)])
    c = LoopCounters()
    policy = CodingAutonomyPolicy(gate_stall_limit=limit)
    stop = None
    # Identical 6/12 across limit + 1 iterations.
    for i in range(1, limit + 2):
        c.iterations = i
        stop = _account_gate_stall(led, c, policy)
        if i <= limit:
            assert stop is None, f"tripped early at iter {i}"
    assert stop is not None and stop.stop_reason == GATE_NOT_IMPROVING
    assert c.iterations - c.last_gate_iter >= limit


def test_rising_pass_count_resets_window() -> None:
    limit = 4
    led = FakeLedger(test_runs=[_test_record(6, 12)])
    c = LoopCounters()
    policy = CodingAutonomyPolicy(gate_stall_limit=limit)
    # 3 iterations stuck at 6...
    for i in range(1, 4):
        c.iterations = i
        assert _account_gate_stall(led, c, policy) is None
    # ...then a command starts passing (6 -> 7): strict improvement resets.
    c.iterations = 4
    _set_latest(led, _test_record(7, 12))
    assert _account_gate_stall(led, c, policy) is None
    assert c.last_gate_best == 7 and c.last_gate_iter == 4
    # It now takes a fresh full window at 7/12 before it trips.
    stop = None
    for i in range(5, 4 + limit + 2):
        c.iterations = i
        stop = _account_gate_stall(led, c, policy)
        if stop is not None:
            break
    assert stop is not None and stop.stop_reason == GATE_NOT_IMPROVING
    assert c.iterations - 4 >= limit


def test_limit_zero_never_trips() -> None:
    led = FakeLedger(test_runs=[_test_record(6, 12)])
    c = LoopCounters()
    policy = CodingAutonomyPolicy(gate_stall_limit=0)
    for i in range(1, 50):
        c.iterations = i
        assert _account_gate_stall(led, c, policy) is None


def test_head_churn_same_score_still_trips() -> None:
    """The key regression: a changing head must NOT count as motion."""
    limit = 4
    led = FakeLedger(test_runs=[_test_record(6, 12, head="h0")])
    c = LoopCounters()
    policy = CodingAutonomyPolicy(gate_stall_limit=limit)
    stop = None
    for i in range(1, limit + 2):
        c.iterations = i
        _set_latest(led, _test_record(6, 12, head=f"h{i}"))  # new head each turn
        stop = _account_gate_stall(led, c, policy)
        if stop is not None:
            break
    assert stop is not None and stop.stop_reason == GATE_NOT_IMPROVING


def test_reshuffled_failset_same_score_still_trips() -> None:
    """A failing set that merely reshuffles (fp changes, score constant) is churn,
    not motion — it must still trip."""
    limit = 4
    led = FakeLedger()
    c = LoopCounters()
    policy = CodingAutonomyPolicy(gate_stall_limit=limit)
    stop = None
    for i in range(1, limit + 2):
        c.iterations = i
        # Same 6/12 score, but a different subset of commands fails each turn.
        _set_latest(led, _test_record(6, 12, fail_from=i))
        stop = _account_gate_stall(led, c, policy)
        if stop is not None:
            break
    assert stop is not None and stop.stop_reason == GATE_NOT_IMPROVING


def test_no_signal_never_trips() -> None:
    led = FakeLedger()  # no test runs, no delivery verdict
    c = LoopCounters()
    policy = CodingAutonomyPolicy(gate_stall_limit=2)
    for i in range(1, 20):
        c.iterations = i
        assert _account_gate_stall(led, c, policy) is None
    assert c.last_gate_best == -1  # never observed a signal


def test_delivery_passed_resets_window() -> None:
    limit = 4
    led = FakeLedger(test_runs=[_test_record(6, 12)])
    c = LoopCounters()
    policy = CodingAutonomyPolicy(gate_stall_limit=limit)
    # Stuck at 6/12 for a few turns...
    for i in range(1, 4):
        c.iterations = i
        assert _account_gate_stall(led, c, policy) is None
    # ...then delivery review passes: score jumps -> reset, no trip.
    c.iterations = 4
    led._run_state = {"delivery_review_passed": True, "delivery_reviewed_head": "h"}
    assert _account_gate_stall(led, c, policy) is None
    assert c.last_gate_best == 10_000 and c.last_gate_iter == 4


# --- policy round-trip ------------------------------------------------------

def test_policy_roundtrip_preserves_value() -> None:
    p = CodingAutonomyPolicy(gate_stall_limit=5)
    assert policy_from_dict(policy_to_dict(p)).gate_stall_limit == 5


def test_policy_missing_key_defaults_to_eight() -> None:
    assert policy_from_dict({}).gate_stall_limit == 8


def test_policy_negative_clamps_to_zero() -> None:
    assert policy_from_dict({"gate_stall_limit": -3}).gate_stall_limit == 0
