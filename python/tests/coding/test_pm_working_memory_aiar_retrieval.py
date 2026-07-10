from __future__ import annotations

from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_project_grounding.adapter import GroundingHit
from errorta_project_grounding.context_packets import (
    build_pm_boot_briefing,
    build_role_context_packet,
    ensure_pm_working_memory,
)
from errorta_project_grounding.corpus_binding import ProjectCorpusBinding, save_binding
from errorta_project_grounding.pm_working_memory import (
    SCHEMA_VERSION,
    mirror_source,
    pm_working_memory_status,
    retrieve_pm_working_memory_from_aiar,
)


def _store(tmp_path: Path, project_id: str = "pmwm-retrieve") -> LedgerStore:
    store = LedgerStore(project_id, root=tmp_path)
    store.create_project(
        north_star="Build app",
        definition_of_done="Tests pass",
        target="new",
        repo_path=None,
    )
    save_binding(
        store,
        ProjectCorpusBinding(
            project_id=project_id,
            mode="existing",
            corpus_id="project-pmwm",
            adapter_source="remote",
            health_state="ready",
            health_reason="ready",
        ),
    )
    ensure_pm_working_memory(store)
    return store


def test_retrieve_pm_working_memory_uses_aiar_hit_when_available(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)
    hit = GroundingHit(
        content="PM working memory content",
        corpus_id="project-pmwm",
        chunk_id="c1",
        score=0.91,
        metadata={
            "source": mirror_source(store.project_id),
            "schema_version": SCHEMA_VERSION,
        },
    )
    from errorta_project_grounding import retrieval
    monkeypatch.setattr(retrieval, "retrieve_with_status", lambda *a, **k: ([hit], "ok"))

    evidence = retrieve_pm_working_memory_from_aiar(store)

    assert evidence.status == "available"
    assert evidence.hits[0]["ref"] == "hit:project-pmwm:c1"
    assert pm_working_memory_status(store)["aiar_retrieval_status"] == "available"


def test_pm_packet_and_boot_include_aiar_pm_memory_hit(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path, "pmwm-packet-hit")
    hit = GroundingHit(
        content="PM working memory content",
        corpus_id="project-pmwm",
        chunk_id="c2",
        metadata={"category": "pm_working_memory", "schema_version": SCHEMA_VERSION},
    )
    from errorta_project_grounding import retrieval
    monkeypatch.setattr(retrieval, "retrieve_with_status", lambda *a, **k: ([hit], "ok"))

    packet = build_role_context_packet(store=store, role="pm")
    briefing = build_pm_boot_briefing(store=store)

    assert any(i["ref"] == "hit:project-pmwm:c2" for i in packet["corpus_evidence"])
    assert any(i["ref"] == "hit:project-pmwm:c2" for i in briefing["corpus_evidence"])


def test_retrieve_warns_on_corpus_miss(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    unrelated = GroundingHit(
        content="unrelated corpus hit",
        corpus_id="project-pmwm",
        chunk_id="c9",
        metadata={"source": "README.md"},
    )
    from errorta_project_grounding import retrieval
    monkeypatch.setattr(retrieval, "retrieve_with_status", lambda *a, **k: ([unrelated], "ok"))

    evidence = retrieve_pm_working_memory_from_aiar(store)
    packet = build_role_context_packet(store=store, role="pm")

    assert evidence.status == "miss"
    assert "pm_working_memory_corpus_miss" in evidence.warnings
    assert "pm_working_memory_corpus_miss" in packet["warnings"]
    status = pm_working_memory_status(store)
    assert status["aiar_retrieval_status"] == "miss"


def test_retrieve_degrades_without_corpus(tmp_path: Path) -> None:
    store = LedgerStore("pmwm-no-corpus", root=tmp_path)
    store.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    ensure_pm_working_memory(store)

    evidence = retrieve_pm_working_memory_from_aiar(store)

    assert evidence.status == "no_corpus"
    assert evidence.hits == ()
