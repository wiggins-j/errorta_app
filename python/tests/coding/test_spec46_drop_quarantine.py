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
