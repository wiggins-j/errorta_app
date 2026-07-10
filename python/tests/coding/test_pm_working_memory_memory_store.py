from __future__ import annotations

from pathlib import Path

import pytest

from errorta_council.coding.ledger import LedgerStore
from errorta_project_grounding.memory_store import (
    InvalidMemoryItem,
    MemoryItem,
    MemoryQuery,
    MemorySourceRef,
    MemoryVisibility,
    ProjectMemoryStore,
)
from errorta_project_grounding.pm_working_memory import SCHEMA_VERSION, SOURCE_TYPE
from errorta_project_grounding.update_pipeline import rebuild_from_ledger, sync_from_ledger


def _store(tmp_path: Path, project_id: str = "pmwm-memory") -> LedgerStore:
    store = LedgerStore(project_id, root=tmp_path)
    store.create_project(
        north_star="Build app",
        definition_of_done="Tests pass",
        target="new",
        repo_path=None,
    )
    return store


def _pm_rows(tmp_path: Path, project_id: str = "pmwm-memory"):
    return ProjectMemoryStore(project_id, root=tmp_path).query(
        MemoryQuery(
            authorities=("durable_truth",),
            source_type=SOURCE_TYPE,
            include_history=True,
            limit=20,
        )
    )


def test_sync_writes_one_pm_only_durable_row(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task = store.add_task(title="Implement app", role="dev")
    store.update_task(task.task_id, state="doing")

    counts = sync_from_ledger(store)

    rows = _pm_rows(tmp_path)
    assert counts["pm_working_memory"] == 1
    assert len([row for row in rows if row.valid_until is None]) == 1
    item = rows[0]
    assert item.authority == "durable_truth"
    assert item.source_type == SOURCE_TYPE
    assert item.source_ref.task_id == "pm"
    assert item.metadata["schema_version"] == SCHEMA_VERSION
    assert item.visibility.visible_to("pm") is True
    assert item.visibility.visible_to("dev") is False
    assert "Implement app" in item.content


def test_sync_is_idempotent_and_rebuild_repairs_store(tmp_path: Path) -> None:
    store = _store(tmp_path, "pmwm-rebuild")
    sync_from_ledger(store)
    sync_from_ledger(store)
    sync_from_ledger(store)
    assert len(_pm_rows(tmp_path, "pmwm-rebuild")) == 1

    db = store.dir / "grounding" / "memory.sqlite3"
    assert db.exists()
    db.unlink()

    rebuild_from_ledger(store)
    assert len(_pm_rows(tmp_path, "pmwm-rebuild")) == 1


def test_pm_working_memory_requires_schema_provenance_and_pm_visibility(tmp_path: Path) -> None:
    memory = ProjectMemoryStore("pmwm-invalid", root=tmp_path)
    base = dict(
        project_id="pmwm-invalid",
        authority="durable_truth",
        source_type=SOURCE_TYPE,
        source_ref=MemorySourceRef(task_id="pm"),
        content='{"schema_version":"pm_working_memory.v1"}',
        metadata={"schema_version": SCHEMA_VERSION},
        visibility=MemoryVisibility(
            default_pm=True,
            default_dev=False,
            default_reviewer=False,
            default_tester=False,
        ),
    )
    saved = memory.put(MemoryItem(**base))
    assert saved.source_type == SOURCE_TYPE

    with pytest.raises(InvalidMemoryItem, match="pm_working_memory"):
        memory.put(MemoryItem(**{**base, "metadata": {}}))

    with pytest.raises(InvalidMemoryItem, match="pm_working_memory"):
        memory.put(MemoryItem(**{
            **base,
            "visibility": MemoryVisibility(default_pm=True, default_dev=True),
        }))
