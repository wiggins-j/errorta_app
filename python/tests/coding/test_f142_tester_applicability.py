"""F142 WS-C — tester applicability gate (acceptance criterion 5).

Once F142 Slice 2's foundation gate arms a manifest, a full-suite test command
can be registered on a project. Every incremental PR would then face that gate,
and a foundation slice whose imports reference not-yet-built modules would fail
"incomplete = fail" one layer down — re-deadlocking the run.

WS-C gives the tester a `not_applicable` outcome: when NO registered command
meaningfully exercises this slice, the test gate is non-blocking for that slice.

The ABSOLUTE guardrail (spec Risks / AC5 second half): the applicability path
must NEVER swallow a test command that actually ran and returned non-zero for a
real defect. `not_applicable` is honored ONLY when `command_ids` is empty; if
the tester names commands, they run and real exit codes govern regardless of the
flag.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from errorta_council.coding.ledger import LedgerStore

_PY = sys.executable
_OK = {"argv": [_PY, "-c", "import sys; sys.exit(0)"], "timeout_seconds": 30}
_FAIL = {"argv": [_PY, "-c", "import sys; sys.exit(1)"], "timeout_seconds": 30}

_MEMBERS = [
    {"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"}},
    {"id": "m-test", "enabled": True, "metadata": {"coding_role": "tester"}},
]


def _store(tmp_path: Path) -> LedgerStore:
    s = LedgerStore("proj-f142", root=tmp_path)
    s.create_project(north_star="build a game", definition_of_done="runnable",
                     target="new", repo_path=None)
    return s


def _tester_envelope(task_id: str, command_ids: list[str], *,
                     not_applicable: bool = False, rationale: str = "validate") -> str:
    intent: dict = {"kind": "test_plan", "command_ids": command_ids,
                    "scope": "full_project", "rationale": rationale}
    if not_applicable:
        intent["not_applicable"] = True
    return json.dumps({
        "schema_version": "coding_turn.v1", "role": "tester", "task_id": task_id,
        "intent": intent,
    })


class _FakeWorkspace:
    def __init__(self, root: Path) -> None:
        self._root = root

    def root(self) -> Path:
        return self._root

    def head(self) -> str:
        return "fakehead"

    def checkout(self, branch: str) -> None:
        pass


def _tester_turn(store, workspace, caller):
    from errorta_council.coding.runner import (
        build_run_turn,
        members_by_coding_role,
    )
    from errorta_council.coding.topology import TESTER, Assign
    pr = store.record_pr(task_id="t-dev", branch="task-foundation",
                         head=workspace.head(), dev_member="m-dev")
    store.update_pr(pr["pr_id"], reviewer_approved=True, reviewed_head=pr["head"])
    vt = store.add_task(title="test PR: foundation", role=TESTER, pr_id=pr["pr_id"])
    rt = build_run_turn(store, workspace, members_by_coding_role(_MEMBERS), caller,
                        guardrail_enabled=True)
    rt(Assign(member_id="m-test", task_id=vt.task_id, role=TESTER), store)
    return pr["pr_id"], store.get_pr(pr["pr_id"])


def _caller_for(command_ids, *, not_applicable=False, rationale="validate"):
    def caller(member, prompt):
        tid = re.search(r"tester for task id '([^']+)'", prompt).group(1)
        return _tester_envelope(tid, command_ids, not_applicable=not_applicable,
                                rationale=rationale)
    return caller


# --- AC5: not-applicable slice is non-blocking -------------------------------

def test_not_applicable_slice_is_non_blocking(tmp_path: Path) -> None:
    """A foundation slice whose full-suite command would only fail on
    not-yet-built modules: the tester declares not_applicable + empty command_ids
    -> PR ends tests_passed True, NOT changes_requested, and a
    `tests_not_applicable` decision is recorded. No test run occurs."""
    s = _store(tmp_path)
    # An armed full-suite command exists (post-Slice-2 manifest) — but the tester
    # judges it doesn't exercise this slice yet.
    s.set_test_commands({"suite": _OK})
    caller = _caller_for([], not_applicable=True,
                         rationale="game.py not runnable end-to-end yet")

    _pr_id, pr = _tester_turn(s, _FakeWorkspace(tmp_path), caller)

    assert pr["tests_passed"] is True
    assert pr["status"] != "changes_requested"
    assert pr["status"] == "mergeable"  # reviewer-approved + tests non-blocking
    # No command ran — this is NOT a false pass of a suite.
    assert s.list_test_runs() == []
    decisions = s.list_decisions()
    assert any(d["choice"] == "tests_not_applicable" for d in decisions)
    # No `fix tests` DEV task spawned.
    from errorta_council.coding.topology import DEV
    assert not any(t.role == DEV and t.title.startswith("fix tests")
                   for t in s.list_tasks())


def test_not_applicable_raises_a_nonblocking_tests_skipped_alert(
        tmp_path: Path) -> None:
    """Review finding B1/A6 (observability): a slice waved through as
    not-applicable must surface a NON-blocking attention Alert so a human sees a
    PR merged without tests — otherwise the run can reach 'done' with tests never
    run and nothing telling the operator. Deduped to one open alert per run."""
    from errorta_council.coding import attention
    s = _store(tmp_path)
    s.set_test_commands({"suite": _OK})
    caller = _caller_for([], not_applicable=True, rationale="not runnable yet")

    _tester_turn(s, _FakeWorkspace(tmp_path), caller)
    # A second not-applicable turn must NOT stack a duplicate alert.
    _tester_turn(s, _FakeWorkspace(tmp_path), caller)

    alerts = [a for a in attention.list_open("proj-f142", store=s)
              if a.source == "tests_skipped"]
    assert len(alerts) == 1
    assert alerts[0].kind == "alert"
    assert alerts[0].blocking is False


# --- AC5 GUARDRAIL: a ran-and-failed command can never be masked -------------

def test_not_applicable_cannot_mask_a_real_failing_command(tmp_path: Path) -> None:
    """The critical guardrail: not_applicable=true BUT a non-empty command_ids
    naming a command that RUNS and FAILS (non-zero exit) must still block. The
    flag is ignored when commands are named; real exit codes govern."""
    s = _store(tmp_path)
    s.set_test_commands({"unit": _FAIL})
    # Adversarial tester tries to mask a genuine failure by also flagging N/A.
    caller = _caller_for(["unit"], not_applicable=True,
                         rationale="pretending this isn't relevant")

    _pr_id, pr = _tester_turn(s, _FakeWorkspace(tmp_path), caller)

    assert pr["tests_passed"] is False
    assert pr["status"] == "changes_requested"
    # The command actually ran and failed.
    runs = s.list_test_runs()
    assert runs and runs[0]["passed"] is False
    assert runs[0]["results"][0]["exit_code"] == 1
    decisions = s.list_decisions()
    assert any(d["choice"] == "tested_fail" for d in decisions)
    # It was NOT recorded as not-applicable.
    assert not any(d["choice"] == "tests_not_applicable" for d in decisions)


# --- AC5: a normal passing command still passes ------------------------------

def test_normal_passing_command_still_passes(tmp_path: Path) -> None:
    """Baseline: a real green run still marks the PR tests_passed and mergeable —
    the applicability field defaults false and doesn't disturb the normal path."""
    s = _store(tmp_path)
    s.set_test_commands({"unit": _OK})
    caller = _caller_for(["unit"])  # not_applicable defaults false

    _pr_id, pr = _tester_turn(s, _FakeWorkspace(tmp_path), caller)

    assert pr["tests_passed"] is True
    assert pr["status"] == "mergeable"
    runs = s.list_test_runs()
    assert runs and runs[0]["passed"] is True
    assert any(d["choice"] == "tested_pass" for d in s.list_decisions())


