from __future__ import annotations

from pathlib import Path

from errorta_council.coding import drop_reasons
from errorta_council.coding.ledger import LedgerStore


def _store(tmp_path: Path, name: str = "p45") -> LedgerStore:
    s = LedgerStore(name, root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new",
                     repo_path=None)
    return s


def test_reason_blob_shape():
    blob = drop_reasons.reason_blob(
        drop_reasons.MISSING_CAPABILITY, detail="no executor", capability="execution_gate")
    assert blob == {"reason_code": "missing_capability",
                    "reason_detail": "no executor",
                    "capability": "execution_gate"}
    assert drop_reasons.MISSING_CAPABILITY in drop_reasons.ALL


from errorta_council.coding.runner import _materialize_pm_tasks


class _Planned:
    def __init__(self, title, detail=""):
        self.title = title
        self.detail = detail
        self.depends_on = []
        self.task_type = "implementation"
        self.difficulty_tier = "mid"
        self.preferred_member_id = ""
        self.preferred_route_id = ""
        self.assignment_rationale = ""


class _Intent:
    def __init__(self, tasks):
        self.tasks = tasks


def _refused_decision(store):
    for d in store.list_decisions():
        if d.get("choice") == "task_requires_absent_capability":
            return d
    return None


def test_capability_refusal_decision_carries_reason(tmp_path):
    store = _store(tmp_path)
    # An execution-imperative task with no gate available -> refused.
    _materialize_pm_tasks(store, _Intent([
        _Planned("run the integration suite and report the failing cases")]))
    dec = _refused_decision(store)
    assert dec is not None
    assert dec["reason_code"] == drop_reasons.MISSING_CAPABILITY
    assert dec["capability"] == "execution_gate"


def _tasks_by_state(store, state):
    return [t for t in store.list_tasks() if t.state == state]


def test_refused_task_is_persisted_blocked(tmp_path):
    store = _store(tmp_path)
    _materialize_pm_tasks(store, _Intent([
        _Planned("run the integration suite and report the failing cases")]))
    blocked = _tasks_by_state(store, "blocked")
    assert len(blocked) == 1
    assert blocked[0]._extras.get("blocked_reason") == "missing_capability:execution_gate"
    assert "execution" in blocked[0].reason_summary.lower() or blocked[0].reason_summary
    # It must NOT have been dropped or silently discarded.
    assert not _tasks_by_state(store, "dropped")


def test_same_batch_duplicate_execution_blocked_once(tmp_path):
    title = "run the integration suite and report the failing cases"
    store = _store(tmp_path)
    _materialize_pm_tasks(store, _Intent([_Planned(title), _Planned(title)]))
    blocked = _tasks_by_state(store, "blocked")
    assert len(blocked) == 1
    assert blocked[0]._extras.get("blocked_reason") == "missing_capability:execution_gate"
    assert any(d.get("choice") == "duplicate_task_rejected" for d in store.list_decisions())


from errorta_council.coding.runner import _reeval_capability_blocked


def test_auto_unblock_when_gate_appears(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _materialize_pm_tasks(store, _Intent([
        _Planned("run the integration suite and report the failing cases")]))
    assert len(_tasks_by_state(store, "blocked")) == 1

    # Gate still closed -> no unblock.
    monkeypatch.setattr(
        "errorta_council.coding.runner._gate_state.gate_available", lambda s: False)
    assert _reeval_capability_blocked(store) == []
    assert len(_tasks_by_state(store, "blocked")) == 1

    # Gate now available -> the task returns to todo, a decision is recorded.
    monkeypatch.setattr(
        "errorta_council.coding.runner._gate_state.gate_available", lambda s: True)
    unblocked = _reeval_capability_blocked(store)
    assert len(unblocked) == 1
    assert len(_tasks_by_state(store, "todo")) == 1
    assert len(_tasks_by_state(store, "blocked")) == 0
    assert any(d.get("choice") == "capability_unblocked" for d in store.list_decisions())
    # Idempotent: nothing left to unblock.
    assert _reeval_capability_blocked(store) == []
