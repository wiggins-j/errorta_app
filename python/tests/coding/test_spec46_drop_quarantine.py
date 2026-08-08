from __future__ import annotations

from pathlib import Path

from errorta_council.coding import drop_ledger, task_dedupe
from errorta_council.coding.ledger import LedgerStore


def _store(tmp_path: Path, name: str = "p46") -> LedgerStore:
    s = LedgerStore(name, root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new",
                     repo_path=None)
    return s


def test_drop_ledger_counts_per_identity(tmp_path):
    store = _store(tmp_path)
    key = task_dedupe.identity_key(title="wire the integration layer", paths=[])
    assert drop_ledger.drop_count(store, key) == 0
    assert drop_ledger.record_drop(store, key) == 1
    assert drop_ledger.record_drop(store, key) == 2
    assert drop_ledger.drop_count(store, key) == 2
    # A different identity is independent.
    other = task_dedupe.identity_key(title="add a config flag", paths=[])
    assert drop_ledger.drop_count(store, other) == 0


class _Intent:
    def __init__(self, cancel_task_ids):
        self.cancel_task_ids = cancel_task_ids


def test_drop_decision_carries_reason_and_count(tmp_path):
    from errorta_council.coding.runner import _apply_pm_cancels
    store = _store(tmp_path)
    t = store.add_task(title="wire the integration layer", role="dev")
    _apply_pm_cancels(store, _Intent([t.task_id]))
    dec = next(d for d in store.list_decisions()
               if d.get("choice") == "pm_task_cancelled")
    assert dec["reason_code"] == "pm_pruned"
    assert dec["drop_count"] == 1


def test_quarantine_limit_default_and_ordering():
    from errorta_council.coding.autonomy import CodingAutonomyPolicy
    p = CodingAutonomyPolicy()
    assert p.task_drop_quarantine_limit == 3
    # MUST fire before planning_churn, or the whole run halts first.
    assert p.task_drop_quarantine_limit < p.plan_streak_limit


def test_task_pathology_problem_is_deduped(tmp_path):
    from errorta_council.coding import attention
    store = _store(tmp_path)
    first = attention.raise_task_pathology_problem(
        store.project_id, identity="k1", title="wire the integration layer",
        drops=3, reason_code="pm_pruned", store=store)
    assert first is not None
    dupe = attention.raise_task_pathology_problem(
        store.project_id, identity="k1", title="wire the integration layer",
        drops=4, reason_code="pm_pruned", store=store)
    assert dupe is None  # one open Problem per identity, no stacking
    opens = [s for s in attention.list_open(store.project_id, store=store)
             if s.source == "task_pathology"]
    assert len(opens) == 1
    assert "pm_pruned" in opens[0].summary or "pm_pruned" in (opens[0].pm_evaluation or "")


class _Planned:
    def __init__(self, title, detail=""):
        self.title = title; self.detail = detail; self.depends_on = []
        self.task_type = "implementation"; self.difficulty_tier = "mid"
        self.preferred_member_id = ""; self.preferred_route_id = ""
        self.assignment_rationale = ""


class _PlanIntent:
    def __init__(self, tasks):
        self.tasks = tasks


def test_task_is_quarantined_at_threshold(tmp_path):
    from errorta_council.coding import drop_ledger, task_dedupe, attention
    from errorta_council.coding.runner import _materialize_pm_tasks
    store = _store(tmp_path)
    title = "consolidate the module registry"
    key = task_dedupe.identity_key(title=title, paths=[])
    # Simulate 3 prior drops of this identity.
    for _ in range(3):
        drop_ledger.record_drop(store, key)
    created = _materialize_pm_tasks(store, _PlanIntent([_Planned(title)]))
    # At the limit, the task is NOT created; a quarantine decision + Problem exist.
    assert created == []
    assert any(d.get("choice") == "task_quarantined" for d in store.list_decisions())
    opens = [s for s in attention.list_open(store.project_id, store=store)
             if s.source == "task_pathology"]
    assert len(opens) == 1


