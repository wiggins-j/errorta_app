"""F096 B1 — real pure-retrieval against a remote AIAR instance (example-host).

This is the live path: when a remote AIAR is configured (``remote-aiar.json`` /
``ERRORTA_AIAR_REMOTE_URL`` — the maintainer's example-host server), Council/judge
retrieval queries AIAR's ``aiar.retrieve.v1`` route
(``POST /instances/{instance}/retrieve``) and maps the returned hits to
``QueryResult``. No model is invoked (pure vector retrieval); no FakeAiar.

The backend target (+ auth token) is resolved through the F096 B4 seam
``errorta_query.backend.aiar_retrieval_target`` so this module never reads
residency/remote config directly. Fail-safe: any transport/parse error on a
corpus yields no hits for that corpus rather than raising, so a retrieval blip
degrades to empty source_refs (explainable) instead of breaking a turn.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .models import QueryResult

_LOG = logging.getLogger(__name__)
_TIMEOUT_S = 20.0


class AiarRetrieveError(RuntimeError):
    """Strict remote AIAR retrieval failed before a verified zero-hit response."""


def _bearer(token: str | None) -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def map_hit_to_query_result(hit: dict[str, Any], *, instance: str) -> QueryResult:
    """Map one ``aiar.retrieve.v1`` hit to a ``QueryResult`` (F096 contract)."""
    span = hit.get("page_span")
    page_span = tuple(span) if isinstance(span, (list, tuple)) and len(span) == 2 else None
    chunk_id = str(hit.get("chunk_id") or "")
    return QueryResult(
        content=str(hit.get("text") or ""),
        corpus_id=instance,
        chunk_id=chunk_id,
        # AIAR has no separate citation id; the chunk id is the stable cite key.
        citation_id=chunk_id,
        score=hit.get("score"),
        tokens=None,
        source=hit.get("source"),
        title=hit.get("title"),
        page_span=page_span,
        metadata=dict(hit.get("metadata") or {}),
    )


def _redact(message: str, headers: dict[str, str]) -> str:
    secret = headers.get("Authorization") or ""
    if secret and secret in message:
        return message.replace(secret, "<redacted>")
    return message


def _retrieve_one(
    client: httpx.Client,
    base_url: str,
    headers: dict[str, str],
    instance: str,
    prompt: str,
    k: int,
    *,
    strict: bool,
) -> list[QueryResult]:
    url = f"{base_url.rstrip('/')}/instances/{instance}/retrieve"
    try:
        resp = client.post(url, headers=headers, json={"q": prompt, "k": k})
    except (httpx.HTTPError, OSError) as exc:
        safe = _redact(str(exc), headers)
        if strict:
            raise AiarRetrieveError(f"aiar retrieve transport error for {instance}") from None
        _LOG.warning("aiar retrieve transport error for %s: %s", instance, safe)
        return []
    if resp.status_code != 200:
        if strict:
            raise AiarRetrieveError(f"aiar retrieve {instance} -> HTTP {resp.status_code}")
        _LOG.warning("aiar retrieve %s -> HTTP %s", instance, resp.status_code)
        return []
    try:
        payload = resp.json()
    except ValueError:
        if strict:
            raise AiarRetrieveError(f"aiar retrieve {instance} returned invalid JSON") from None
        return []
    hits = payload.get("hits") if isinstance(payload, dict) else None
    if not isinstance(hits, list):
        if strict:
            raise AiarRetrieveError(f"aiar retrieve {instance} returned malformed hits")
        return []
    return [map_hit_to_query_result(h, instance=instance)
            for h in hits if isinstance(h, dict)]


def remote_aiar_retrieve(*, prompt: str, corpus_ids: list[str],
                         top_k: int, strict: bool = False) -> list[QueryResult]:
    """Pure-retrieve ``prompt`` across ``corpus_ids`` from the configured remote
    AIAR, merged by score and trimmed to ``top_k``. Returns ``[]`` when no remote
    AIAR is configured (the caller falls back to the local/stub path)."""
    from .backend import aiar_retrieval_target

    target = aiar_retrieval_target()
    if target is None:
        if strict:
            raise AiarRetrieveError("remote AIAR retrieval target unavailable")
        return []
    base_url, token = target
    if not (prompt or "").strip() or not corpus_ids:
        return []
    headers = _bearer(token)
    out: list[QueryResult] = []
    # Over-fetch per corpus, then merge+trim — mirrors the iterate-and-merge
    # shape the local AiarPipeline.query uses.
    with httpx.Client(timeout=_TIMEOUT_S) as client:
        for cid in corpus_ids:
            if not cid:
                continue
            out.extend(
                _retrieve_one(
                    client,
                    base_url,
                    headers,
                    cid,
                    prompt,
                    top_k,
                    strict=strict,
                )
            )
    out.sort(key=lambda r: (r.score if r.score is not None else 0.0), reverse=True)
    return out[:top_k]
