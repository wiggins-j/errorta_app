from __future__ import annotations

from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_project_grounding.adapter import GroundingRecordRef
from errorta_project_grounding.corpus_binding import ProjectCorpusBinding, save_binding
from errorta_project_grounding.pm_working_memory import (
    SCHEMA_VERSION,
    mirror_pm_working_memory_to_aiar,
    mirror_source,
    pm_working_memory_status,
)
from errorta_project_grounding.update_pipeline import sync_from_ledger


class _FakeAdapter:
    def __init__(self) -> None:
        self.records: list[dict] = []
        self.published: list[str] = []

    def ingest_record(self, *, corpus_id, content, metadata):
        self.records.append({
            "corpus_id": corpus_id,
            "content": content,
            "metadata": metadata,
        })
        return GroundingRecordRef(corpus_id=corpus_id, record_id="rec-1", metadata=metadata)

    def publish(self, corpus_id):
        self.published.append(corpus_id)
        return {"published": True}


class _UnsupportedAdapter:
    pass


def _store(tmp_path: Path, project_id: str = "pmwm-mirror") -> LedgerStore:
    store = LedgerStore(project_id, root=tmp_path)
    store.create_project(
        north_star="Build app",
        definition_of_done="Tests pass",
        target="new",
        repo_path=None,
    )
    sync_from_ledger(store)
    return store


def _bind(store: LedgerStore, corpus_id: str = "project-pmwm") -> None:
    save_binding(
        store,
        ProjectCorpusBinding(
            project_id=store.project_id,
            mode="existing",
            corpus_id=corpus_id,
            adapter_source="remote",
            health_state="ready",
            health_reason="ready",
        ),
    )
    sync_from_ledger(store)


def test_mirrors_pm_working_memory_to_bound_aiar_corpus(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _bind(store)
    adapter = _FakeAdapter()

    result = mirror_pm_working_memory_to_aiar(store, adapter=adapter)

    assert result.status == "mirrored"
    assert result.record_id == "rec-1"
    assert adapter.published == ["project-pmwm"]
    assert len(adapter.records) == 1
    record = adapter.records[0]
    assert record["metadata"]["source"] == mirror_source(store.project_id)
    assert record["metadata"]["schema_version"] == SCHEMA_VERSION
    assert "PM working memory" in record["content"]
    assert "/Users/" not in record["metadata"]["source"]


def test_mirror_degrades_without_bound_corpus(tmp_path: Path) -> None:
    store = _store(tmp_path)
    adapter = _FakeAdapter()

    result = mirror_pm_working_memory_to_aiar(store, adapter=adapter)

    assert result.status == "no_corpus"
    assert adapter.records == []


def test_mirror_degrades_when_record_ingest_is_unsupported(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _bind(store)

    result = mirror_pm_working_memory_to_aiar(store, adapter=_UnsupportedAdapter())

    assert result.status == "unsupported"
    status = pm_working_memory_status(store)
    assert status["aiar_mirror_status"] == "unsupported"
