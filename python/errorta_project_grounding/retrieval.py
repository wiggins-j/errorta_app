"""F088 enablement Slice 4 — retrieval over a project's bound corpus.

Makes corpus retrieval AVAILABLE (remote AIAR when configured, else the local
in-process adapter) so the consumption slices (F088-08 PM briefing, F088-09 dev
context requests) can pull corpus evidence. This slice does NOT inject anything
into member prompts — it only makes retrieval correct/reachable.

Retrieval failure NEVER blocks a turn: every error path degrades to ``[]`` so a
corpus miss / unreachable remote / unbound corpus looks like "no evidence", not
a crash.
"""
from __future__ import annotations

import logging
from typing import Any

from .adapter import GroundingHit

# Trace logger for grounding consumption — INFO lines let an operator follow a
# run end to end (set log level to DEBUG/INFO and tail the sidecar log). Lines
# carry metadata only (corpus id, status, hit count, a truncated query) — never
# raw corpus content.
_LOG = logging.getLogger("errorta.grounding")


def _residency_is_remote() -> bool:
    try:
        from errorta_residency import config as residency_config
        return getattr(residency_config.load(), "mode", "local") != "local"
    except Exception:
        return False


def _adapter_for_project() -> Any:
    """Remote AIAR adapter when configured, else the local default. Under remote
    residency with NO remote AIAR, returns None (fail closed) rather than reading
    the local corpus — that would violate the residency promise."""
    try:
        from .remote_adapter import active_remote_adapter
        remote = active_remote_adapter()
        if remote is not None:
            return remote
    except Exception:
        pass
    if _residency_is_remote():
        return None  # no remote AIAR + remote residency -> never fall back to local
    try:
        from .adapter import default_project_grounding_adapter
        return default_project_grounding_adapter()
    except Exception:
        return None


def retrieve_with_status(store: Any, *, query: str, top_k: int = 6,
                         filters: dict[str, Any] | None = None) -> tuple[list[GroundingHit], str]:
    """Like ``retrieve_project_corpus`` but returns ``(hits, status)`` so callers
    can distinguish *no evidence* from *failure*. status:
    ``no_corpus`` | ``empty_query`` | ``unavailable`` | ``ok``."""
    project_id = getattr(store, "project_id", "?")
    q_trace = (query or "").strip().replace("\n", " ")[:80]
    if not query or not query.strip():
        return [], "empty_query"
    try:
        from .corpus_binding import load_binding
        binding = load_binding(store)
    except Exception:
        _LOG.info("grounding retrieve: project=%s status=unavailable (binding error)",
                  project_id)
        return [], "unavailable"
    if not binding.corpus_id or binding.mode == "none":
        _LOG.info("grounding retrieve: project=%s status=no_corpus", project_id)
        return [], "no_corpus"
    adapter = _adapter_for_project()
    adapter_source = "remote" if type(adapter).__name__ == "RemoteAiarCorpusAdapter" else (
        "none" if adapter is None else "local")
    retrieve = getattr(adapter, "retrieve", None)
    if not callable(retrieve):
        # no usable adapter (e.g. remote-residency fail-close) -> unavailable,
        # NOT silently "no evidence".
        _LOG.info("grounding retrieve: project=%s corpus=%s status=unavailable "
                  "adapter=%s (no retrieve)", project_id, binding.corpus_id, adapter_source)
        return [], "unavailable"
    try:
        hits = list(retrieve(corpus_id=binding.corpus_id, query=query,
                             top_k=int(top_k), filters=filters) or [])
        _LOG.info("grounding retrieve: project=%s corpus=%s status=ok hits=%d "
                  "top_k=%d adapter=%s query=%r",
                  project_id, binding.corpus_id, len(hits), int(top_k),
                  adapter_source, q_trace)
        return hits, "ok"
    except Exception:
        # unreachable remote / auth / unsupported filter -> unavailable.
        _LOG.info("grounding retrieve: project=%s corpus=%s status=unavailable "
                  "adapter=%s query=%r", project_id, binding.corpus_id,
                  adapter_source, q_trace)
        return [], "unavailable"


def retrieve_project_corpus(store: Any, *, query: str, top_k: int = 6,
                            filters: dict[str, Any] | None = None) -> list[GroundingHit]:
    """Retrieve from the project's bound corpus. Fail-safe — returns ``[]`` for
    any non-ok status so retrieval never raises into a turn."""
    return retrieve_with_status(store, query=query, top_k=top_k, filters=filters)[0]


__all__ = ["retrieve_project_corpus", "retrieve_with_status"]
