"""Spec 12-18 prep PR — the de-conflicting seams, locked to today's behaviour.

This PR exists only so two engineers can build Specs 12-14 and 15-18 in parallel
branches without colliding. Its review bar is therefore "nothing behaves
differently", and these tests are that bar:

* P0.1 — ``_handle_review_rejection`` produces byte-identical revise tasks to the
  two inlined copies it replaced (reviewer arm and strict-mode PM-review arm).
* P0.2 — the seven new policy knobs round-trip and default as documented.
* P0.3 — ``dev_repo_read``'s default and every statement about it agree.
* P0.4 — ``gate_state`` answers match the existing ``evidence`` predicate.
"""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding import gate_state, runner
from errorta_council.coding.autonomy import (
    CodingAutonomyPolicy,
    policy_from_dict,
    policy_to_dict,
)
from errorta_council.coding.ledger import LedgerStore

_FINDINGS = [
    {"severity": "blocking", "title": "Missing null check", "path": "src/api.ts",
     "body": "…", "blocking": True},
    {"severity": "minor", "title": "Rename var", "path": "src/x.ts",
     "body": "…", "blocking": False},
]


def _project(pid: str) -> LedgerStore:
    store = LedgerStore(pid)
    store.create_project(north_star="n", definition_of_done="d", target="new",
                         repo_path=None)
    return store


def _pr(store: LedgerStore, branch: str = "task-t-1") -> dict:
    task = store.add_task(title="build a thing", role="dev")
    return store.record_pr(task_id=task.task_id, branch=branch, head="h1",
                           dev_member="m-dev")


class _FakeResult:
    """Minimal stand-in for testing.TestRunResult (record_test_run calls
    to_dict() on each)."""

    def __init__(self, command_id="acceptance", *, exit_code=1, passed=False,
                 stdout="", stderr=""):
        self.command_id, self.exit_code, self.passed = command_id, exit_code, passed
        self.status = "completed"
        self.stdout_preview, self.stderr_preview = stdout, stderr

    def to_dict(self) -> dict:
        return {"command_id": self.command_id, "status": self.status,
                "exit_code": self.exit_code, "passed": self.passed,
                "stdout_preview": self.stdout_preview,
                "stderr_preview": self.stderr_preview}


class _FakeSession:
    def __init__(self, results, *, passed=False):
        self.results = list(results)
        self.command_ids = [r.command_id for r in self.results]
        self.unknown_ids: list[str] = []
        self.passed, self.sandbox = passed, ""


# --------------------------------------------------------------------------- #
# P0.1 — the review-rejection seam. These strings are the contract: Specs 13/14
# change what goes IN and Specs 15/16 change what comes OUT, so the baseline has
# to be pinned before either side moves.
# --------------------------------------------------------------------------- #

def test_reviewer_rejection_task_is_unchanged(tmp_errorta_home: Path) -> None:
    store = _project("p01a")
    pr = _pr(store)
    review_task = store.add_task(title="review: build a thing", role="reviewer")

    runner._handle_review_rejection(
        store, None, pr=pr, task=review_task, findings=_FINDINGS,
        source="reviewer")

    revise = [t for t in store.list_tasks() if t.title.startswith("revise:")]
    assert len(revise) == 1
    t = revise[0]
    assert t.title == "revise: task-t-1"
    assert t.role == "dev"
    assert t.pr_id == pr["pr_id"]
    assert t.depends_on == [review_task.task_id]
    assert t.reason_summary == (
        "1 blocking finding: 'Missing null check' (src/api.ts)")
    assert t.detail == (
        f"Address reviewer findings on branch task-t-1 and open a new PR. "
        f"The prior PR ({pr['pr_id']}) is superseded when this lands. "
        f"Findings: Missing null check (src/api.ts); Rename var (src/x.ts).")
    assert store.get_pr(pr["pr_id"])["status"] == "changes_requested"


def test_pm_review_rejection_task_is_unchanged(tmp_errorta_home: Path) -> None:
    store = _project("p01b")
    pr = _pr(store)
    pm_task = store.add_task(title="review PR: build a thing", role="pm")

    runner._handle_review_rejection(
        store, None, pr=pr, task=pm_task, findings=_FINDINGS,
        source="pm_review")

    t = [x for x in store.list_tasks() if x.title.startswith("revise:")][0]
    assert t.depends_on == [pm_task.task_id]
    assert t.reason_summary == (
        "1 blocking finding: 'Missing null check' (src/api.ts)")
    # The PM arm's own wording — the one string that differed between the two
    # inlined copies.
    assert t.detail.startswith("Address PM review findings on branch task-t-1")


