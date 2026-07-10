"""FakeAiar — in-memory implementation of the five F096 AIAR contracts.

Lets Errorta integration (B1 retrieve wiring, B2 grounding migration, B3 corpus
readiness, B5 telemetry) develop and test in parallel BEFORE AIAR ships the real
APIs. The shapes match ``docs/handoff/F096-aiar-contract.md`` exactly; the flip
from this fake to the real AIAR is a provider swap, and contract-targeted tests
stay green.

Deterministic by construction: retrieval scores derive from token overlap, so a
seeded corpus yields stable, ordered hits with no model and no network.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

RETRIEVE_SCHEMA = "aiar.retrieve.v1"
INGEST_SCHEMA = "aiar.ingest.v1"
GROUNDING_SCHEMA = "aiar.grounding.v1"
ANSWER_SCHEMA = "aiar.answer.v1"
CAPABILITIES_SCHEMA = "aiar.capabilities.v1"

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


def _overlap_score(query: str, text: str) -> float:
    q, t = _tokens(query), _tokens(text)
    if not q or not t:
        return 0.0
    return round(len(q & t) / len(q | t), 4)


@dataclass
class _Chunk:
    chunk_id: str
    source: str
    title: str
    text: str
    chunk_index: int
    category: str = "general"
    page_span: tuple[int, int] = (1, 1)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Instance:
    name: str
    chunks: list[_Chunk] = field(default_factory=list)
    published: bool = False
    last_ingest_error: str | None = None
    last_ingest_at: str | None = None


class FakeAiar:
    """A single-process stand-in for an AIAR backend.

    Construct, ``seed_corpus(...)`` one or more instances, then call the contract
    methods. ``fail_ingest=True`` makes the next ingest report a failed job (for
    Errorta's corpus-readiness fail-closed tests).
    """

    def __init__(self, *, aiar_version: str = "0.2.4-fake",
                 features: dict[str, bool] | None = None) -> None:
        self._instances: dict[str, _Instance] = {}
        self._grounding: list[dict[str, Any]] = []
        self._calls: dict[str, dict[str, Any]] = {}
        self._call_seq = 0
        self._aiar_version = aiar_version
        # Defaults mirror the AIAR 0.2.* train: semantic grounding is deferred to
        # A5, so the manifest reports it OFF. Tests that exercise the deferred
        # semantic path opt in via features={"semantic_grounding": True}.
        self._features = {
            "pure_retrieve": True,
            "remote_ingest": True,
            "grounding_v1": True,
            # Live example-host (v0.2.4) reports these on too: answer_prompt
            # include_sources + GET /calls are deployed.
            "answer_sources": True,
            "call_trace": True,
            "semantic_grounding": False,
            "judge_only": False,
            "streaming": False,
        }
        if features:
            self._features.update(features)

    # ---- seeding -------------------------------------------------------------

    def seed_corpus(self, instance: str, docs: list[tuple[str, str]],
                    *, published: bool = True) -> None:
        """Seed ``instance`` with ``[(source, text), ...]``. One chunk per doc."""
        inst = self._instances.setdefault(instance, _Instance(name=instance))
        for i, (source, text) in enumerate(docs):
            inst.chunks.append(_Chunk(
                chunk_id=f"{instance}-c{len(inst.chunks)}",
                source=source, title=source, text=text,
                chunk_index=i, metadata={"source": source}))
        inst.published = published
        inst.last_ingest_at = "2026-06-19T00:00:00Z"

    # ---- 1. retrieve (aiar.retrieve.v1) -------------------------------------

    def retrieve_chunks(self, query: str, *, instance: str, k: int = 8,
                        category: str | None = None) -> dict[str, Any]:
        if not (query or "").strip():
            raise ValueError("empty query")
        inst = self._instances.get(instance)
        if inst is None:
            raise KeyError(f"unknown instance: {instance}")
        scored = []
        for c in inst.chunks:
            if category is not None and c.category != category:
                continue
            scored.append((_overlap_score(query, c.text), c))
        scored.sort(key=lambda s: s[0], reverse=True)
        # Raw vector similarity (model-free, deterministic). NOT the answerer's
        # hybrid+rerank order — see the contract's ranking caveat.
        hits = [{
            "chunk_id": c.chunk_id, "source": c.source, "title": c.title,
            "text": c.text, "score": score, "chunk_index": c.chunk_index,
            "category": c.category, "page_span": list(c.page_span),
            "metadata": dict(c.metadata),
        } for score, c in scored[:k] if score > 0]
        # score_kind/score_order are response-level (constant across hits).
        return {"instance": instance, "query": query, "k": k,
                "score_kind": "cosine_similarity", "score_order": "higher_is_better",
                "hits": hits, "count": len(hits), "schema_version": RETRIEVE_SCHEMA}

    # ---- 2. grounding (aiar.grounding.v1) -----------------------------------

    def record_grounding(self, *, signature: str, verdict: dict,
                         correction: str = "", instance: str | None = None,
                         answer: str | None = None, prompt: str | None = None,
                         source_chunks: list[dict] | None = None) -> dict[str, Any]:
        rec = {
            "record_id": hashlib.sha256(
                f"{signature}:{instance}".encode()).hexdigest()[:16],
            "signature": signature, "instance": instance, "verdict": dict(verdict),
            "correction": correction, "prompt": prompt, "answer": answer,
            "source_chunk_ids": [c.get("chunk_id") for c in (source_chunks or [])],
            "created_at": "2026-06-19T00:00:00Z", "schema_version": GROUNDING_SCHEMA,
        }
        # last write wins per (signature, instance)
        self._grounding = [g for g in self._grounding
                           if not (g["signature"] == signature
                                   and g["instance"] == instance)]
        self._grounding.append(rec)
        return rec

    def lookup_grounding(self, *, signature: str,
                         instance: str | None = None) -> dict[str, Any] | None:
        for g in reversed(self._grounding):
            if g["signature"] == signature and g["instance"] == instance:
                return g
        return None

    def lookup_similar_groundings(self, *, prompt: str, instance: str | None = None,
                                  threshold: float = 0.72, limit: int = 5
                                  ) -> list[dict[str, Any]]:
        out = []
        for g in self._grounding:
            if instance is not None and g["instance"] != instance:
                continue
            sim = _overlap_score(prompt, g.get("prompt") or g.get("correction") or "")
            if sim >= threshold:
                out.append({**g, "similarity": sim})
        out.sort(key=lambda g: g["similarity"], reverse=True)
        return out[:limit]

    # ---- 3. ingest (aiar.ingest.v1) -----------------------------------------

    def ingest_documents(self, documents: list, *, instance: str,
                         publish: bool = False, _fail: bool = False,
                         _errors: list[str] | None = None) -> dict[str, Any]:
        """A3 IngestResult — the synchronous Python twin (the polled HTTP job is
        the same superset shape). Models the contract's explicit partial-success
        semantics so B3 can test fail-closed: 0 added + errors -> ``failed`` (never
        ready); an all-duplicate re-ingest -> ``done``; added + some errors ->
        ``done`` with non-empty ``errors``. ``publish`` defaults to ``False``.
        """
        inst = self._instances.setdefault(instance, _Instance(name=instance))
        if _fail:
            inst.last_ingest_error = "fake ingest failure"
            return {"instance": instance, "status": "failed", "accepted": 0,
                    "chunks_added": 0, "duplicates": 0,
                    "errors": ["fake ingest failure"], "published": inst.published,
                    "schema_version": INGEST_SCHEMA}
        errors = list(_errors or [])
        # Each entry in ``_errors`` marks one TRAILING document that failed to
        # ingest (0 chunks). The rest add normally (or dedupe). This lets a test
        # produce all three contract cases: added+errors (done), all-duplicate
        # (done), and 0-added+errors (failed).
        fail_n = min(len(errors), len(documents))  # clamp: can't fail more than sent
        cutoff = len(documents) - fail_n
        seen = {c.source for c in inst.chunks}
        added = duplicates = 0
        for i, doc in enumerate(documents):
            if i >= cutoff:
                continue  # this doc failed — counted in ``errors``
            src = (doc.get("source") if isinstance(doc, dict) else str(doc)) or f"doc{i}"
            txt = doc.get("text", "") if isinstance(doc, dict) else ""
            if src in seen:
                duplicates += 1
                continue
            seen.add(src)
            inst.chunks.append(_Chunk(
                chunk_id=f"{instance}-c{len(inst.chunks)}", source=src,
                title=src, text=txt, chunk_index=i, metadata={"source": src}))
            added += 1
        # failed iff nothing landed AND nothing was idempotently skipped.
        if added == 0 and duplicates == 0 and errors:
            status = "failed"
        else:
            status = "done"
            if publish and added:
                inst.published = True
        inst.last_ingest_at = "2026-06-19T00:00:00Z"
        # last_ingest_error reflects the MOST RECENT error regardless of overall
        # status — a "done"-with-errors ingest still records it. B3 must gate
        # readiness on status/published, NOT on last_ingest_error being non-None.
        inst.last_ingest_error = errors[0] if errors else None
        # ``accepted`` = documents submitted for ingest (mirrors the contract's
        # accepted:3 -> chunks_added:42 example where accepted counts docs, not
        # chunks); it is NOT reduced by per-doc failures.
        return {"instance": instance, "status": status, "accepted": len(documents),
                "chunks_added": added, "duplicates": duplicates, "errors": errors,
                "published": inst.published, "schema_version": INGEST_SCHEMA}

    def health(self, instance: str) -> dict[str, Any]:
        inst = self._instances.get(instance)
        if inst is None:
            return {"instance": instance, "published": False, "chunk_count": 0,
                    "last_ingest_error": None, "last_ingest_at": None}
        return {"instance": instance, "published": inst.published,
                "chunk_count": len(inst.chunks),
                "last_ingest_error": inst.last_ingest_error,
                "last_ingest_at": inst.last_ingest_at}

    # ---- 4. telemetry --------------------------------------------------------

    def answer_prompt(self, prompt: str, *, instance: str | None = None,
                      model: str = "fake-model", judge: bool = True,
                      include_sources: bool = False, **_: Any) -> dict[str, Any]:
        self._call_seq += 1
        call_id = f"call-{self._call_seq:04d}"
        retr = self.retrieve_chunks(prompt, instance=instance) if instance else \
            {"hits": [], "count": 0}
        meta = {
            "schema_version": ANSWER_SCHEMA,
            "call_id": call_id, "instance": instance, "model": model,
            "system_source": "fake", "grounded": bool(retr["count"]),
            "reground_applied": False, "rag_enabled": instance is not None,
            "retrieval": {"k": retr.get("k", 8), "count": retr["count"]},
            "latency": 0.01,
            "answer": f"[fake answer for: {prompt[:40]}]",
        }
        # A4: sources (the answerer's actual retrieved set) attach ONLY when
        # include_sources=True — F001 judge-verdict provenance asks for them.
        if include_sources:
            meta["sources"] = retr["hits"]
        self._calls[call_id] = meta
        return meta

    def get_call(self, call_id: str) -> dict[str, Any] | None:
        meta = self._calls.get(call_id)
        if meta is None:
            return None
        # redact bytes, keep trace fields
        return {k: v for k, v in meta.items()
                if k not in ("answer", "sources", "retrieval")} | {
            "source_count": meta["retrieval"]["count"]}

    # ---- 5. capability manifest ---------------------------------------------

    def capability_manifest(self) -> dict[str, Any]:
        return {
            "schema_version": CAPABILITIES_SCHEMA,
            "aiar_version": self._aiar_version,
            "backend_id": "fake-aiar",
            "features": dict(self._features),
            "schemas": {"retrieve": RETRIEVE_SCHEMA, "ingest": INGEST_SCHEMA,
                        "grounding": GROUNDING_SCHEMA},
        }
