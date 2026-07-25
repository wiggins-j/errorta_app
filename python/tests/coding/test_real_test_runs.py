"""F087-10 — real test runs: registry + engine + grounded tester verdict.

The tester role cannot self-assert pass/fail (F087-09 removed `passed` from its
schema). These tests lock that a tester `task_done` is reachable ONLY through a
real subprocess exit code, and that every fail-closed edge blocks.

The engine tests run real (but hermetic, no-network) `python -c` subprocesses,
same posture as the F039 sandbox tests.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from errorta_council.coding.ledger import LedgerError, LedgerStore


# --- Task A: registry validation + run records ------------------------------

def _store(tmp_path: Path) -> LedgerStore:
    s = LedgerStore("proj-rt", root=tmp_path)
    s.create_project(north_star="x", definition_of_done="y",
                     target="new", repo_path=None)
    return s


def test_registry_roundtrips(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert s.get_test_commands() == {}
    cmds = {"unit": {"argv": ["python", "-c", "pass"], "cwd": ".",
                     "timeout_seconds": 30, "label": "unit"}}
    s.set_test_commands(cmds)
    # Spec 12 (S1): the stored shape gains a `scope`, defaulting to "unit" for a
    # command that didn't declare one — so pre-Spec-12 registries keep their exact
    # merge-gate meaning.
    expected = {"unit": {**cmds["unit"], "scope": "unit"}}
    assert LedgerStore("proj-rt", root=tmp_path).get_test_commands() == expected


@pytest.mark.parametrize("bad", [
    {"unit": {"argv": "python -c pass"}},                 # argv not a list
    {"unit": {"argv": []}},                                # empty argv
    {"unit": {"argv": ["python", 3]}},                    # non-str element
    {"unit": {"argv": ["x"], "timeout_seconds": 0}},      # timeout <= 0
    {"unit": {"argv": ["x"], "timeout_seconds": 10_000}}, # timeout > 600
    {"unit": {"argv": ["x"], "cwd": "/abs"}},             # absolute cwd
    {"unit": {"argv": ["x"], "cwd": "../up"}},            # traversal cwd
    {"../bad": {"argv": ["x"]}},                           # unsafe command_id
    {"x" * 65: {"argv": ["x"]}},                           # command_id too long
])
def test_registry_rejects_malformed(tmp_path: Path, bad: dict) -> None:
    s = _store(tmp_path)
    with pytest.raises(LedgerError):
        s.set_test_commands(bad)


def test_run_records_append_and_list(tmp_path: Path) -> None:
    from errorta_council.coding.testing import TestRunResult, TestRunSession
    s = _store(tmp_path)
    session = TestRunSession(
        command_ids=["unit"], unknown_ids=[], passed=True,
        results=[TestRunResult(
            command_id="unit", argv_sha256="a" * 64, status="completed",
            exit_code=0, passed=True, duration_ms=12, stdout_sha256="b" * 64,
            stdout_preview="ok", stderr_preview="", reason="")])
    s.record_test_run(session, task_id="t-1")
    runs = s.list_test_runs()
    assert len(runs) == 1
    assert runs[0]["task_id"] == "t-1"
    assert runs[0]["passed"] is True
    assert runs[0]["results"][0]["exit_code"] == 0
    assert runs[0]["results"][0]["argv_sha256"] == "a" * 64


# --- Task B: real test-run engine (hermetic subprocesses, no network) --------

_PY = sys.executable  # bare "python" may not exist on a host (macOS ships python3)
_OK = {"argv": [_PY, "-c", "import sys; sys.exit(0)"], "timeout_seconds": 30}
_FAIL = {"argv": [_PY, "-c", "import sys; sys.exit(1)"], "timeout_seconds": 30}
_SLOW = {"argv": [_PY, "-c", "import time; time.sleep(5)"], "timeout_seconds": 1}
_MISSING = {"argv": ["this-binary-does-not-exist-f087", "x"], "timeout_seconds": 5}


def test_resolve_commands_splits_known_and_unknown() -> None:
    from errorta_council.coding.testing import resolve_commands
    resolved, unknown = resolve_commands({"ok": _OK}, ["ok", "nope"])
    assert [c for c, _ in resolved] == ["ok"]
    assert unknown == ["nope"]


def test_engine_passes_on_exit_zero(tmp_path: Path) -> None:
    from errorta_council.coding.testing import run_test_commands
    session = run_test_commands(tmp_path, {"ok": _OK}, ["ok"], sandbox="none")
    assert session.passed is True
    r = session.results[0]
    assert r.status == "completed" and r.exit_code == 0 and r.passed is True
    assert len(r.argv_sha256) == 64 and len(r.stdout_sha256) == 64


def test_engine_fails_on_nonzero_exit(tmp_path: Path) -> None:
    from errorta_council.coding.testing import run_test_commands
    session = run_test_commands(tmp_path, {"bad": _FAIL}, ["bad"], sandbox="none")
    assert session.passed is False
    assert session.results[0].status == "failed"
    assert session.results[0].exit_code == 1


def test_engine_fails_closed_on_timeout(tmp_path: Path) -> None:
    from errorta_council.coding.testing import run_test_commands
    session = run_test_commands(tmp_path, {"slow": _SLOW}, ["slow"], sandbox="none")
    assert session.passed is False
    assert session.results[0].status == "timed_out"


def test_engine_unknown_id_never_passes(tmp_path: Path) -> None:
    from errorta_council.coding.testing import run_test_commands
    session = run_test_commands(tmp_path, {"ok": _OK}, ["ok", "ghost"], sandbox="none")
    assert session.unknown_ids == ["ghost"]
    assert session.passed is False


def test_engine_empty_plan_never_passes(tmp_path: Path) -> None:
    from errorta_council.coding.testing import run_test_commands
    session = run_test_commands(tmp_path, {"ok": _OK}, [], sandbox="none")
    assert session.passed is False


def test_engine_missing_binary_fails_closed(tmp_path: Path) -> None:
    from errorta_council.coding.testing import run_test_commands
    session = run_test_commands(tmp_path, {"x": _MISSING}, ["x"], sandbox="none")
    assert session.passed is False
    assert session.results[0].status in ("failed", "blocked")


# --- Task C: tester branch grounds the verdict in a real run -----------------

_MEMBERS = [
    {"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"}},
    {"id": "m-test", "enabled": True, "metadata": {"coding_role": "tester"}},
]


def _tester_envelope(task_id: str, command_ids: list[str]) -> str:
    import json
    return json.dumps({
        "schema_version": "coding_turn.v1", "role": "tester", "task_id": task_id,
        "intent": {"kind": "test_plan", "command_ids": command_ids,
                   "scope": "full_project", "rationale": "validate"},
    })


class _FakeWorkspace:
    def __init__(self, root: Path) -> None:
        self._root = root

    def root(self) -> Path:
        return self._root

    def head(self) -> str:
        return "fakehead"

    def checkout(self, branch: str) -> None:  # F087-17: tester checks out the PR
        pass


def _tester_turn(store, workspace, caller):
    """F087-17: the tester now acts on a PR. Open one + a tester task bound to it,
    run the tester turn, and return (pr_id, outcome)."""
    from errorta_council.coding.runner import build_run_turn, members_by_coding_role
    from errorta_council.coding.topology import Assign, TESTER
    pr = store.record_pr(task_id="t-dev", branch="task-x",
                         head=workspace.head(), dev_member="m-dev")
    store.update_pr(pr["pr_id"], reviewer_approved=True, reviewed_head=pr["head"])
    vt = store.add_task(title="test PR: impl", role=TESTER, pr_id=pr["pr_id"])
    rt = build_run_turn(store, workspace, members_by_coding_role(_MEMBERS), caller,
                        guardrail_enabled=True)
    rt(Assign(member_id="m-test", task_id=vt.task_id, role=TESTER), store)
    return pr["pr_id"], store.get_pr(pr["pr_id"])


def test_tester_passes_only_on_real_green_run(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_test_commands({"unit": _OK})

    def caller(member, prompt):
        import re
        tid = re.search(r"tester for task id '([^']+)'", prompt).group(1)
        return _tester_envelope(tid, ["unit"])

    pr_id, pr = _tester_turn(s, _FakeWorkspace(tmp_path), caller)
    assert pr["tests_passed"] is True
    assert pr["status"] == "mergeable"  # reviewer-approved + tests-green
    runs = s.list_test_runs()
    assert runs and runs[0]["passed"] is True and runs[0]["results"][0]["exit_code"] == 0
    assert any(d["choice"] == "tested_pass" for d in s.list_decisions())


def test_tester_blocks_on_real_red_run(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_test_commands({"unit": _FAIL})

    def caller(member, prompt):
        import re
        tid = re.search(r"tester for task id '([^']+)'", prompt).group(1)
        return _tester_envelope(tid, ["unit"])

    _pr_id, pr = _tester_turn(s, _FakeWorkspace(tmp_path), caller)
    assert pr["tests_passed"] is False and pr["status"] == "changes_requested"
    assert s.list_test_runs()[0]["passed"] is False
    assert any(d["choice"] == "tested_fail" for d in s.list_decisions())


def test_tester_blocks_on_unknown_command_id(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_test_commands({"unit": _OK})

    def caller(member, prompt):
        import re
        tid = re.search(r"tester for task id '([^']+)'", prompt).group(1)
        return _tester_envelope(tid, ["ghost"])

    _pr_id, pr = _tester_turn(s, _FakeWorkspace(tmp_path), caller)
    assert pr["status"] == "changes_requested"
    assert s.list_test_runs() == []  # never ran a typo'd plan
    assert any(d["choice"] == "invalid_test_command" for d in s.list_decisions())


def test_tester_blocks_on_non_json(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_test_commands({"unit": _OK})

    def caller(member, prompt):
        return "not json at all"

    _pr_id, pr = _tester_turn(s, _FakeWorkspace(tmp_path), caller)
    assert pr["status"] == "changes_requested"
    assert any(d["choice"] == "tester_turn_rejected" for d in s.list_decisions())


def test_legacy_self_report_no_longer_validates(tmp_path: Path) -> None:
    # A bare {"passed": true} self-report is NOT a valid coding_turn.v1 envelope
    # -> the PR is never marked tests-green. This is the whole point.
    s = _store(tmp_path)
    s.set_test_commands({"unit": _OK})

    def caller(member, prompt):
        import json
        return json.dumps({"passed": True, "output": "1 passed"})

    _pr_id, pr = _tester_turn(s, _FakeWorkspace(tmp_path), caller)
    assert pr["status"] == "changes_requested"
    assert s.list_test_runs() == []
