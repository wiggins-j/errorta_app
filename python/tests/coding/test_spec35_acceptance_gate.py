"""SPEC-35 — the recoverable acceptance `done` gate.

`done` is refused while the project's own acceptance gate is not green at the current
master head. Unlike SPEC-34's rejected draft (which blocked on a never-cleared
`acceptance_test_unrun` record and wedged permanently), this blocks on a LIVE result
the in-loop gate refreshes every merge — so red -> fix -> merge -> green lifts the
block automatically, with no state to clear by hand.

Locks: G1 precise acceptance-only lookup (ignores web:probe / unit / PR runs), G2 the
status classifier, G3/G4 the block + bounded stale-arm at the done chokepoints, plus
the recovery invariant and result isolation.
"""
from __future__ import annotations

from errorta_council.coding import completion, gate_state
from errorta_council.coding.runner import _acceptance_gate_blocks_done

_ACC = {"scope": "acceptance", "argv": ["node", "t.js"]}
_UNIT = {"scope": "unit", "argv": ["true"]}


def _run(head, results):
    # A recorded run's per-command results carry a `status` (to_dict). Default to
    # "completed" so a plain {command_id, passed} models a genuine (RAN) result;
    # a launch failure passes status="blocked"/"timed_out" explicitly.
    norm = []
    for r in results:
        rr = dict(r)
        rr.setdefault("status", "completed")
        norm.append(rr)
    return {"head": head, "results": norm,
            "passed": all(r.get("passed") for r in norm)}


class _Store:
    def __init__(self, cmds=None, runs=None, run_state=None) -> None:
        self._cmds = dict(cmds or {})
        self._runs = list(runs or [])
        self._rs = dict(run_state or {})
        self.decisions: list = []
        self.raise_runs = False

    def get_test_commands(self) -> dict: return dict(self._cmds)

    def list_test_runs(self) -> list:
        if self.raise_runs:
            raise RuntimeError("boom")
        return list(self._runs)

    def get_run_state(self) -> dict: return dict(self._rs)
    def set_run_state(self, **p) -> None: self._rs.update(p)
    def record_decision(self, **kw) -> None: self.decisions.append(kw)


class _WS:
    def __init__(self, head: str) -> None: self._h = head
    def head(self) -> str: return self._h


# --------------------------------------------------------------------------- #
# G1 — latest_acceptance_result: isolate the acceptance command's own verdict
# --------------------------------------------------------------------------- #
def test_g1_picks_the_acceptance_run_over_probe_and_unit() -> None:
    runs = [
        _run("h1", [{"command_id": "acc", "passed": True}]),          # acceptance
        _run("h2", [{"command_id": "web:probe", "passed": True}]),    # probe (newer)
        _run("h2", [{"command_id": "u", "passed": True}]),            # unit  (newer)
    ]
    store = _Store(cmds={"acc": _ACC, "u": _UNIT}, runs=runs)
    res = gate_state.latest_acceptance_result(store)
    assert res == {"passed": True, "ran": True, "head": "h1"}


def test_g1_uses_acceptance_own_result_not_session_verdict() -> None:
    # A mixed session where the unit cmd failed but the acceptance cmd passed: the
    # acceptance verdict must be read from its OWN per-result, not session.passed.
    runs = [_run("h9", [{"command_id": "acc", "passed": True},
                        {"command_id": "u", "passed": False}])]
    store = _Store(cmds={"acc": _ACC, "u": _UNIT}, runs=runs)
    assert gate_state.latest_acceptance_result(store) == {
        "passed": True, "ran": True, "head": "h9"}


def test_g1_ran_false_for_a_launch_failure() -> None:
    # A blocked/timed_out acceptance result did not cleanly execute -> ran is False,
    # so the caller can route it to `stale` (bounded) instead of `red` (unbounded).
    runs = [_run("h1", [{"command_id": "acc", "passed": False, "status": "blocked"}])]
    store = _Store(cmds={"acc": _ACC}, runs=runs)
    assert gate_state.latest_acceptance_result(store) == {
        "passed": False, "ran": False, "head": "h1"}


def test_g1_none_when_no_acceptance_command_registered() -> None:
    runs = [_run("h1", [{"command_id": "acc", "passed": True}])]
    store = _Store(cmds={"u": _UNIT}, runs=runs)  # 'acc' not registered as acceptance
    assert gate_state.latest_acceptance_result(store) is None


def test_g1_none_when_registered_but_never_run() -> None:
    store = _Store(cmds={"acc": _ACC}, runs=[
        _run("h1", [{"command_id": "web:probe", "passed": True}])])
    assert gate_state.latest_acceptance_result(store) is None


def test_g1_guarded_against_read_error() -> None:
    store = _Store(cmds={"acc": _ACC})
    store.raise_runs = True
    assert gate_state.latest_acceptance_result(store) is None


# --------------------------------------------------------------------------- #
# G2 — acceptance_gate_status
# --------------------------------------------------------------------------- #
def test_g2_no_gate_when_unregistered() -> None:
    assert completion.acceptance_gate_status(_Store(cmds={"u": _UNIT}), "h1") == "no_gate"


def test_g2_green_red_at_head() -> None:
    green = _Store(cmds={"acc": _ACC},
                   runs=[_run("h1", [{"command_id": "acc", "passed": True}])])
    assert completion.acceptance_gate_status(green, "h1") == "green"
    red = _Store(cmds={"acc": _ACC},
                 runs=[_run("h1", [{"command_id": "acc", "passed": False}])])
    assert completion.acceptance_gate_status(red, "h1") == "red"


