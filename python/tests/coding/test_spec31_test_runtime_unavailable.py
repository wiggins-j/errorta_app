"""SPEC-31 — an authored acceptance test that cannot run is recorded, not hidden.

Run 10 reached definition_of_done with an acceptance test that gate_bootstrap
REFUSED (it could not launch — no browser/deps in the executor's minimal env), and
nothing said so: `done` overstated "tested". The refusal is still correct (an
unrunnable command is not a gate), but it is now recorded as a distinct
`test_runtime_unavailable` fact + persisted to run_state, and `done` acknowledges
it. (Provisioning the runtime so the test CAN run is a separate design change — the
executor gives test runs a minimal env by contract.)
"""
from __future__ import annotations

from typing import Any

from errorta_council.coding.gate_bootstrap import _record_test_runtime_unavailable
from errorta_council.coding.runner import _ack_unrun_acceptance_test


class _FakeStore:
    def __init__(self, run_state=None) -> None:
        self._rs = dict(run_state or {})
        self.decisions: list = []

    def record_decision(self, **kw: Any) -> None:
        self.decisions.append(kw)

    def set_run_state(self, **patch: Any) -> None:
        self._rs.update(patch)

    def get_run_state(self) -> dict:
        return dict(self._rs)


def test_unrunnable_test_is_recorded_and_persisted() -> None:
    store = _FakeStore()
    _record_test_runtime_unavailable(
        store, "acceptance",
        {"argv": ["node", "test/acceptance.test.js"]},
        "unrunnable: 'cannot find module' with no test output")
    # a distinct decision (not a generic refusal)
    assert any(d.get("choice") == "test_runtime_unavailable" for d in store.decisions)
    # persisted so the completion path can see it
    unrun = store.get_run_state().get("acceptance_test_unrun")
    assert unrun and unrun["command_id"] == "acceptance"
    assert "cannot find module" in unrun["reason"]


def test_done_acknowledges_an_unrun_acceptance_test() -> None:
    store = _FakeStore(run_state={"acceptance_test_unrun": {
        "command_id": "acceptance", "argv": ["node", "x"], "reason": "no browser"}})
    _ack_unrun_acceptance_test(store)
    acks = [d for d in store.decisions if d.get("choice") == "done_acceptance_test_unrun"]
    assert len(acks) == 1
    assert "not executed" in acks[0]["rationale"]


def test_done_ack_is_noop_when_test_ran() -> None:
    store = _FakeStore(run_state={})  # no acceptance_test_unrun flag
    _ack_unrun_acceptance_test(store)
    assert store.decisions == [], "no ack when the acceptance test was not flagged unrun"
