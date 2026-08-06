"""F156 — the delivery gate cannot be satisfied without real verification.

Two audit findings from the F152 family, both "success without verification":

* **G7** — ``delivery_review``'s ``no_reviewer`` early-return sat BEFORE the tests
  and launch steps, so a team with neither REVIEWER nor PM reached ``project_done``
  with *zero* delivery verification. The short-circuit was broader than its name:
  "no reviewer" silently also meant "no tests, no launch check, no web probe". Only
  the reviewer VERDICT should be skipped when there is genuinely no reviewer.
* **G5** — a tester turn with ``not_applicable=true`` stamps ``tests_passed=True``
  and raises only a deduped non-blocking alert, so a lazy tester can merge every PR
  by declaring each slice not-applicable. Not forbidden (a partial slice genuinely
  has no test) but bounded and surfaced.
"""
import json
import re
import sys
from pathlib import Path

from errorta_council.coding.autonomy import (
    CADENCE_OFF,
    CodingAutonomyPolicy,
    policy_from_dict,
    policy_to_dict,
    save_policy,
)
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import (
    CodingRunner,
    build_run_turn,
    members_by_coding_role,
)

# A team with a PM (so the run can plan) — used for the control cases.
MEMBERS = [
    {"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"}},
    {"id": "m-dev", "enabled": True, "metadata": {"coding_role": "dev"}},
    {"id": "m-rev", "enabled": True, "metadata": {"coding_role": "reviewer"}},
    {"id": "m-test", "enabled": True, "metadata": {"coding_role": "tester"}},
]

_ADD = "def add(a, b):\n    return a + b\n"


def _task_id(prompt: str, role: str) -> str:
    return re.search(rf"{role} for task id '([^']+)'", prompt).group(1)


def _pr_head(prompt: str) -> str:
    return re.search(r"PR head you are reviewing is '([^']*)'", prompt).group(1)


def _delivery_head(prompt: str) -> str:
    return re.search(r"delivered head you are reviewing is '([^']*)'", prompt).group(1)


def _pm_env(*, tasks=None, done=False, completion_summary="") -> str:
    intent = {"kind": "plan", "done": done}
    if tasks is not None:
        intent["tasks"] = tasks
    if completion_summary:
        intent["completion_summary"] = completion_summary
    return json.dumps({"schema_version": "coding_turn.v1", "role": "pm",
                       "intent": intent})


def _dev_env(task_id: str, files) -> str:
    return json.dumps({
        "schema_version": "coding_turn.v1", "role": "dev", "task_id": task_id,
        "intent": {"kind": "tool_plan", "task_type": "implementation",
                   "tool_calls": [{"tool": "code_write",
                                   "args": {"path": p, "content": c}}
                                  for p, c in files]}})


def _rev_env(task_id: str, head: str, *, approved=True, findings=None) -> str:
    return json.dumps({
        "schema_version": "coding_turn.v1", "role": "reviewer", "task_id": task_id,
        "intent": {"kind": "review_verdict", "reviewed_head": head,
                   "approved": approved, "findings": findings or []}})


def _tester_env(task_id: str, command_ids, *, not_applicable=False,
                rationale="run") -> str:
    return json.dumps({
        "schema_version": "coding_turn.v1", "role": "tester", "task_id": task_id,
        "intent": {"kind": "test_plan", "command_ids": command_ids,
                   "scope": "full_project", "not_applicable": not_applicable,
                   "rationale": rationale}})


class _Fake:
    """One dev task, then done. Tester behavior is configurable so G5's
    not_applicable path can be driven."""

    def __init__(self, *, tester_not_applicable: bool = False,
                 n_tasks: int = 1) -> None:
        self.pm_calls = 0
        self.tester_not_applicable = tester_not_applicable
        self.n_tasks = n_tasks
        self.delivery_review_calls = 0

    def __call__(self, member: dict, prompt: str) -> str:
        if "DELIVERY reviewer" in prompt:
            self.delivery_review_calls += 1
            return _rev_env("delivery-review", _delivery_head(prompt), approved=True)
        if "You are the PM" in prompt:
            self.pm_calls += 1
            if self.pm_calls == 1:
                return _pm_env(tasks=[
                    {"title": f"implement add {i}", "role": "dev"}
                    for i in range(self.n_tasks)])
            return _pm_env(done=True, completion_summary="done")
        if "You are a developer" in prompt:
            tid = _task_id(prompt, "developer")
            return _dev_env(tid, [(f"calc_{tid[:6]}.py", _ADD)])
        if "You are a reviewer" in prompt:
            return _rev_env(_task_id(prompt, "reviewer"), _pr_head(prompt),
                            approved=True)
        if "You are a tester" in prompt:
            tid = _task_id(prompt, "tester")
            if self.tester_not_applicable:
                return _tester_env(tid, [], not_applicable=True,
                                   rationale="no command exercises this slice")
            return _tester_env(tid, ["unit"])
        return "{}"


_PASS_CMD = {"unit": {
    "argv": [sys.executable, "-c", "pass"], "cwd": ".", "timeout_seconds": 30}}


def _make(pid: str, cmds: dict | None = None) -> LedgerStore:
    store = LedgerStore(pid)
    store.create_project(north_star="calc with add",
                         definition_of_done="add works", target="new",
                         repo_path=None)
    if cmds:
        store.set_test_commands(cmds)
    return store