def test_pm_rejection_without_findings_keeps_its_fallback_reason(
        tmp_errorta_home: Path) -> None:
    """The PM arm alone had `or "PM requested changes"`; the reviewer arm did
    not. Losing that asymmetry in the extraction would be a silent regression."""
    store = _project("p01c")
    pr = _pr(store)
    pm_task = store.add_task(title="review PR: x", role="pm")

    runner._handle_review_rejection(
        store, None, pr=pr, task=pm_task, findings=[], source="pm_review")
    t = [x for x in store.list_tasks() if x.title.startswith("revise:")][0]
    assert t.reason_summary == "PM requested changes"
    # ...and no "Findings:" clause when there are none.
    assert "Findings:" not in t.detail


def test_reviewer_rejection_without_findings_has_blank_reason(
        tmp_errorta_home: Path) -> None:
    store = _project("p01d")
    pr = _pr(store)
    review_task = store.add_task(title="review: x", role="reviewer")

    runner._handle_review_rejection(
        store, None, pr=pr, task=review_task, findings=[], source="reviewer")
    t = [x for x in store.list_tasks() if x.title.startswith("revise:")][0]
    assert t.reason_summary == ""


def test_revise_title_still_matches_the_supersede_matcher(
        tmp_errorta_home: Path) -> None:
    """_supersede_ancestors / _reconcile_stale prune corrective tasks by matching
    `branch in title AND title.lower().startswith(_CORRECTIVE_PREFIXES)`. The
    extraction must not put the reason on the title."""
    store = _project("p01e")
    pr = _pr(store, branch="task-t-zz")
    review_task = store.add_task(title="review: x", role="reviewer")

    runner._handle_review_rejection(
        store, None, pr=pr, task=review_task, findings=_FINDINGS,
        source="reviewer")
    t = [x for x in store.list_tasks() if x.title.startswith("revise:")][0]
    assert "task-t-zz" in t.title
    assert t.title.lower().startswith(runner._CORRECTIVE_PREFIXES)


# --------------------------------------------------------------------------- #
# P0.2 — the seven policy knobs, landed with no consumers so neither branch has
# to touch the dataclass or its two round-trip functions.
# --------------------------------------------------------------------------- #

_NEW_KNOBS = {
    "gate_bootstrap": True,
    "gate_min_merge_interval": 3,
    "reviewer_repo_read": False,
    "review_min_latency_ms": 0,
    "review_screenshot": False,
    "revise_chain_limit": 3,
    "revise_livelock_limit": 5,
}


def test_new_policy_knobs_have_their_specced_defaults() -> None:
    base = policy_to_dict(CodingAutonomyPolicy())
    for key, expected in _NEW_KNOBS.items():
        assert base[key] == expected, key


def test_absent_keys_round_trip_to_the_dataclass_defaults() -> None:
    assert policy_to_dict(policy_from_dict({})) == policy_to_dict(
        CodingAutonomyPolicy())


def test_new_policy_knobs_round_trip_explicit_values() -> None:
    raw = {"gate_bootstrap": False, "gate_min_merge_interval": 7,
           "reviewer_repo_read": True, "review_min_latency_ms": 2500,
           "review_screenshot": True, "revise_chain_limit": 4,
           "revise_livelock_limit": 9}
    out = policy_to_dict(policy_from_dict(raw))
    for key, value in raw.items():
        assert out[key] == value, key


def test_detector_knobs_accept_zero_to_disable() -> None:
    """Spec 04 / Spec 10 convention: max(0, …) on a detector limit so an operator
    can disable it; max(1, …) on a cadence where 0 is meaningless."""
    out = policy_to_dict(policy_from_dict(
        {"revise_chain_limit": 0, "revise_livelock_limit": 0,
         "review_min_latency_ms": 0, "gate_min_merge_interval": 0}))
    assert out["revise_chain_limit"] == 0
    assert out["revise_livelock_limit"] == 0
    assert out["review_min_latency_ms"] == 0
    assert out["gate_min_merge_interval"] == 1  # clamped up, not a disable


def test_negative_values_are_clamped() -> None:
    out = policy_to_dict(policy_from_dict(
        {"revise_chain_limit": -5, "review_min_latency_ms": -1,
         "gate_min_merge_interval": -3}))
    assert out["revise_chain_limit"] == 0
    assert out["review_min_latency_ms"] == 0
    assert out["gate_min_merge_interval"] == 1


