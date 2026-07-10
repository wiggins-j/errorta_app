"""F087-15 — head-bound merge evidence, preview-error blocker, sandbox, deps, L1."""
from __future__ import annotations

from pathlib import Path

import pytest

from errorta_council.coding.evidence import gather_merge_evidence, merge_review
from errorta_council.coding.ledger import LedgerStore, _atomic_write_json
from errorta_council.coding.workspace import CodingWorkspace


def _project(tmp_path: Path, pid: str):
    s = LedgerStore(pid, root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    # F146 Slice D: register a test command so the tests gate is REQUIRED here
    # (these head-binding tests assert `tests_missing`). A genuinely test-less,
    # non-runnable project now vacuously satisfies that gate — covered separately
    # in test_f146_gate_consistency.py.
    s.set_test_commands({"unit": {"argv": ["python", "-c", "pass"], "cwd": ".",
                                  "timeout_seconds": 30}})
    ws = CodingWorkspace(pid, s)
    ws.setup(target="new", repo_path=None)
    return s, ws


def _mark_done_dod(s):
    raw = s.get_project().to_dict()
    raw["status"] = "done"
    _atomic_write_json(s._project_path, raw)


class _PassSession:
    command_ids = ["unit"]
    results: list = []
    unknown_ids: list = []
    passed = True


# --- H1: evidence is bound to the current head ------------------------------


def test_review_and_test_bound_to_head_then_invalidated_by_new_write(tmp_path: Path) -> None:
    s, ws = _project(tmp_path, "h1")
    t = s.add_task(title="impl", role="dev")
    ws.write_file("a.py", "x = 1\n", task_id=t.task_id)
    s.update_task(t.task_id, state="done")
    _mark_done_dod(s)
    head1 = ws.head()
    s.record_decision(title="r", context="c", choice="review_approved",
                      rationale="ok", extra={"reviewed_head": head1})
    s.record_test_run(_PassSession(), task_id=t.task_id, head=head1)

    # bound to head1 -> gate allows
    assert merge_review(s, ws)["_gate"].allowed is True

    # the dev writes again: head advances, the prior review/test no longer apply
    ws.write_file("a.py", "x = 2\n", task_id=t.task_id)
    assert ws.head() != head1
    ev = gather_merge_evidence(s, ws)
    assert ev["reviewed_approved"] is None  # stale -> unreviewed
    assert ev["tests_passed"] is None       # stale -> untested
    gate = merge_review(s, ws)["_gate"]
    assert gate.allowed is False
    codes = {b.code for b in gate.blockers}
    assert "unreviewed_changes" in codes and "tests_missing" in codes


# --- M1: preview failure is a blocker, not "no conflicts" -------------------


def test_preview_failure_is_a_blocker(tmp_path: Path) -> None:
    s, ws = _project(tmp_path, "m1")

    class _Broken:
        def head(self):
            return ""
        def preview(self):
            raise RuntimeError("worktree gone")

    ev = gather_merge_evidence(s, _Broken())
    assert ev["preview_ok"] is False
    gate = merge_review(s, _Broken())["_gate"]
    assert gate.allowed is False
    assert "preview_unavailable" in {b.code for b in gate.blockers}


# --- M4: sandbox backend recorded + require_sandbox fails closed ------------


def test_test_run_records_sandbox_backend(tmp_path: Path) -> None:
    import sys
    s, ws = _project(tmp_path, "m4a")
    s.set_test_commands({"unit": {"argv": [sys.executable, "-c", "pass"], "cwd": ".",
                                  "timeout_seconds": 30}})
    from errorta_council.coding.testing import run_test_commands
    session = run_test_commands(ws.root(), s.get_test_commands(), ["unit"])
    rec = s.record_test_run(session, task_id="t", head="h")
    assert rec["sandbox"] == session.sandbox
    assert session.sandbox  # a backend string is recorded (seatbelt/bwrap/none)


def test_require_sandbox_fails_closed_without_os_sandbox(tmp_path: Path) -> None:
    import sys
    from errorta_council.coding.testing import run_test_commands
    from errorta_tools.runner.sandbox import SANDBOX_NONE
    s, ws = _project(tmp_path, "m4b")
    reg = {"unit": {"argv": [sys.executable, "-c", "pass"], "cwd": ".", "timeout_seconds": 30}}
    session = run_test_commands(ws.root(), reg, ["unit"],
                                sandbox=SANDBOX_NONE, require_sandbox=True)
    assert session.passed is False
    assert session.results[0].reason == "sandbox_unavailable"


def test_require_sandbox_setting_roundtrips(tmp_path: Path) -> None:
    s, _ws = _project(tmp_path, "m4c")
    assert s.get_require_sandbox() is False
    s.set_require_sandbox(True)
    assert LedgerStore("m4c", root=tmp_path).get_require_sandbox() is True
