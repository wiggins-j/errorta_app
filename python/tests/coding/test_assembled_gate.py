"""Spec 05 Phase A — the honest assembled-run gate closes the vacuous-pass loophole.

A web/app deliverable with NO registered test commands and NO runnable runtime
profile used to be *vacuously done* (tests_passed=None + tests_required=False let
the merge gate through), so a broken-green-square index.html could ship a
"12/12 PASS". When the ``assembled_run_required`` policy is on, such a deliverable
now raises the ``assembled_run_unverified`` blocker unless the operator overrides.
Library/CLI projects and web projects that DO have a test command or a runnable
profile are unaffected.
"""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding.diff_review import MergeGate, evaluate_merge_gate
from errorta_council.coding.evidence import (
    _assembled_run_unverified,
    _looks_like_web_app,
    gather_merge_evidence,
    merge_review,
)
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runtime import RuntimeProfile, RuntimeProfileStore
from errorta_council.coding.workspace import CodingWorkspace

# --- evaluate_merge_gate: the pure blocker ---------------------------------- #

_CLEAR_TASKS = [
    {"taskId": "t1", "state": "done"},
    {"taskId": "t2", "state": "dropped"},
]


def _gate(**over) -> MergeGate:
    base = dict(
        tasks=list(_CLEAR_TASKS),
        reviewed_approved=True,
        tests_passed=True,
        conflicts=[],
        definition_of_done_met=True,
    )
    base.update(over)
    return evaluate_merge_gate(**base)


def _codes(gate: MergeGate) -> set[str]:
    return {b.code for b in gate.blockers}


def test_assembled_run_unverified_blocks() -> None:
    g = _gate(assembled_run_unverified=True)
    assert g.allowed is False
    assert "assembled_run_unverified" in _codes(g)


def test_assembled_run_unverified_default_off_does_not_block() -> None:
    # Additive + off by default: the parameter is absent -> no new blocker.
    assert "assembled_run_unverified" not in _codes(_gate())
    assert _gate().allowed is True


def test_assembled_run_unverified_is_overridable() -> None:
    # Every blocker is operator-overridable via allow_override (the accept route's
    # override:true path clears them); this one is no exception.
    g = _gate(assembled_run_unverified=True)
    assert g.allow_override is True


# --- integration: gather_merge_evidence / merge_review ----------------------- #


def _project(pid: str, tmp_path: Path) -> tuple[LedgerStore, CodingWorkspace]:
    store = LedgerStore(pid, root=tmp_path / f"ledger-{pid}")
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    ws = CodingWorkspace(pid, store)
    ws.setup(target="new", repo_path=None)
    return store, ws


def _merge_file(ws: CodingWorkspace, task_id: str, path: str, content: str) -> None:
    branch = ws.start_task_branch(task_id)
    ws.write_file(path, content, task_id=task_id)
    res = ws.merge_pr(branch)
    assert res.get("merged"), res


def test_web_app_no_tests_no_profile_blocks_when_required(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    # The exact vacuous case: an index.html deliverable, no test commands, no
    # runnable profile, policy ON -> the merge gate refuses.
    store, ws = _project("web-vac", tmp_path)
    _merge_file(ws, "t1", "index.html", "<html><body>app</body></html>\n")
    store.set_assembled_run_required(True)

    assert _looks_like_web_app(store, ws) is True
    assert _assembled_run_unverified(store, ws) is True

    ev = gather_merge_evidence(store, ws)
    assert ev["assembled_run_unverified"] is True

    gate = merge_review(store, ws)["_gate"]
    assert gate.allowed is False
    assert "assembled_run_unverified" in {b.code for b in gate.blockers}


def test_web_app_override_clears_blocker(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    # The accept route clears blockers when the operator passes override:true;
    # the gate itself always permits that (allow_override).
    store, ws = _project("web-ovr", tmp_path)
    _merge_file(ws, "t1", "index.html", "<html></html>\n")
    store.set_assembled_run_required(True)

    gate = merge_review(store, ws)["_gate"]
    assert gate.allowed is False
    assert gate.allow_override is True
    # override:true at the accept route proceeds despite `not gate.allowed`.


def test_policy_off_is_not_blocked(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    # Default (policy off): a web deliverable is NOT blocked -> no regression to
    # existing green projects.
    store, ws = _project("web-off", tmp_path)
    _merge_file(ws, "t1", "index.html", "<html></html>\n")
    # assembled_run_required defaults False.
    assert _assembled_run_unverified(store, ws) is False
    ev = gather_merge_evidence(store, ws)
    assert ev["assembled_run_unverified"] is False


def test_library_cli_project_is_not_blocked(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    # A non-web deliverable (no index.html, no web/static profile) is never the
    # target of this gate, even with the policy on.
    store, ws = _project("lib", tmp_path)
    _merge_file(ws, "t1", "src/lib.py", "def f():\n    return 1\n")
    store.set_assembled_run_required(True)
    assert _looks_like_web_app(store, ws) is False
    assert _assembled_run_unverified(store, ws) is False
    ev = gather_merge_evidence(store, ws)
    assert ev["assembled_run_unverified"] is False


def test_web_app_with_test_command_is_not_blocked(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    # The loophole only covers the vacuous case: a web deliverable that DOES have
    # a registered acceptance/test command has something to verify it -> not
    # blocked by this gate.
    store, ws = _project("web-tests", tmp_path)
    _merge_file(ws, "t1", "index.html", "<html></html>\n")
    store.set_assembled_run_required(True)
    store.set_test_commands({"unit": {"argv": ["python", "-c", "pass"], "cwd": ".",
                                      "timeout_seconds": 30}})
    assert _assembled_run_unverified(store, ws) is False


def test_web_app_with_runnable_profile_is_not_blocked(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    # A web deliverable with a runnable runtime profile (a `start` argv) can be
    # launched/probed -> not the vacuous case, not blocked.
    store, ws = _project("web-run", tmp_path)
    _merge_file(ws, "t1", "index.html", "<html></html>\n")
    store.set_assembled_run_required(True)
    RuntimeProfileStore.for_ledger(store).upsert_profile(RuntimeProfile(
        profile_id="default", project_id="web-run", kind="static",
        runtime_mode="managed_local",
        start=["python", "-m", "http.server", "{port}"]))
    assert _assembled_run_unverified(store, ws) is False
