"""F088-08 — PM boot briefing (first PM turn, grounded + source-cited)."""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_project_grounding import retrieval
from errorta_project_grounding.adapter import GroundingHit
from errorta_project_grounding.context_packets import build_pm_boot_briefing
from errorta_project_grounding.corpus_binding import ProjectCorpusBinding, save_binding
from errorta_project_grounding.memory_store import (
    MemoryItem,
    MemorySourceRef,
    ProjectMemoryStore,
)


def _store(tmp: Path, pid: str) -> LedgerStore:
    s = LedgerStore(pid, root=tmp)
    s.create_project(north_star="build calc", definition_of_done="add/sub work",
                     target="new", repo_path=None)
    return s


def _durable(mem, mid, content, **md):
    mem.put(MemoryItem(project_id=mem.project_id, authority="durable_truth",
                       source_type="pm_decision", source_ref=MemorySourceRef(task_id="t"),
                       content=content, memory_id=mid, created_at="2026-01-01T00:00:00Z",
                       metadata=md))


# --- first-PM-turn gating ---------------------------------------------------


def test_first_pm_turn_gets_briefing_second_does_not(tmp_path) -> None:
    from errorta_council.coding.runner import _pm_boot_text
    s = _store(tmp_path, "b1")
    mem = ProjectMemoryStore("b1", root=tmp_path)
    _durable(mem, "d1", "public API: add, subtract")

    # first turn: no tasks -> boot briefing
    first = _pm_boot_text(s)
    assert "pm_boot_briefing.v1" in first and "mem:d1" in first

    # a task now exists -> no briefing on later turns
    s.add_task(title="impl add", role="dev")
    assert _pm_boot_text(s) == ""


def test_no_grounding_no_briefing(tmp_path) -> None:
    from errorta_council.coding.runner import _pm_boot_text
    s = _store(tmp_path, "b2")  # no memory db
    assert _pm_boot_text(s) == ""
    assert build_pm_boot_briefing(store=s) is None


# --- provenance + content ---------------------------------------------------


def test_every_durable_item_has_source_ids(tmp_path) -> None:
    s = _store(tmp_path, "b3")
    mem = ProjectMemoryStore("b3", root=tmp_path)
    _durable(mem, "d1", "fact one")
    _durable(mem, "d2", "fact two")
    b = build_pm_boot_briefing(store=s)
    assert b["durable_truth"]
    assert all(item["source_ids"] for item in b["durable_truth"])


def test_corpus_evidence_when_bound(tmp_path, monkeypatch) -> None:
    s = _store(tmp_path, "b4")
    mem = ProjectMemoryStore("b4", root=tmp_path)
    _durable(mem, "d1", "x")
    save_binding(s, ProjectCorpusBinding(project_id="b4", mode="existing",
                 corpus_id="b4-corpus", adapter_source="remote", health_state="ready"))
    monkeypatch.setattr(retrieval, "retrieve_with_status",
                        lambda *a, **k: ([GroundingHit(content="README: divide raises ValueError",
                                                       corpus_id="b4-corpus", chunk_id="c9", score=0.8)], "ok"))
    b = build_pm_boot_briefing(store=s)
    assert b["freshness"]["corpus_retrieval"] == "available"
    assert b["corpus_evidence"][0]["ref"] == "hit:b4-corpus:c9"
    assert b["corpus_evidence"][0]["source_ids"] == ["corpus:b4-corpus:c9"]


def test_no_corpus_evidence_when_unbound(tmp_path) -> None:
    s = _store(tmp_path, "b5")
    mem = ProjectMemoryStore("b5", root=tmp_path)
    _durable(mem, "d1", "x")
    b = build_pm_boot_briefing(store=s)
    assert b["corpus_evidence"] == [] and b["freshness"]["corpus_retrieval"] == "no_corpus"


def test_corpus_evidence_even_when_memory_absent(tmp_path, monkeypatch) -> None:
    # memory.sqlite3 absent but a corpus IS bound -> still retrieve corpus +
    # warn memory_unavailable (P1 fix: don't bail before corpus retrieval).
    s = _store(tmp_path, "b7")  # no memory db created
    save_binding(s, ProjectCorpusBinding(project_id="b7", mode="existing",
                 corpus_id="b7-corpus", adapter_source="remote", health_state="ready"))
    monkeypatch.setattr(retrieval, "retrieve_with_status",
                        lambda *a, **k: ([GroundingHit(content="api docs", corpus_id="b7-corpus",
                                                       chunk_id="c1")], "ok"))
    b = build_pm_boot_briefing(store=s)
    assert b is not None
    assert "memory_unavailable" in b["warnings"]
    assert b["durable_truth"] == [] and b["corpus_evidence"]


def test_corpus_failure_is_unavailable_not_empty(tmp_path, monkeypatch) -> None:
    s = _store(tmp_path, "b8")
    mem = ProjectMemoryStore("b8", root=tmp_path)
    _durable(mem, "d1", "x")
    save_binding(s, ProjectCorpusBinding(project_id="b8", mode="existing",
                 corpus_id="b8-corpus", adapter_source="remote", health_state="ready"))
    monkeypatch.setattr(retrieval, "retrieve_with_status", lambda *a, **k: ([], "unavailable"))
    b = build_pm_boot_briefing(store=s)
    assert b["freshness"]["corpus_retrieval"] == "unavailable"  # not "empty"
    assert "corpus_unavailable" in b["warnings"]


def test_briefing_labeled_not_auto_truth(tmp_path) -> None:
    from errorta_project_grounding.context_packets import format_pm_boot_briefing
    s = _store(tmp_path, "b6")
    mem = ProjectMemoryStore("b6", root=tmp_path)
    _durable(mem, "d1", "x")
    text = format_pm_boot_briefing(build_pm_boot_briefing(store=s))
    assert "not" in text.lower() and "truth" in text.lower()  # evidence informs, not approves
