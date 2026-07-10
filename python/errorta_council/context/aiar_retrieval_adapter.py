"""F031-RETRIEVAL Council/Query seam adapter.

Bridges errorta_query.Pipeline (judge-shaped) to Council's _QueryPipeline
Protocol (retrieval-shaped). This module is the SOLE link between
errorta_council and errorta_query — no other Council module should import
from errorta_query directly.

Invariants (F031-00):
- 3 (gateway is the only egress): this module imports errorta_query, never
  ``aiar``. AIAR's import site stays errorta_judge.aiar_adapter.
- The adapter swallows every exception from pipeline.query() and returns
  [] — retrieval failure does not block a Council turn (spec §Failure
  modes); the ContextRouter's BlockedContextResult path is reserved for
  policy failures, not adapter failures.

PM decisions (2026-06-12):
- Lazy pipeline construction with no caching — mirrors
  errorta_query.default_pipeline()'s "re-read residency on every call"
  policy, so a Settings panel mode switch between turns is honored.
- top_k passed through unchanged (router hardcodes 8 today).
- Multi-corpus iterate-and-merge happens inside Pipeline.query() (AIAR
  side) — the adapter just passes the list through.
- StubPipeline detection: instead of isinstance-checking, the adapter
  trusts the Pipeline contract — StubPipeline.query() already returns [].
  This keeps the adapter from importing StubPipeline.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from errorta_query.models import QueryResult
from errorta_query.pipeline import Pipeline

_LOG = logging.getLogger(__name__)


def _resolve_default_pipeline() -> Pipeline:
    """Call ``errorta_query.default_pipeline()`` via a fresh attribute lookup.

    Done this way (not as a captured function default) so that test code
    monkeypatching ``errorta_query.default_pipeline`` is honored. The cost
    is one module attribute lookup per turn — negligible. The original
    captured-default pattern silently bound the production factory at
    adapter-class evaluation time, defeating monkeypatch.
    """
    import errorta_query  # local: deferred to call time
    return errorta_query.default_pipeline()


class AiarRetrievalAdapter:
    """Council's ``_QueryPipeline``-shaped facade over errorta_query.Pipeline."""

    def __init__(
        self,
        pipeline_factory: Optional[Callable[[], Pipeline]] = None,
    ) -> None:
        # When no explicit factory is provided, look up
        # ``errorta_query.default_pipeline`` lazily at call time so that
        # tests can monkeypatch it. Passing a factory explicitly remains
        # the supported override for callers that want full control.
        self._factory = pipeline_factory or _resolve_default_pipeline

    def query(
        self,
        *,
        prompt: str,
        corpus_ids: list[str],
        top_k: int,
    ) -> list[QueryResult]:
        if not corpus_ids:
            return []
        try:
            pipe = self._factory()
        except Exception as exc:
            _LOG.warning(
                "retrieval_adapter_aiar_absent class=%s",
                exc.__class__.__name__,
            )
            return []
        try:
            results = pipe.query(
                prompt=prompt, corpus_ids=list(corpus_ids), top_k=top_k,
            )
        except Exception as exc:
            _LOG.warning(
                "retrieval_adapter_query_failed class=%s",
                exc.__class__.__name__,
            )
            return []
        return list(results or [])


__all__ = ["AiarRetrievalAdapter"]
