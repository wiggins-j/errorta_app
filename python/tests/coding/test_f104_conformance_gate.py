"""F104 S5 — implementer-grounding signal + spec-conformance merge policy."""
from __future__ import annotations

from pathlib import Path

import pytest

from errorta_council.coding.evidence import gather_merge_evidence, merge_review
from errorta_council.coding.ledger import LedgerError, LedgerStore
from errorta_project_grounding.corpus_binding import ProjectCorpusBinding, save_binding


class _WS:
    def head(self):
        return "head-abc"

    def preview(self):
        return {"diff": "", "conflicts": []}


def _store(tmp: Path, pid: str, *, bound: bool = False) -> LedgerStore:
    s = LedgerStore(pid, root=tmp)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    if bound:
        save_binding(s, ProjectCorpusBinding(project_id=pid, mode="existing", corpus_id="c"))
    return s


def _codes(review):
    return {b["code"] for b in review["gate"]["blockers"]}


# --- the signal store -------------------------------------------------------

def test_grounding_signal_records_max(tmp_path):
    s = _store(tmp_path, "g1")
    assert s.implementer_grounding("t-1") == 0
    assert s.any_implementer_grounded() is False
    s.record_implementer_grounding(task_id="t-1", corpus_evidence_count=2)
    s.record_implementer_grounding(task_id="t-1", corpus_evidence_count=0)  # retry, ungrounded
    assert s.implementer_grounding("t-1") == 2  # max kept; not erased
    assert s.any_implementer_grounded() is True


def test_grounding_policy_validation(tmp_path):
    s = _store(tmp_path, "g2")
    assert s.get_grounding_policy() == "warn"  # default
    assert s.set_grounding_policy("required_when_corpus_bound") == "required_when_corpus_bound"
    assert s.get_grounding_policy() == "required_when_corpus_bound"
    with pytest.raises(LedgerError):
        s.set_grounding_policy("bogus")


# --- gather + surface -------------------------------------------------------

def test_evidence_surfaces_grounding(tmp_path):
    s = _store(tmp_path, "g3", bound=True)
    s.record_implementer_grounding(task_id="t-1", corpus_evidence_count=3)
    ev = gather_merge_evidence(s, _WS())
    assert ev["corpus_bound"] is True and ev["implementer_grounded"] is True


def test_merge_review_surfaces_grounding_block(tmp_path):
    s = _store(tmp_path, "g4", bound=True)
    review = merge_review(s, _WS())
    assert review["grounding"] == {
        "corpus_bound": True, "implementer_grounded": False, "policy": "warn"}


# --- policy behavior --------------------------------------------------------

def test_warn_default_does_not_block_on_ungrounded(tmp_path):
    s = _store(tmp_path, "g5", bound=True)  # ungrounded + corpus-bound
    review = merge_review(s, _WS())
    assert "implementer_not_grounded" not in _codes(review)  # warn = surface only


def test_required_when_corpus_bound_blocks_ungrounded(tmp_path):
    s = _store(tmp_path, "g6", bound=True)
    s.set_grounding_policy("required_when_corpus_bound")
    review = merge_review(s, _WS())
    assert "implementer_not_grounded" in _codes(review)
    assert review["gate"]["allowed"] is False


def test_required_when_corpus_bound_passes_when_grounded(tmp_path):
    s = _store(tmp_path, "g7", bound=True)
    s.set_grounding_policy("required_when_corpus_bound")
    s.record_implementer_grounding(task_id="t-1", corpus_evidence_count=4)
    review = merge_review(s, _WS())
    assert "implementer_not_grounded" not in _codes(review)


def test_required_when_corpus_bound_ignores_unbound_project(tmp_path):
    s = _store(tmp_path, "g8", bound=False)  # no corpus -> policy n/a
    s.set_grounding_policy("required_when_corpus_bound")
    review = merge_review(s, _WS())
    assert "implementer_not_grounded" not in _codes(review)


def test_required_policy_blocks_even_unbound(tmp_path):
    s = _store(tmp_path, "g9", bound=False)
    s.set_grounding_policy("required")
    review = merge_review(s, _WS())
    assert "implementer_not_grounded" in _codes(review)


def test_worktree_route_surfaces_grounding(tmp_errorta_home):
    # F104 S5 route gap: GET /worktree must pass the grounding signal through.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from errorta_app.routes import coding as coding_routes
    from errorta_council.coding.workspace import CodingWorkspace

    s = LedgerStore("wtg")  # default root (tmp_errorta_home redirects HOME)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    save_binding(s, ProjectCorpusBinding(project_id="wtg", mode="existing", corpus_id="c"))
    s.record_implementer_grounding(task_id="t-1", corpus_evidence_count=2)
    CodingWorkspace("wtg", s).setup(target="new", repo_path=None)

    app = FastAPI()
    app.include_router(coding_routes.router)
    c = TestClient(app, headers={"x-errorta-origin": "tauri-ui"})
    r = c.get("/coding/projects/wtg/worktree")
    assert r.status_code == 200, r.text
    assert r.json()["grounding"] == {
        "corpus_bound": True, "implementer_grounded": True, "policy": "warn"}


def test_runner_records_signal_for_grounded_dev_turn(tmp_path, monkeypatch):
    # the runner's grounding-packet build records the signal end-to-end.
    import errorta_project_grounding.retrieval as retrieval
    from errorta_council.coding.runner import _grounding_packet_text

    class _Task:
        task_id = "t-grounded"
        title = "implement the tiers"
        detail = "use the spec"

    s = _store(tmp_path, "g10", bound=True)
    monkeypatch.setattr(retrieval, "retrieve_with_status",
                        lambda store, *, query, top_k=6, filters=None:
                        ([type("H", (), {"content": "silver 7%", "corpus_id": "c",
                                         "chunk_id": "1", "score": 0.9, "metadata": {}})()],
                         "ok"))
    _grounding_packet_text("dev", s, task=_Task())
    assert s.implementer_grounding("t-grounded") == 1
    assert s.any_implementer_grounded() is True
