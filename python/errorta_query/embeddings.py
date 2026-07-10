"""F024 — Embedding-keyed grounding backend (default-off).

Adds an optional similarity-based lookup layer on top of the existing
SHA-256-keyed grounding store. Behavior is gated by the
``ERRORTA_GROUNDING_EMBEDDINGS`` env var; with it unset, this module is dormant.

Embeddings are fetched from Ollama's ``/api/embeddings`` endpoint via httpx and
persisted as line-delimited JSON records keyed by the same prompt signature the
SHA-256 store uses, so the two layers join trivially:

    {"signature": "<hex>", "embedding": [..], "prompt_text": "<raw>", "created_at": "<iso>"}

Fail-open philosophy: any Ollama failure (unreachable, timeout, missing model)
raises ``EmbeddingUnavailable``; callers fall back to the SHA-256 path.

This module deliberately knows nothing about FastAPI or AIAR — it's a leaf
utility consumed by ``errorta_query.grounding``.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

import httpx

DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
DEFAULT_EMBED_MODEL = "nomic-embed-text"
DEFAULT_SIMILARITY_THRESHOLD = 0.85


class EmbeddingUnavailable(RuntimeError):
    """Raised when Ollama cannot return an embedding for any reason.

    Callers MUST treat this as a soft failure and fall back to the SHA-256
    grounding path. Never surfaced to end users directly.
    """


def _errorta_home() -> Path:
    """Compatibility shim. New code should import from ``errorta_app.paths``."""
    from errorta_app.paths import errorta_home
    return errorta_home()


def embeddings_path() -> Path:
    from errorta_app.paths import grounding_embeddings_path
    return grounding_embeddings_path()


def ollama_embed(
    text: str,
    host: Optional[str] = None,
    model: str = DEFAULT_EMBED_MODEL,
    timeout: float = 10.0,
) -> list[float]:
    """Fetch an embedding for ``text`` from Ollama.

    Raises ``EmbeddingUnavailable`` on any failure (network, timeout, missing
    model, malformed response). Never raises ``httpx`` errors to callers — the
    seam is intentional so the grounding layer can stay simple.
    """
    target_host = (host or os.environ.get("OLLAMA_HOST") or DEFAULT_OLLAMA_HOST).rstrip("/")
    url = f"{target_host}/api/embeddings"
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json={"model": model, "prompt": text})
        if resp.status_code != 200:
            raise EmbeddingUnavailable(
                f"ollama embeddings http {resp.status_code}"
            )
        data = resp.json()
    except httpx.HTTPError as exc:
        raise EmbeddingUnavailable(f"ollama embeddings transport: {exc}") from exc
    except ValueError as exc:  # json decode
        raise EmbeddingUnavailable(f"ollama embeddings json: {exc}") from exc

    emb = data.get("embedding") if isinstance(data, dict) else None
    if not isinstance(emb, list) or not emb:
        raise EmbeddingUnavailable("ollama embeddings: missing 'embedding' field")
    try:
        return [float(x) for x in emb]
    except (TypeError, ValueError) as exc:
        raise EmbeddingUnavailable(f"ollama embeddings: non-numeric values: {exc}") from exc


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Return cosine similarity in [-1, 1].

    Returns 0.0 if either vector is zero-length, has mismatched length, or has
    zero magnitude. This is a quiet guard — callers don't need to special-case
    degenerate inputs.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


class EmbeddingStore:
    """Append-only line-delimited JSON store for grounding embeddings.

    One record per line keeps writes O(1) and crash-safe (a torn write only
    corrupts the trailing line, which ``iter_records`` skips). The store is
    keyed by prompt signature for trivial joining with the SHA-256 store.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or embeddings_path()

    # ---- writes ----

    def append(self, signature: str, embedding: list[float], prompt_text: str) -> None:
        record = {
            "signature": signature,
            "embedding": list(embedding),
            "prompt_text": prompt_text,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        # Append + fsync — survives crash mid-write without rewriting the file.
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass

    # ---- reads ----

    def iter_records(self) -> Iterator[dict]:
        if not self.path.exists():
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        # Skip the torn trailing line; everything before it is fine.
                        continue
                    if isinstance(rec, dict):
                        yield rec
        except OSError:
            return

    def lookup_by_similarity(
        self,
        query_embedding: list[float],
        threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    ) -> list[tuple[str, float]]:
        """Return [(signature, similarity), ...] above ``threshold``, descending.

        Linear scan — fine for v0.1 scale (hundreds of corrections). If the
        store grows past ~10k records we revisit with a vector index.
        """
        matches: list[tuple[str, float]] = []
        for rec in self.iter_records():
            sig = rec.get("signature")
            emb = rec.get("embedding")
            if not isinstance(sig, str) or not isinstance(emb, list):
                continue
            sim = cosine_similarity(query_embedding, emb)
            if sim >= threshold:
                matches.append((sig, sim))
        matches.sort(key=lambda t: t[1], reverse=True)
        return matches


__all__ = [
    "EmbeddingStore",
    "EmbeddingUnavailable",
    "cosine_similarity",
    "embeddings_path",
    "ollama_embed",
    "DEFAULT_SIMILARITY_THRESHOLD",
]
