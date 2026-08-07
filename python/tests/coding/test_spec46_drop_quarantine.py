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