# --------------------------------------------------------------------------- #
# P0.3 — the drift lock. dev_repo_read disagreed with itself in four places
# (field False; its docstring "Default ON"; policy_from_dict's comment "dataclass
# default (True)"; build_run_turn's "(default True)"). The field has always been
# False, so the prose was reconciled to it.
# --------------------------------------------------------------------------- #

def test_repo_read_defaults_agree_across_dev_and_reviewer() -> None:
    base = CodingAutonomyPolicy()
    assert base.dev_repo_read == base.reviewer_repo_read, (
        "the two repo-read flags must default alike — Spec 14 sets the reviewer "
        "flag from the same decision that governs the dev flag")


def test_no_docstring_still_claims_a_different_default() -> None:
    """Cheap textual drift lock: nothing near these fields may claim a default
    the dataclass does not have."""
    import inspect

    from errorta_council.coding import autonomy

    src = inspect.getsource(autonomy)
    field_default = CodingAutonomyPolicy().dev_repo_read
    claims_on = ("Default ON" in src) or ("dataclass default (True)" in src)
    assert claims_on == bool(field_default), (
        "autonomy.py prose and CodingAutonomyPolicy.dev_repo_read disagree")

    runner_src = inspect.getsource(runner.build_run_turn)
    assert ("default True" in runner_src) == bool(field_default), (
        "build_run_turn's docstring and the dataclass default disagree")


# --------------------------------------------------------------------------- #
# P0.4 — the shared read-only gate seam. Engineer B consumes these from day one;
# Spec 12 (Engineer A) later enriches latest_gate_text's content only.
# --------------------------------------------------------------------------- #

def test_gate_available_matches_the_existing_evidence_predicate(
        tmp_errorta_home: Path) -> None:
    """Equivalence lock: gate_state.gate_available is today's
    evidence._tests_required. Spec 12 may change what feeds it, but the two must
    not silently diverge in the meantime."""
    from errorta_council.coding import evidence

    store = _project("p04a")
    assert gate_state.gate_available(store) is evidence._tests_required(store)

    store.set_test_commands({"unit": {"argv": ["true"], "timeout_seconds": 5}})
    assert gate_state.gate_available(store) is True
    assert gate_state.gate_available(store) is evidence._tests_required(store)


def test_latest_gate_run_is_the_newest_record(tmp_errorta_home: Path) -> None:
    store = _project("p04b")
    assert gate_state.latest_gate_run(store) is None

    store.record_test_run(_FakeSession([_FakeResult("a")]), task_id="t1", head="h1")
    store.record_test_run(
        _FakeSession([_FakeResult("b", exit_code=0, passed=True)], passed=True),
        task_id="t2", head="h2")
    latest = gate_state.latest_gate_run(store)
    assert latest is not None and latest["head"] == "h2"


def test_latest_gate_text_is_empty_without_a_run(tmp_errorta_home: Path) -> None:
    """Empty means callers OMIT the prompt segment entirely rather than emitting
    an empty one — that is what keeps gate-less projects' goldens byte-identical."""
    store = _project("p04c")
    assert gate_state.latest_gate_text(store) == ""


def test_latest_gate_text_carries_verbatim_stderr_and_the_head(
        tmp_errorta_home: Path) -> None:
    store = _project("p04d")
    store.record_test_run(
        _FakeSession([_FakeResult(
            stdout="lev | par | strokes\n  3 |   3 |       0 FAIL",
            stderr="AssertionError: getState() returned ball: null")]),
        task_id="t1", head="deadbeef")
    text = gate_state.latest_gate_text(store)
    assert "deadbeef" in text
    assert "AssertionError: getState() returned ball: null" in text
    assert "strokes" in text
    assert "acceptance" in text


def test_latest_gate_text_respects_its_cap(tmp_errorta_home: Path) -> None:
    store = _project("p04e")
    store.record_test_run(
        _FakeSession([_FakeResult(stdout="x" * 50_000, stderr="y" * 50_000)]),
        task_id="t1", head="h")
    assert len(gate_state.latest_gate_text(store, cap=500)) <= 500


def test_gate_state_never_raises_on_a_broken_store() -> None:
    class _Broken:
        def __getattr__(self, name):
            raise RuntimeError("ledger unavailable")

    broken = _Broken()
    assert gate_state.gate_available(broken) is False
    assert gate_state.latest_gate_run(broken) is None
    assert gate_state.latest_gate_text(broken) == ""