def test_g2_stale_on_head_mismatch_or_never_run() -> None:
    mismatch = _Store(cmds={"acc": _ACC},
                      runs=[_run("OLD", [{"command_id": "acc", "passed": True}])])
    assert completion.acceptance_gate_status(mismatch, "NEW") == "stale"
    never = _Store(cmds={"acc": _ACC})
    assert completion.acceptance_gate_status(never, "h1") == "stale"


def test_g2_no_gate_when_head_unresolvable() -> None:
    # empty head -> cannot bind -> never block (fail-open)
    store = _Store(cmds={"acc": _ACC},
                   runs=[_run("h1", [{"command_id": "acc", "passed": False}])])
    assert completion.acceptance_gate_status(store, "") == "no_gate"


def test_g2_launch_failure_is_stale_not_red() -> None:
    # BLOCKER LOCK: a launch/provisioning failure (blocked/timed_out) at the head is
    # environmental — no code merge flips it green. It must be `stale` (bounded via
    # the completion_refused ladder), never `red` (an unbounded permanent wedge).
    for bad in ("blocked", "timed_out", "failed"):
        store = _Store(cmds={"acc": _ACC}, runs=[
            _run("h1", [{"command_id": "acc", "passed": False, "status": bad}])])
        assert completion.acceptance_gate_status(store, "h1") == "stale", bad


# --------------------------------------------------------------------------- #
# G3 / G4 — block + bounded stale-arm at the done chokepoint
# --------------------------------------------------------------------------- #
def test_g3_green_allows() -> None:
    store = _Store(cmds={"acc": _ACC},
                   runs=[_run("h1", [{"command_id": "acc", "passed": True}])])
    assert _acceptance_gate_blocks_done(store, _WS("h1")) is None


def test_g3_no_gate_allows() -> None:
    store = _Store(cmds={"u": _UNIT})
    assert _acceptance_gate_blocks_done(store, _WS("h1")) is None


def test_g3_red_blocks() -> None:
    store = _Store(cmds={"acc": _ACC},
                   runs=[_run("h1", [{"command_id": "acc", "passed": False}])])
    reason = _acceptance_gate_blocks_done(store, _WS("h1"))
    assert reason and "RED" in reason


def test_g3_stale_arms_the_in_loop_gate_and_blocks() -> None:
    store = _Store(cmds={"acc": _ACC})  # registered, never run -> stale
    reason = _acceptance_gate_blocks_done(store, _WS("HEAD1"))
    assert reason and "no usable result" in reason
    rs = store.get_run_state()
    assert rs.get("gate_due") is True
    assert rs.get("gate_dirty_head") == "HEAD1"


def test_g4_launch_failure_blocks_via_stale_not_unbounded_red() -> None:
    # BLOCKER LOCK: a launch-failing acceptance gate is `stale` (arms + blocks), NOT
    # an unbounded `red`. Boundedness comes from the completion_refused ladder (each
    # block returns completion_refused -> completion_blocked at the limit), so this
    # never wedges; here we just assert it arms + defers rather than red-blocking.
    store = _Store(cmds={"acc": _ACC}, runs=[
        _run("h1", [{"command_id": "acc", "passed": False, "status": "blocked"}])])
    reason = _acceptance_gate_blocks_done(store, _WS("h1"))
    assert reason and "RED" not in reason           # not an unbounded red block
    assert store.get_run_state().get("gate_due") is True  # armed a fresh run instead


def test_g3_fail_open_without_workspace_or_head() -> None:
    store = _Store(cmds={"acc": _ACC},
                   runs=[_run("h1", [{"command_id": "acc", "passed": False}])])
    assert _acceptance_gate_blocks_done(store, None) is None
    assert _acceptance_gate_blocks_done(store, _WS("")) is None


# --------------------------------------------------------------------------- #
# Recovery invariant + isolation
# --------------------------------------------------------------------------- #
def test_recovery_red_then_green_lifts_the_block_with_no_manual_clear() -> None:
    # 1) acceptance gate red at H1 -> blocked
    store = _Store(cmds={"acc": _ACC},
                   runs=[_run("H1", [{"command_id": "acc", "passed": False}])])
    assert _acceptance_gate_blocks_done(store, _WS("H1")) is not None
    # 2) team fixes it; master advances to H2 and the in-loop gate runs it green.
    #    NOTHING clears run-state by hand — a new green run is simply appended.
    store._runs.append(_run("H2", [{"command_id": "acc", "passed": True}]))
    assert _acceptance_gate_blocks_done(store, _WS("H2")) is None  # block lifted


def test_isolation_probe_or_unit_green_does_not_satisfy_acceptance() -> None:
    # green web:probe + green unit at the current head, but the acceptance gate has
    # no result there -> still stale (blocked), never spuriously green.
    store = _Store(cmds={"acc": _ACC, "u": _UNIT}, runs=[
        _run("h1", [{"command_id": "web:probe", "passed": True}]),
        _run("h1", [{"command_id": "u", "passed": True}])])
    assert completion.acceptance_gate_status(store, "h1") == "stale"
    assert _acceptance_gate_blocks_done(store, _WS("h1")) is not None
