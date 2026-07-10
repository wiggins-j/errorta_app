"""F031-RETRIEVAL Council/Query seam tests.

Asserts:
- adapter returns [] when factory yields a StubPipeline (no-AIAR path);
- adapter returns [] when corpus_ids is empty (defensive; seam also guards);
- adapter swallows pipeline.query() exceptions and returns [];
- adapter passes top_k through to the underlying pipeline;
- adapter module does NOT import `aiar` (invariant 3 lock).
"""
from __future__ import annotations

import sys

from errorta_council.context.aiar_retrieval_adapter import AiarRetrievalAdapter
from errorta_query.models import QueryResult
from errorta_query.pipeline import StubPipeline


class _CapturingPipeline:
    def __init__(self, results=None, raises=None):
        self.calls: list[dict] = []
        self._results = results or []
        self._raises = raises

    def query(self, *, prompt, corpus_ids, top_k):
        self.calls.append(
            {"prompt": prompt, "corpus_ids": list(corpus_ids), "top_k": top_k}
        )
        if self._raises:
            raise self._raises
        return list(self._results)


def test_adapter_returns_empty_when_factory_yields_stub():
    adapter = AiarRetrievalAdapter(pipeline_factory=StubPipeline)
    out = adapter.query(prompt="any", corpus_ids=["welcome"], top_k=8)
    assert out == []


def test_adapter_returns_empty_when_corpus_ids_is_empty():
    pipe = _CapturingPipeline(
        results=[
            QueryResult(
                content="x",
                corpus_id="c1",
                chunk_id="ch1",
                citation_id="ct1",
            )
        ]
    )
    adapter = AiarRetrievalAdapter(pipeline_factory=lambda: pipe)
    out = adapter.query(prompt="any", corpus_ids=[], top_k=8)
    assert out == []
    # Defensive: the adapter should not bother calling the pipeline either.
    # (Council's RetrievalSeam already short-circuits, but the adapter
    # double-checks to keep the seam composable.)
    assert pipe.calls == []


def test_adapter_swallows_query_exception_returns_empty():
    pipe = _CapturingPipeline(raises=RuntimeError("AIAR went sideways"))
    adapter = AiarRetrievalAdapter(pipeline_factory=lambda: pipe)
    out = adapter.query(prompt="hi", corpus_ids=["welcome"], top_k=8)
    assert out == []


def test_adapter_passes_top_k_through_to_pipeline():
    pipe = _CapturingPipeline(results=[])
    adapter = AiarRetrievalAdapter(pipeline_factory=lambda: pipe)
    adapter.query(prompt="hi", corpus_ids=["welcome"], top_k=3)
    assert pipe.calls[0]["top_k"] == 3


def test_adapter_does_not_import_aiar_at_module_level():
    """Invariant 3 lock — Council code never imports aiar directly.

    The adapter imports errorta_query.default_pipeline (which lazily
    routes to AIAR via errorta_judge.aiar_adapter — the F001 seam).
    Importing the adapter module must not pull `aiar` into sys.modules
    on its own.
    """
    # Force a clean re-import of just the adapter module.
    sys.modules.pop("errorta_council.context.aiar_retrieval_adapter", None)
    # Also clear aiar if a prior test pulled it in via the F001 path.
    aiar_keys = [k for k in sys.modules if k == "aiar" or k.startswith("aiar.")]
    for k in aiar_keys:
        sys.modules.pop(k, None)

    import errorta_council.context.aiar_retrieval_adapter  # noqa: F401

    assert "aiar" not in sys.modules, (
        "AiarRetrievalAdapter module pulled `aiar` into sys.modules — "
        "invariant 3 violated. The adapter must import only from errorta_query."
    )
