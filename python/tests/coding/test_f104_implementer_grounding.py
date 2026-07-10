"""F104 S1/S2 — the implementer/reviewer must receive the bound corpus's facts.

Reproduces the 2026-06-20 finding: dev/reviewer context packets carried NO
corpus evidence (only the PM did), so the implementer coded the spec values
blind. These tests fail on the pre-S2 code and pass after.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import errorta_project_grounding.retrieval as retrieval
from errorta_council.coding.ledger import LedgerStore
from errorta_project_grounding.context_packets import (
    build_role_context_packet,
    format_packet,
)
from errorta_project_grounding.corpus_binding import ProjectCorpusBinding, save_binding

# The load-bearing spec facts that must reach the implementer.
_SPEC = (
    "Customer tiers: bronze 0%, silver 7%, gold 15%, platinum 22%. "
    "Order-size bonus: 3% over $500, 6% over $2000. Gift cards never discounted."
)


@dataclass
class _Hit:
    content: str
    corpus_id: str = "pricing"
    chunk_id: str = "c1"
    score: float = 0.9
    metadata: dict = field(default_factory=dict)


@dataclass
class _Task:
    task_id: str = "t-1"
    title: str = "Implement the tier discounts and order-size bonus"
    detail: str = "apply the discount tiers from the spec"


def _project(tmp: Path, pid: str, *, bound: bool) -> LedgerStore:
    s = LedgerStore(pid, root=tmp)
    s.create_project(north_star="pricing module", definition_of_done="tests pass",
                     target="new", repo_path=None)
    if bound:
        save_binding(s, ProjectCorpusBinding(project_id=pid, mode="existing",
                                             corpus_id="pricing"))
    return s


def _patch_retrieval(monkeypatch, hits):
    monkeypatch.setattr(retrieval, "retrieve_with_status",
                        lambda store, *, query, top_k=6, filters=None: (hits, "ok"))


# --- the headline: the dev packet carries the corpus facts ------------------

def test_dev_packet_contains_corpus_facts(tmp_path, monkeypatch):
    s = _project(tmp_path, "p1", bound=True)
    _patch_retrieval(monkeypatch, [_Hit(_SPEC)])
    pkt = build_role_context_packet(store=s, role="dev", task=_Task())
    assert pkt is not None
    ev = pkt["corpus_evidence"]
    assert ev, "dev packet must carry corpus evidence when a corpus is bound"
    blob = " ".join(h["summary"] for h in ev)
    for fact in ("silver 7%", "gold 15%", "platinum 22%", "bronze"):
        assert fact in blob, f"missing load-bearing fact: {fact}"
    # and it must actually render into the injected prompt
    rendered = format_packet(pkt)
    assert "platinum 22%" in rendered and "corpus_evidence" in rendered


def test_reviewer_packet_also_grounded(tmp_path, monkeypatch):
    s = _project(tmp_path, "p2", bound=True)
    _patch_retrieval(monkeypatch, [_Hit(_SPEC)])
    pkt = build_role_context_packet(store=s, role="reviewer", task=_Task())
    assert pkt["corpus_evidence"], "reviewer must see the spec to check the diff"


def test_corpus_facts_not_truncated_midlist(tmp_path, monkeypatch):
    # a >240-char rule block must survive (the old _SUMMARY_CAP would cut it).
    long_spec = _SPEC + " " + ("Rounding is half-up to the nearest cent. " * 8)
    s = _project(tmp_path, "p3", bound=True)
    _patch_retrieval(monkeypatch, [_Hit(long_spec)])
    pkt = build_role_context_packet(store=s, role="dev", task=_Task())
    assert "platinum 22%" in pkt["corpus_evidence"][0]["summary"]
    assert len(pkt["corpus_evidence"][0]["summary"]) > 240


# --- decouple from the local memory index (remote-adapter path) -------------

def test_bound_corpus_grounds_even_with_no_memory_index(tmp_path, monkeypatch):
    # No ProjectMemoryStore sqlite exists (remote adapter) -> must NOT return
    # None; build a corpus-only packet.
    s = _project(tmp_path, "p4", bound=True)
    _patch_retrieval(monkeypatch, [_Hit(_SPEC)])
    pkt = build_role_context_packet(store=s, role="dev", task=_Task())
    assert pkt is not None
    assert pkt["items"] == []
    assert pkt["corpus_evidence"]


# --- invariant: no corpus bound => byte-identical (no grounding) ------------

def test_no_corpus_no_memory_returns_none(tmp_path, monkeypatch):
    s = _project(tmp_path, "p5", bound=False)
    # even if retrieval would return hits, an unbound project gets no packet
    _patch_retrieval(monkeypatch, [_Hit(_SPEC)])
    pkt = build_role_context_packet(store=s, role="dev", task=_Task())
    assert pkt is None
    assert format_packet(pkt) == ""


# --- S7: the grounding trace logs the corpus_evidence_count -----------------

def test_grounding_log_emits_corpus_evidence_count(tmp_path, monkeypatch, caplog):
    import logging
    from errorta_council.coding.runner import _grounding_packet_text
    s = _project(tmp_path, "p6", bound=True)
    _patch_retrieval(monkeypatch, [_Hit(_SPEC)])
    caplog.set_level(logging.INFO, logger="errorta.grounding")
    _grounding_packet_text("dev", s, task=_Task())
    blob = " ".join(r.getMessage() for r in caplog.records if r.name == "errorta.grounding")
    assert "corpus_evidence_count=1" in blob
