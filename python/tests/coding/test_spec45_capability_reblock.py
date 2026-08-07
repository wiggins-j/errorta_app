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