def test_not_applicable_defaults_false_backward_compatible(tmp_path: Path) -> None:
    """An envelope WITHOUT the new field parses and runs commands exactly as
    before (schema addition is optional/backward-compatible)."""
    s = _store(tmp_path)
    s.set_test_commands({"unit": _FAIL})

    def caller(member, prompt):
        tid = re.search(r"tester for task id '([^']+)'", prompt).group(1)
        # Legacy envelope: no not_applicable key at all.
        return json.dumps({
            "schema_version": "coding_turn.v1", "role": "tester", "task_id": tid,
            "intent": {"kind": "test_plan", "command_ids": ["unit"],
                       "scope": "full_project", "rationale": "legacy"},
        })

    _pr_id, pr = _tester_turn(s, _FakeWorkspace(tmp_path), caller)
    assert pr["tests_passed"] is False
    assert pr["status"] == "changes_requested"


def test_empty_plan_without_not_applicable_still_blocks(tmp_path: Path) -> None:
    """An empty command_ids WITHOUT not_applicable is a schema violation (parse
    error) -> the PR is changes_requested, never silently non-blocking."""
    s = _store(tmp_path)
    s.set_test_commands({"unit": _OK})
    caller = _caller_for([], not_applicable=False)

    _pr_id, pr = _tester_turn(s, _FakeWorkspace(tmp_path), caller)
    assert pr["status"] == "changes_requested"
    assert s.list_test_runs() == []
    assert any(d["choice"] == "tester_turn_rejected" for d in s.list_decisions())
