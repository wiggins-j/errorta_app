"""F128 validation — a PM done=true claim never completes a run with open work.

Deterministic (no live models): reproduces the ARK-Login-Sentinel false-done — a
run reporting status=done while a blocked, human-required task remained. Drives
the real runner done-claim guard + the real escalate ladder and asserts:

  * a blocked task is classified as open AND human-required;
  * the runner REFUSES the done-claim (status stays active, never "done");
  * repeated false claims escalate to ONE blocking completion_blocked Problem
    (not a silent no_progress, not a false done);
  * once the obsolete task is dropped through the ledger path, done is accepted.

Run: ERRORTA_HOME=$(mktemp -d) python scripts/validate_f128.py
"""
from __future__ import annotations

import json
import sys
import tempfile

from errorta_council.coding import attention
from errorta_council.coding.autonomy import (
    COMPLETION_BLOCKED,
    CodingAutonomyPolicy,
    LoopCounters,
    _handle_completion_refused,
)
from errorta_council.coding.completion import pending_completion_work
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import (
    build_run_turn,
    members_by_coding_role,
)
from errorta_council.coding.topology import DEV, Plan
from errorta_council.coding.workspace import CodingWorkspace

MEMBERS = [
    {"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"}},
    {"id": "m-dev", "enabled": True, "metadata": {"coding_role": "dev"}},
    {"id": "m-rev", "enabled": True, "metadata": {"coding_role": "reviewer"}},
]

_PM_DONE = json.dumps({
    "schema_version": "coding_turn.v1", "role": "pm",
    "intent": {"kind": "plan", "done": True, "completion_summary": "all done"},
})


def _ok(label: str) -> None:
    print(f"  ✓ {label}")


def _run_pm_plan(store, name):
    ws = CodingWorkspace(name, store)
    ws.setup(target="new", repo_path=None)
    rt = build_run_turn(store, ws, members_by_coding_role(MEMBERS),
                        lambda member, prompt: _PM_DONE, guardrail_enabled=True)
    return rt(Plan(member_id="m-pm"), store)


def main() -> int:
    print("F128 validation — no false 'done' with open work\n")
    name = "f128"
    store = LedgerStore(name)
    store.create_project(north_star="n", definition_of_done="", target="new",
                         repo_path=None)
    task = store.add_task(title="Resolve merge conflict", role=DEV)
    store.update_task(task.task_id, state="blocked")  # human-required, like ARK

    # 1) The blocked task is open AND human-required.
    items = pending_completion_work(store)
    assert len(items) == 1 and items[0].state == "blocked"
    assert items[0].human_required is True
    _ok("blocked task -> open + human-required (recognized)")

    # 2) The runner refuses the PM done=true claim; status never flips to done.
    outcome = _run_pm_plan(store, name)
    assert outcome.kind == "completion_refused", outcome.kind
    assert store.get_project().status != "done"
    assert store.get_project().completion_summary == ""  # never persisted
    assert any(d["choice"] == "pm_completion_refused" for d in store.list_decisions())
    _ok("PM done=true REFUSED while a blocked task is open (status stays active)")

    # 3) Repeated false claims escalate to ONE blocking Problem, not a false done.
    c = LoopCounters()
    policy = CodingAutonomyPolicy(completion_refused_limit=2)
    assert _handle_completion_refused(store, c, policy) is None
    stop = _handle_completion_refused(store, c, policy)
    assert stop == COMPLETION_BLOCKED
    problems = [s for s in attention.list_open(name, store=store)
                if s.source == "completion_blocked"]
    assert len(problems) == 1 and problems[0].blocking is True
    assert "need you" in problems[0].summary  # flags the human-required item
    assert store.get_project().status != "done"
    _ok("repeated false done -> ONE blocking completion_blocked Problem (not no_progress)")

    # 4) Drop the obsolete task through the ledger path -> done is now accepted.
    store.update_task(task.task_id, state="dropped")
    accepted = _run_pm_plan(store, name)
    assert accepted.kind == "project_done"
    assert store.get_project().completion_summary == "all done"
    _ok("after the task is dropped through the ledger path, done is accepted")

    print("\nALL CHECKS PASSED — a run never reports done while open work remains.")
    return 0


if __name__ == "__main__":
    if "ERRORTA_HOME" not in __import__("os").environ:
        __import__("os").environ["ERRORTA_HOME"] = tempfile.mkdtemp()
    sys.exit(main())