def test_quarantine_stop_reason_is_benign_exit():
    from errorta_cli import runstream
    payload = {"running": False,
               "state": {"status": "stopped",
                         "stop_reason": "quarantined_task_needs_input"}}
    assert runstream.classify_exit(payload) == runstream.EXIT_OK
    assert "quarantine" in runstream.gloss("quarantined_task_needs_input").lower()


def test_quarantine_isolates_one_task_backlog_continues(tmp_path):
    """A task dropped to the threshold is suppressed on the next materialize while a
    fresh, distinct task still gets created — the loop is not wedged on the bad one."""
    from errorta_council.coding import drop_ledger, task_dedupe
    from errorta_council.coding.runner import _materialize_pm_tasks
    store = _store(tmp_path)
    bad = "reconcile the cross-module event bus"
    key = task_dedupe.identity_key(title=bad, paths=[])
    for _ in range(3):
        drop_ledger.record_drop(store, key)
    created = _materialize_pm_tasks(store, _PlanIntent([
        _Planned(bad), _Planned("add a --verbose flag to the CLI")]))
    titles = [t.title for t in created]
    assert bad not in titles                       # quarantined
    assert "add a --verbose flag to the CLI" in titles  # unrelated work proceeds


# --------------------------------------------------------------------------- #
# The ledger is specified PER-RUN. Left uncleared in `run_state.json`, a task the
# PM prunes once in each of three separate runs is silently quarantined on run 4.
# --------------------------------------------------------------------------- #
_PM_MEMBERS = [
    {"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"}},
    {"id": "m-dev", "enabled": True, "metadata": {"coding_role": "dev"}},
]


def _done_pm(member, prompt):
    import json
    return json.dumps({"schema_version": "coding_turn.v1", "role": "pm",
                       "intent": {"kind": "plan", "done": True,
                                  "completion_summary": "done"}})


def _run(pid: str, *, counters):
    from errorta_council.coding.autonomy import CADENCE_OFF, CodingAutonomyPolicy
    from errorta_council.coding.runner import CodingRunner
    CodingRunner(pid, _PM_MEMBERS, _done_pm, guardrail_enabled=True).run(
        CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF, max_iterations=4),
        counters=counters)


def test_drop_ledger_is_cleared_on_a_fresh_run(tmp_errorta_home):
    store = LedgerStore("p46-fresh")
    store.create_project(north_star="n", definition_of_done="d", target="new",
                         repo_path=None)
    key = task_dedupe.identity_key(title="wire the integration layer", paths=[])
    for _ in range(3):
        drop_ledger.record_drop(store, key)

    _run("p46-fresh", counters=None)

    assert drop_ledger.drop_count(store, key) == 0


def test_drop_ledger_survives_a_resume(tmp_errorta_home):
    """The KEEP half: a carried-counters resume is the SAME run, so the
    create->drop cycles it counted must not restart."""
    from errorta_council.coding.autonomy import LoopCounters

    store = LedgerStore("p46-resume")
    store.create_project(north_star="n", definition_of_done="d", target="new",
                         repo_path=None)
    key = task_dedupe.identity_key(title="wire the integration layer", paths=[])
    for _ in range(3):
        drop_ledger.record_drop(store, key)

    _run("p46-resume", counters=LoopCounters())

    assert drop_ledger.drop_count(store, key) == 3


def test_quarantine_problem_does_not_block_the_development_stage(tmp_path):
    """The escalation flags for the operator; it must NOT trip the governance
    blocking gate, which halts the whole run with `blocked_on_problem`."""
    from errorta_council.coding import attention
    store = _store(tmp_path, "p46-nonblocking")
    sig = attention.raise_task_pathology_problem(
        store.project_id, identity="k1", title="wire the integration layer",
        drops=3, reason_code="pm_pruned", store=store)
    assert sig is not None and sig.blocking is False
    assert attention.blocks_stage(store.project_id, "development",
                                  store=store) is False