# --------------------------------------------------------------------------- #
# G7 — a missing reviewer skips only the VERDICT, never the deterministic checks
# --------------------------------------------------------------------------- #
def test_no_reviewer_still_runs_delivery_tests(tmp_errorta_home: Path) -> None:
    """The G7 lock: a reviewer-less team still runs the registered suite.

    Before F156 the early return fired before step 2, so ``delivery_review``
    returned ``passed=True, reason="no_reviewer"`` having executed nothing. A
    failing suite at the delivered head must now block ``done``.
    """
    store = _make("f156-g7-tests", None)
    runner = CodingRunner("f156-g7-tests", MEMBERS, _Fake(), guardrail_enabled=True)
    runner.run(CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF,
                                    max_iterations=40))
    # Register a FAILING command and bust the once-per-head cache, then verify with
    # a team that has NEITHER a reviewer NOR a PM.
    store.set_test_commands({"unit": {
        "argv": [sys.executable, "-c", "import sys; sys.exit(1)"],
        "cwd": ".", "timeout_seconds": 30}})
    store.set_run_state(delivery_reviewed_head="__stale__")

    def _never_called(member: dict, prompt: str) -> str:  # pragma: no cover
        raise AssertionError("no reviewer configured — no model call is legal")

    dev_only = [{"id": "m-dev", "enabled": True,
                 "metadata": {"coding_role": "dev"}}]
    rt = build_run_turn(store, runner.workspace, members_by_coding_role(dev_only),
                        _never_called, guardrail_enabled=True)
    result = rt.delivery_review(store)
    assert result.passed is False, result
    assert any(t.title == "fix delivery tests" for t in store.list_tasks())


def test_no_reviewer_clean_app_completes(tmp_errorta_home: Path) -> None:
    """The degenerate-but-working case is not regressed.

    ``approved`` defaults True when no reviewer exists — a team that cannot produce
    a verdict must not be blocked by its absence — so a clean tree still passes,
    under a distinct reason so the reviewer-less path stays visible in the record.
    """
    store = _make("f156-g7-clean", None)
    runner = CodingRunner("f156-g7-clean", MEMBERS, _Fake(), guardrail_enabled=True)
    runner.run(CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF,
                                    max_iterations=40))
    store.set_test_commands(_PASS_CMD)
    store.set_run_state(delivery_reviewed_head="__stale__")

    def _never_called(member: dict, prompt: str) -> str:  # pragma: no cover
        raise AssertionError("no reviewer configured — no model call is legal")

    dev_only = [{"id": "m-dev", "enabled": True,
                 "metadata": {"coding_role": "dev"}}]
    rt = build_run_turn(store, runner.workspace, members_by_coding_role(dev_only),
                        _never_called, guardrail_enabled=True)
    result = rt.delivery_review(store)
    assert result.passed is True, result
    assert result.reason == "reviewed_no_reviewer", result


# --------------------------------------------------------------------------- #
# G5 — not_applicable is bounded and surfaced
# --------------------------------------------------------------------------- #
def test_not_applicable_soft_limit_knob_roundtrips() -> None:
    p = CodingAutonomyPolicy()
    assert p.not_applicable_soft_limit == 3
    d = policy_to_dict(p)
    assert d["not_applicable_soft_limit"] == 3
    assert policy_from_dict({**d, "not_applicable_soft_limit": 0}
                            ).not_applicable_soft_limit == 0


def test_not_applicable_below_limit_merges_quietly(tmp_errorta_home: Path) -> None:
    """Partial slices genuinely lack tests — the first few are unchanged."""
    store = _make("f156-g5-under", _PASS_CMD)
    runner = CodingRunner("f156-g5-under", MEMBERS,
                          _Fake(tester_not_applicable=True, n_tasks=1),
                          guardrail_enabled=True)
    runner.run(CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF,
                                    max_iterations=40))
    assert store.get_run_state().get("tests_not_applicable_count") == 1
    choices = [d.get("choice") for d in store.list_decisions()]
    assert "tests_not_applicable" in choices
    # Under the limit nothing is escalated.
    assert "tests_not_applicable_over_limit" not in choices


def test_not_applicable_over_limit_escalates(tmp_errorta_home: Path) -> None:
    """Crossing the soft limit makes the run's reliance on the escape visible.

    The merge gate is running on review alone for these slices; past the limit the
    operator is told so, instead of it passing silently forever.

    NOTE the knob must be PERSISTED, not passed to ``run()``: turn-side code reads
    ``load_policy(store)`` (autonomy.json), which is how ``control_actions`` /
    ``pm_changes`` configure it in production. A policy handed to ``run()`` is never
    written to disk, so setting it there would leave the default in force and make
    this test pass for the wrong reason.
    """
    store = _make("f156-g5-over", _PASS_CMD)
    save_policy(store, CodingAutonomyPolicy(not_applicable_soft_limit=2))
    runner = CodingRunner("f156-g5-over", MEMBERS,
                          _Fake(tester_not_applicable=True, n_tasks=4),
                          guardrail_enabled=True)
    runner.run(CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF,
                                    max_iterations=60))
    assert store.get_run_state().get("tests_not_applicable_count", 0) >= 3
    choices = [d.get("choice") for d in store.list_decisions()]
    assert "tests_not_applicable_over_limit" in choices, choices


def test_not_applicable_limit_zero_disables_escalation(
        tmp_errorta_home: Path) -> None:
    """The escape hatch: 0 restores today's always-non-blocking alert."""
    store = _make("f156-g5-off", _PASS_CMD)
    save_policy(store, CodingAutonomyPolicy(not_applicable_soft_limit=0))
    runner = CodingRunner("f156-g5-off", MEMBERS,
                          _Fake(tester_not_applicable=True, n_tasks=5),
                          guardrail_enabled=True)
    runner.run(CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF,
                                    max_iterations=60))
    # The escape itself still works — only the escalation is suppressed.
    assert store.get_run_state().get("tests_not_applicable_count", 0) >= 1
    choices = [d.get("choice") for d in store.list_decisions()]
    assert "tests_not_applicable_over_limit" not in choices
