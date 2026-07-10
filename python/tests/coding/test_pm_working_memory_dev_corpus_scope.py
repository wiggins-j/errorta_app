"""F099 scoping regression: a dev context request must NOT receive PM working
memory, even though it is mirrored into the SAME bound AIAR corpus the dev's
corpus retrieval reads. (Spec non-goal: no developer memory pollution.)"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from errorta_council.coding.ledger import LedgerStore, Task
from errorta_council.coding import runner as coding_runner
from errorta_project_grounding import retrieval
from errorta_project_grounding.adapter import GroundingHit
from errorta_project_grounding.corpus_binding import ProjectCorpusBinding, save_binding
from errorta_project_grounding.context_packets import ensure_pm_working_memory
from errorta_project_grounding.pm_working_memory import SCHEMA_VERSION, mirror_source


@dataclass
class _Scope:
    sources: tuple[str, ...] = ("corpus",)
    corpus_query: str = "current focus and next tasks"


@dataclass
class _Intent:
    question: str = "what is the team working on?"
    reason: str = "other"
    max_items: int = 6
    scope: _Scope = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.scope is None:
            self.scope = _Scope()


def _store(tmp_path: Path) -> LedgerStore:
    store = LedgerStore("pmwm-dev-scope", root=tmp_path)
    store.create_project(
        north_star="SECRET PM FOCUS that devs must not see",
        definition_of_done="done",
        target="new",
        repo_path=None,
    )
    save_binding(
        store,
        ProjectCorpusBinding(
            project_id=store.project_id,
            mode="existing",
            corpus_id="project-pmwm",
            adapter_source="remote",
            health_state="ready",
            health_reason="ready",
        ),
    )
    ensure_pm_working_memory(store)
    return store


def test_dev_context_request_excludes_pm_working_memory_corpus_hit(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)
    pm_hit = GroundingHit(
        content="PM working memory: SECRET PM FOCUS, blockers, decisions",
        corpus_id="project-pmwm",
        chunk_id="pm1",
        score=0.99,
        metadata={
            "source": mirror_source(store.project_id),
            "category": "pm_working_memory",
            "schema_version": SCHEMA_VERSION,
        },
    )
    ordinary_hit = GroundingHit(
        content="ordinary code chunk in the corpus",
        corpus_id="project-pmwm",
        chunk_id="code1",
        score=0.5,
        metadata={"source": "src/app.py"},
    )
    # PM hit ranks first (highest score) — without the filter it would be returned.
    monkeypatch.setattr(
        retrieval, "retrieve_with_status",
        lambda *a, **k: ([pm_hit, ordinary_hit], "ok"),
    )

    task = store.add_task(title="implement thing", role="dev")
    answer = coding_runner._answer_dev_context_request(store, task, _Intent())

    refs = [e["ref"] for e in answer["corpus_evidence"]]
    summaries = " ".join(e["summary"] for e in answer["corpus_evidence"])
    assert "hit:project-pmwm:pm1" not in refs, "PM working memory leaked to dev"
    assert "SECRET PM FOCUS" not in summaries
    assert "hit:project-pmwm:code1" in refs, "ordinary corpus hit must still pass"
