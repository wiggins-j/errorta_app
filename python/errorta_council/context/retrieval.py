"""Narrow read-only adapter over errorta_query (F001-SEAM pattern).

Returns SourceEnvelopes for downstream redaction/packing. Memoized within
a run by (member_id, transcript_cursor, retrieval_query_hash) so a round
robin that cycles through the same query doesn't double-pay.
"""
from __future__ import annotations

import hashlib
from typing import Protocol

from .transforms.schema import SourceEnvelope


class _QueryPipeline(Protocol):
    def query(self, *, prompt: str, corpus_ids: list[str], top_k: int): ...


def _coerce(r, attr: str) -> str | None:
    if isinstance(r, dict):
        v = r.get(attr)
    else:
        v = getattr(r, attr, None)
    return None if v is None else str(v)


class RetrievalSeam:
    def __init__(self, pipeline: _QueryPipeline | None) -> None:
        self._pipeline = pipeline
        self._memo: dict[str, list[SourceEnvelope]] = {}

    def fetch(
        self,
        *,
        member_id: str,
        prompt: str,
        corpus_ids: list[str],
        transcript_cursor: int,
        top_k: int = 8,
    ) -> list[SourceEnvelope]:
        if self._pipeline is None or not corpus_ids:
            return []
        key = self._key(member_id, prompt, corpus_ids, transcript_cursor)
        cached = self._memo.get(key)
        if cached is not None:
            return list(cached)
        results = self._pipeline.query(prompt=prompt, corpus_ids=corpus_ids, top_k=top_k)
        envelopes: list[SourceEnvelope] = []
        for r in results or []:
            content = _coerce(r, "content") or ""
            sha = hashlib.sha256(content.encode()).hexdigest()
            envelopes.append(SourceEnvelope(
                class_="retrieved_snippet",
                corpus_id=_coerce(r, "corpus_id") or "",
                chunk_id=_coerce(r, "chunk_id") or "",
                citation_id=_coerce(r, "citation_id") or "",
                content=content,
                content_sha256=sha,
                tokens=len(content.split()) or None,
                sensitivity="may_contain_corpus",
            ))
        self._memo[key] = list(envelopes)
        return envelopes

    @staticmethod
    def _key(member_id: str, prompt: str, corpus_ids: list[str], cursor: int) -> str:
        s = f"{member_id}|{cursor}|{sorted(corpus_ids)}|{prompt}"
        return hashlib.sha256(s.encode()).hexdigest()


__all__ = ["RetrievalSeam"]
