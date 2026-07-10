"""Typed result structures for the query/judge/grounding pipeline (F001).

Plain dataclasses with ``to_dict`` helpers so the sidecar can serialize them
to JSON for the frontend. Field names here are the wire contract the React side
is built against — keep them stable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class Verdict:
    """The LLM-judge's structured verdict for an answer.

    ``rating`` is one of ``good`` | ``partial`` | ``bad`` | ``unknown``. When the
    judge could not produce a usable verdict (timeout, schema failure,
    unparseable JSON), ``usable`` is False and ``failure_tags`` carries a tag
    like ``["judge_failed"]`` — the UI must say so honestly rather than silently
    fall back to ``bad`` (see the F001 acceptance criteria).
    """

    rating: str
    reason: str
    failure_tags: list[str] = field(default_factory=list)
    confidence: Optional[float] = None
    usable: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Retrieval:
    """Retrieval/grounding metadata surfaced as badges in the result panel."""

    grounded: bool = False
    reground_applied: bool = False
    top_k: int = 0
    chunks_used: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class QueryResult:
    """Single retrieved chunk from the F001 retrieval seam (F031-RETRIEVAL).

    Shape matches what RetrievalSeam._coerce() reads. Attribute access only —
    no dict-style; Council's seam handles both.

    F096 B1: the provenance fields below carry AIAR ``aiar.retrieve.v1`` hit
    metadata (``source``/``title``/``page_span``/``metadata``) through to the UI
    and diagnostics. All additive + defaulted, so existing constructors and the
    no-AIAR StubPipeline path are unchanged.
    """

    content: str
    corpus_id: str
    chunk_id: str
    citation_id: str
    score: Optional[float] = None
    tokens: Optional[int] = None
    source: Optional[str] = None
    title: Optional[str] = None
    page_span: Optional[tuple[int, int]] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class AnswerResult:
    """A complete query result: the answer plus judge + retrieval annotations."""

    answer: str
    model: Optional[str]
    verdict: Optional[Verdict]
    retrieval: Retrieval
    prompt_signature: str
    aiar: bool
    call_id: Optional[str] = None
    instance: Optional[str] = None
    grounded: Optional[bool] = None
    reground_applied: Optional[bool] = None
    rag_enabled: Optional[bool] = None
    latency: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "answer": self.answer,
            "model": self.model,
            "verdict": self.verdict.to_dict() if self.verdict else None,
            "retrieval": self.retrieval.to_dict(),
            "prompt_signature": self.prompt_signature,
            "aiar": self.aiar,
            "call_id": self.call_id,
            "instance": self.instance,
            "grounded": self.grounded,
            "reground_applied": self.reground_applied,
            "rag_enabled": self.rag_enabled,
            "latency": self.latency,
        }
