"""Local grounding store for the dev seam.

AIAR owns the *real* grounding store (``aiar.grounding.store``); this module
backs the ``StubPipeline`` so the accept -> reground loop runs end-to-end with
NO ``import aiar``. The ``AiarPipeline`` adapter records to AIAR's store
instead; this file stays as the stub's backing.

Corrections are persisted to ``$ERRORTA_HOME/grounding.json`` keyed by prompt
signature, written atomically (temp file + ``os.replace``) like
``errorta_corpus.manifest``. ``ERRORTA_HOME`` is honored exactly like
``errorta_corpus.store.errorta_home`` so tests stay off the real home dir.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional


def errorta_home() -> Path:
    """Compatibility shim. New code should import from ``errorta_app.paths``."""
    from errorta_app.paths import errorta_home as _h
    return _h()


def grounding_path() -> Path:
    from errorta_app.paths import grounding_json_path
    return grounding_json_path()


def _load_all() -> dict[str, str]:
    path = grounding_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def record(signature: str, correction: str) -> None:
    """Persist ``correction`` under ``signature`` (atomic write)."""
    correction = (correction or "").strip()
    if not correction:
        return
    path = grounding_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _load_all()
    data[signature] = correction
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)  # atomic on POSIX + Windows


def lookup(signature: str) -> Optional[str]:
    """Return the correction recorded for ``signature``, or None."""
    return _load_all().get(signature)


# ---------- F024: embedding-keyed similarity layer (default-off) ----------


def _embeddings_enabled() -> bool:
    raw = os.environ.get("ERRORTA_GROUNDING_EMBEDDINGS", "")
    return raw.strip().lower() in ("1", "true", "yes", "on")


def lookup_by_similarity(
    prompt_text: str,
    threshold: Optional[float] = None,
) -> Optional[tuple[str, str, float]]:
    """Return ``(signature, correction, similarity)`` for the best similar match.

    Returns ``None`` when ``ERRORTA_GROUNDING_EMBEDDINGS`` is unset (default
    behavior), when embeddings are unavailable (Ollama down), or when no record
    exceeds the threshold. Joins the matched signature back to the SHA-256-keyed
    correction text via ``lookup`` so the two layers share one source of truth.
    """
    if not _embeddings_enabled():
        return None
    # Import lazily so importing this module never requires httpx at runtime.
    from .embeddings import (
        DEFAULT_SIMILARITY_THRESHOLD,
        EmbeddingStore,
        EmbeddingUnavailable,
        ollama_embed,
    )

    try:
        query_emb = ollama_embed(prompt_text)
    except EmbeddingUnavailable:
        return None  # fail open — SHA-256 path still applies

    store = EmbeddingStore()
    matches = store.lookup_by_similarity(
        query_emb,
        threshold=threshold if threshold is not None else DEFAULT_SIMILARITY_THRESHOLD,
    )
    if not matches:
        return None
    top_sig, top_sim = matches[0]
    correction = lookup(top_sig)
    if not correction:
        return None
    return top_sig, correction, top_sim


def record_with_embedding(
    signature: str,
    correction: str,
    prompt_text: str,
) -> None:
    """Persist ``correction`` and (if env enabled) its embedding.

    The SHA-256 write always happens. The embedding write is best-effort — if
    Ollama is down we still record the correction so the exact-match path keeps
    working. Embedding writes are skipped entirely when the env var is unset.
    """
    record(signature, correction)
    if not _embeddings_enabled():
        return
    if not (correction or "").strip():
        return
    from .embeddings import EmbeddingStore, EmbeddingUnavailable, ollama_embed

    try:
        emb = ollama_embed(prompt_text)
    except EmbeddingUnavailable:
        return  # fail open
    try:
        EmbeddingStore().append(signature, emb, prompt_text)
    except OSError:
        return  # fail open on disk errors too
