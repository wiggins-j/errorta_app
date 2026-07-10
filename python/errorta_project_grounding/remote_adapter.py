"""Errorta-side client for a REMOTE AIAR corpus (Mac → watchdog AIAR).

Implements the ``ProjectGroundingAdapter`` protocol against the authenticated
HTTP API specified in ``docs/specs/AIAR-remote-corpus-ingest-api.md``. Use it
when Errorta runs on the Mac but the AIAR instance lives on another host
(reached over an SSH tunnel): corpus creation + document ingest are POSTed to the
remote AIAR, which chunks + embeds server-side so the corpus uses AIAR's
embedder (never a client one).

Built in PARALLEL with the AIAR-side endpoints — it targets the spec contract,
not a live server, and is unit-tested with an injected transport. It is INERT
until configured (``ERRORTA_AIAR_REMOTE_URL`` unset → ``remote_aiar_config()``
returns None → ``default_project_grounding_adapter`` keeps today's behavior).

Security: secret-bearing content and denied paths are screened with the shared
``paths`` policy BEFORE anything leaves the machine; the client never sends
embedding vectors (the server embeds).
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

import httpx

from . import paths as _paths
from .adapter import (
    GroundingHit,
    GroundingRecordRef,
    ProjectGroundingError,
    UnsupportedGroundingOperation,
)
from .capabilities import AiarGroundingCapabilities


@dataclass(frozen=True)
class RemoteAiarConfig:
    base_url: str
    token: str | None = None
    timeout_s: float = 60.0
    verify: bool = True


def remote_aiar_config() -> RemoteAiarConfig | None:
    """Read remote-AIAR settings from persisted config/env, or None when unset.

    Persisted settings win over env vars. ``ERRORTA_AIAR_REMOTE_URL`` remains
    the fallback gate — typically the local end of an SSH tunnel to the AIAR
    host (e.g. ``http://127.0.0.1:8766``)."""
    try:
        from .remote_config import effective_for_adapter
        stored = effective_for_adapter()
    except Exception:
        stored = None
    if stored is not None:
        # F089 managed mode: derive base_url from the live local end of the
        # Errorta-owned SSH tunnel (ensure brings it up / reuses it). BYO mode
        # keeps the stored base_url verbatim.
        base_url = stored.base_url.rstrip("/")
        if getattr(stored, "managed", False):
            try:
                from errorta_tunnels import tunnel_manager

                from .remote_config import tunnel_spec
                spec = tunnel_spec(stored)
                if spec is not None:
                    local_port = tunnel_manager.ensure(spec)
                    base_url = f"http://127.0.0.1:{local_port}"
            except Exception:
                # Tunnel couldn't be ensured -> fall through with whatever
                # base_url we have; the HTTP call surfaces the failure honestly.
                pass
        # A stored URL with no token still falls back to the env token, so an
        # operator who set ERRORTA_AIAR_REMOTE_TOKEN isn't silently left
        # tokenless just because they saved an endpoint without one.
        env_token = (os.environ.get("ERRORTA_AIAR_REMOTE_TOKEN") or "").strip() or None
        return RemoteAiarConfig(
            base_url=base_url,
            token=stored.token or env_token,
            timeout_s=stored.timeout_s,
            verify=stored.verify,
        )
    url = (os.environ.get("ERRORTA_AIAR_REMOTE_URL") or "").strip()
    if not url:
        return None
    try:
        timeout = float(os.environ.get("ERRORTA_AIAR_REMOTE_TIMEOUT", "60") or "60")
    except ValueError:
        timeout = 60.0
    verify = (os.environ.get("ERRORTA_AIAR_REMOTE_VERIFY", "1") or "1").lower() not in (
        "0", "false", "no")
    return RemoteAiarConfig(
        base_url=url.rstrip("/"),
        token=(os.environ.get("ERRORTA_AIAR_REMOTE_TOKEN") or "").strip() or None,
        timeout_s=timeout,
        verify=verify,
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _screen_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Enforce the no-secrets / no-vectors-before-egress invariant on
    caller-supplied metadata (not just document text). Rejects metadata that
    serializes to anything secret-shaped, or that carries an embedding-vector-
    like value (a long numeric list)."""
    import json as _json
    meta = dict(metadata or {})
    if _paths.content_has_secret(_json.dumps(meta, default=str)):
        raise ProjectGroundingError("refusing to send secret-bearing metadata")
    vector_path = _vector_like_path(meta)
    if vector_path:
        raise ProjectGroundingError(
            f"refusing to send a vector-like metadata value ({vector_path}); "
            "the remote AIAR embeds server-side")
    return meta


def _vector_like_path(value: Any, path: str = "metadata") -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            hit = _vector_like_path(child, f"{path}.{key}")
            if hit:
                return hit
        return None
    if isinstance(value, (list, tuple)):
        if (len(value) >= 16
                and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in value)):
            return path
        for idx, child in enumerate(value):
            hit = _vector_like_path(child, f"{path}[{idx}]")
            if hit:
                return hit
    return None


def _source_from_metadata(metadata: dict[str, Any] | None, fallback: str) -> str:
    source = str((metadata or {}).get("source") or (metadata or {}).get("path") or fallback)
    if Path(source).is_absolute():
        return Path(source).name or fallback
    if PureWindowsPath(source).is_absolute():
        return PureWindowsPath(source).name or fallback
    return source


class RemoteAiarCorpusAdapter:
    """ProjectGroundingAdapter over the remote AIAR ingest/instance API."""

    def __init__(self, config: RemoteAiarConfig, *,
                 transport: httpx.BaseTransport | None = None) -> None:
        self._cfg = config
        self._transport = transport  # injected in tests; None = real network
        self._pure_retrieve_cached: bool | None = None  # /healthz pure_retrieve marker

    # --- HTTP plumbing -----------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._cfg.token:
            headers["Authorization"] = f"Bearer {self._cfg.token}"
        return headers

    def _request(self, method: str, path: str, *, json: Any = None) -> dict[str, Any]:
        url = f"{self._cfg.base_url}{path}"
        try:
            with httpx.Client(timeout=httpx.Timeout(self._cfg.timeout_s),
                              verify=self._cfg.verify,
                              transport=self._transport) as client:
                resp = client.request(method, url, json=json, headers=self._headers())
        except (httpx.HTTPError, OSError) as exc:
            raise ProjectGroundingError(f"remote AIAR unreachable: {exc}") from exc
        if resp.status_code == 401:
            raise ProjectGroundingError("remote AIAR rejected the token (401)")
        if resp.status_code == 503:
            raise ProjectGroundingError("remote AIAR store/embedder not ready (503)")
        if resp.status_code >= 400:
            raise ProjectGroundingError(
                f"remote AIAR {method} {path} failed: {resp.status_code} {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError:
            return {}

    # --- capabilities ------------------------------------------------------
    def capabilities(self) -> AiarGroundingCapabilities:
        """Probe remote ``/healthz``; fail closed to unavailable on any error."""
        try:
            h = self._request("GET", "/healthz")
        except ProjectGroundingError:
            return AiarGroundingCapabilities(
                available=False, version=None, source="remote-unreachable",
                supports_corpus_ids=False, supports_file_ingest=False,
                supports_record_ingest=False, supports_metadata_filters=False,
                supports_provenance_metadata=False, supports_incremental_refresh=False,
                supports_supersession=False, supports_export_import=False,
                local_only_embedding=False, notes=("remote AIAR unreachable",))
        rag = h.get("rag") if isinstance(h.get("rag"), dict) else h
        ready = bool(rag.get("store_ready") and rag.get("embedder_ready"))
        # Ingest capability is NOT implied by store/embedder readiness — today's
        # AIAR service exposes query/eval only. Trust it only when the service
        # explicitly advertises the remote-ingest API (spec §9): a healthy but
        # ingest-less AIAR must report ingest UNSUPPORTED, not 404 later.
        ingest = bool(h.get("remote_ingest") or rag.get("remote_ingest"))
        note = (f"remote AIAR @ {self._cfg.base_url}; "
                f"embedder={rag.get('embedding_model', '?')}")
        if ready and not ingest:
            note += "; remote ingest API NOT advertised (query-only)"
        return AiarGroundingCapabilities(
            available=ready, version=h.get("version"), source="remote",
            supports_corpus_ids=ingest, supports_file_ingest=ingest,
            supports_record_ingest=ingest,
            supports_metadata_filters=bool(rag.get("supports_metadata_filters")),
            supports_provenance_metadata=ingest, supports_incremental_refresh=ingest,
            supports_supersession=False, supports_export_import=False,
            # The corpus is embedded on the REMOTE host, never locally.
            local_only_embedding=False,
            notes=(note,))

    def _require_write_token(self) -> None:
        """Spec §10.1 locks AIAR_SERVICE_TOKEN as REQUIRED for writes. Fail
        closed CLIENT-side so a misconfigured client never transmits document
        payloads (or instance mutations) to an endpoint over an unauthenticated
        request — even though the server would also reject it."""
        if not self._cfg.token:
            raise ProjectGroundingError(
                "remote AIAR writes require a token; set ERRORTA_AIAR_REMOTE_TOKEN")

    # --- instance management ----------------------------------------------
    def ensure_instance(self, corpus_id: str, *, display_name: str | None = None) -> dict[str, Any]:
        self._require_write_token()
        return self._request("POST", "/instances",
                             json={"name": corpus_id, "display_name": display_name})

    def instance_health(self, corpus_id: str) -> dict[str, Any]:
        return self._request("GET", f"/instances/{corpus_id}/health")

    def list_instances(self) -> list[dict[str, Any]]:
        """Remote corpora (AIAR instances). Fail-safe to [] — a listing failure
        must not break the corpora panel."""
        try:
            resp = self._request("GET", "/instances")
        except ProjectGroundingError:
            return []
        items = resp.get("instances") if isinstance(resp, dict) else resp
        return [i for i in (items or []) if isinstance(i, dict)]

    def publish(self, corpus_id: str) -> dict[str, Any]:
        self._require_write_token()
        return self._request("POST", f"/instances/{corpus_id}/publish")

    # --- ingest ------------------------------------------------------------
    def _post_document(self, corpus_id: str, *, source: str, title: str,
                       text: str, metadata: dict[str, Any],
                       pages: list[dict[str, Any]] | None = None) -> GroundingRecordRef:
        self._require_write_token()
        if not text.strip():
            raise ProjectGroundingError("refusing to ingest empty document")
        # Secrets never leave the machine — same screen the local store uses,
        # applied to BOTH the document text AND caller-supplied metadata.
        clean_meta = _screen_metadata(metadata)
        if _paths.content_has_secret(text):
            raise ProjectGroundingError("refusing to ingest secret-bearing content")
        doc: dict[str, Any] = {
            "doc_id": _sha256(text),  # idempotency key (canonical text)
            "source": source, "title": title, "text": text,
            "metadata": clean_meta,
        }
        # Per-page text lets AIAR derive Chunk.page_span (F013 source-jump);
        # omitted for non-paged formats -> server falls back to page_span=None.
        if pages:
            doc["pages"] = pages
        resp = self._request("POST", f"/instances/{corpus_id}/documents",
                            json={"documents": [doc]})
        job_id = str(resp.get("job_id") or "")
        if not job_id:
            # 'accepted' is docs RECEIVED, not stored — without a job to confirm
            # storage we cannot claim the corpus was written. Fail closed.
            raise ProjectGroundingError(
                "remote ingest returned no job_id; cannot confirm the document was stored")
        job = self._await_ingest(corpus_id, job_id)
        record_id = job_id
        return GroundingRecordRef(
            corpus_id=corpus_id, record_id=record_id,
            metadata={**clean_meta,
                      "chunks_added": int(job.get("chunks_added") or 0),
                      "duplicates": int(job.get("duplicates") or 0)})

    def _await_ingest(self, corpus_id: str, job_id: str, *,
                      max_polls: int = 8, interval_s: float = 0.25) -> dict[str, Any]:
        """Poll the ingest job and CONFIRM it actually stored content. The server
        runs inline (sync behind an async contract), so this is usually one GET;
        the bounded loop tolerates a future async runner. Fails closed on a
        failed job, any errors, or 'done' with nothing stored (no chunks added
        AND no duplicates) — so a partial/empty ingest can never look successful.
        A done job with chunks_added==0 but duplicates>0 is a SUCCESSFUL
        idempotent re-ingest, not a failure."""
        import time
        job: dict[str, Any] = {}
        for i in range(max_polls):
            job = self._request("GET", f"/instances/{corpus_id}/ingest-jobs/{job_id}")
            status = str(job.get("status") or "")
            if status in ("done", "failed"):
                break
            if i < max_polls - 1:
                time.sleep(interval_s)
        status = str(job.get("status") or "")
        errors = job.get("errors") or []
        chunks_added = int(job.get("chunks_added") or 0)
        duplicates = int(job.get("duplicates") or 0)
        if status == "failed" or errors:
            raise ProjectGroundingError(f"remote ingest failed: {errors or status or 'unknown'}")
        if status != "done":
            raise ProjectGroundingError(f"remote ingest did not complete (status={status!r})")
        if chunks_added == 0 and duplicates == 0:
            raise ProjectGroundingError(
                "remote ingest stored nothing (chunks_added=0, duplicates=0)")
        return job

    def ingest_file(self, *, corpus_id: str, path: Path,
                    metadata: dict[str, Any]) -> GroundingRecordRef:
        p = Path(path)
        if _paths.is_sensitive_path(str(p)):
            raise ProjectGroundingError("refusing to ingest a sensitive/denied path")
        if not p.is_file():
            raise ProjectGroundingError(f"not a file: {p}")
        from errorta_extract.registry import get_extractor, supported_extensions

        from .bootstrap import CODE_EXTENSIONS
        ext = p.suffix.lower()
        pages: list[dict[str, Any]] = []
        if ext in set(supported_extensions()):
            # A document format (PDF/DOCX/…): run the extractor + carry page text
            # so AIAR can derive page_span.
            try:
                chunks = get_extractor(ext)(p)
            except Exception as exc:
                raise ProjectGroundingError(f"extraction failed: {exc}") from exc
            text = "\n\n".join(str(c.get("text", "")) for c in chunks if c.get("text"))
            for c in chunks:
                pg = (c.get("meta") or {}).get("page_number")
                txt = str(c.get("text", ""))
                if pg is not None and txt:
                    pages.append({"page": int(pg), "text": txt})
        elif ext in CODE_EXTENSIONS:
            # F088-04: source/text files are already plain text — read directly
            # (no document extractor exists for code). AIAR chunks + embeds it.
            try:
                text = p.read_text("utf-8", errors="replace")
            except Exception as exc:
                raise ProjectGroundingError(f"read failed: {exc}") from exc
        else:
            raise ProjectGroundingError(f"unsupported file extension: {ext}")
        # The remote dedups on `source`, so it must be STABLE + UNIQUE per
        # document. A caller-supplied source (e.g. the corpus-relative path) is
        # preferred; the bare filename is the fallback (NOT unique across dirs —
        # callers ingesting many files should pass metadata["source"]). Never
        # send the absolute path (leaks the Mac's home/username/FS layout).
        source = _source_from_metadata(metadata, p.name)
        return self._post_document(corpus_id, source=source, title=p.name,
                                  text=text, metadata=metadata, pages=pages or None)

    def ingest_record(self, *, corpus_id: str, content: str,
                      metadata: dict[str, Any]) -> GroundingRecordRef:
        title = str((metadata or {}).get("title") or "record")
        # Dedup keys on `source`; a constant default would collapse every record
        # into one. Use the caller's stable source, else a content-derived id so
        # distinct records never collide (identical content dedups, as intended).
        source = _source_from_metadata(metadata, f"errorta-record:{_sha256(content)[:16]}")
        return self._post_document(corpus_id, source=source, title=title,
                                  text=content, metadata=metadata)

    # --- retrieval ---------------------------------------------------------
    def _pure_retrieve_available(self) -> bool:
        """Whether the remote AIAR advertises the pure-retrieve endpoint
        (``/healthz`` ``pure_retrieve`` marker, AIAR >= 0.2.3). Probed once and
        cached on this adapter; fails closed to the legacy path on any error."""
        if self._pure_retrieve_cached is None:
            try:
                h = self._request("GET", "/healthz")
                self._pure_retrieve_cached = bool(h.get("pure_retrieve"))
            except ProjectGroundingError:
                self._pure_retrieve_cached = False
        return self._pure_retrieve_cached

    def retrieve(self, *, corpus_id: str, query: str, top_k: int,
                 filters: dict[str, Any] | None = None) -> list[GroundingHit]:
        if filters:
            # The remote query route does not forward structured filters yet;
            # fail closed rather than silently drop them (matches the local
            # adapter's contract).
            raise UnsupportedGroundingOperation(
                "metadata filters are not forwarded to remote AIAR")
        # PRIMARY (AIAR >= 0.2.3): the pure-retrieve endpoint returns ranked
        # chunks with NO LLM call (cheaper, needs no generation model pulled, and
        # gives per-chunk citations). Use it whenever the server advertises it.
        if self._pure_retrieve_available():
            from urllib.parse import quote
            path = (f"/instances/{quote(corpus_id, safe='')}/retrieve"
                    f"?q={quote(query, safe='')}&k={int(top_k)}")
            resp = self._request("GET", path)
            return _parse_pure_retrieve(resp, corpus_id)
        # FALLBACK (query-only AIAR without the marker): /services/prompt couples
        # retrieval with generation and returns a grounded ANSWER (not chunks),
        # which we represent as a single evidence hit. service_name + prompt are
        # required; `instance` selects the RAG instance, `rag:true` enables
        # retrieval. Model defaults to AIAR's active_model unless the operator
        # pins ERRORTA_AIAR_REMOTE_MODEL.
        body: dict[str, Any] = {
            "service_name": "errorta-grounding", "prompt": query,
            "instance": corpus_id, "rag": True, "judge": False, "think": False,
            "top_k": int(top_k),
        }
        model = (os.environ.get("ERRORTA_AIAR_REMOTE_MODEL") or "").strip()
        if model:
            body["model"] = model
        resp = self._request("POST", "/services/prompt", json=body)
        return _parse_retrieval(resp, corpus_id)


def _parse_pure_retrieve(resp: dict[str, Any], corpus_id: str) -> list[GroundingHit]:
    """Map an AIAR pure-retrieve response (``GET /instances/{id}/retrieve``,
    spec ``AIAR-pure-retrieve-endpoint.md``) to GroundingHits — one per chunk,
    preserving real citations (source/title/page_span) and the relevance score
    (``score_kind=cosine_similarity``, higher better)."""
    instance = str(resp.get("instance") or corpus_id)
    score_kind = resp.get("score_kind")
    hits: list[GroundingHit] = []
    for it in resp.get("hits") or []:
        if not isinstance(it, dict):
            continue
        content = str(it.get("text") or "")
        if not content:
            continue
        score = it.get("score")
        valid_score = isinstance(score, (int, float)) and not isinstance(score, bool)
        hits.append(GroundingHit(
            content=content,
            corpus_id=instance,
            chunk_id=str(it.get("chunk_id") or ""),
            score=score if valid_score else None,
            metadata={
                "source": it.get("source"),
                "title": it.get("title"),
                "page_span": it.get("page_span"),
                "category": it.get("category"),
                "chunk_index": it.get("chunk_index"),
                "score_kind": score_kind,
            },
        ))
    return hits


def _parse_retrieval(resp: dict[str, Any], corpus_id: str) -> list[GroundingHit]:
    """Map an AIAR /services/prompt response to GroundingHits.

    AIAR returns a grounded ANSWER (keys: answer / grounded / instance / model),
    not a chunk list. Represent a grounded answer as ONE evidence hit; an
    ungrounded or empty answer yields [] (never inject the model's general
    knowledge as corpus evidence). If a future AIAR pure-retrieve endpoint
    returns real chunks, the chunk-list branch below maps them."""
    answer = str(resp.get("answer") or "").strip()
    if answer and resp.get("grounded") is not False and (
            resp.get("grounded") is True or resp.get("context_used")):
        return [GroundingHit(
            content=answer, corpus_id=str(resp.get("instance") or corpus_id),
            chunk_id=str(resp.get("call_id") or "services_prompt_answer"),
            score=None,
            metadata={"grounded": bool(resp.get("grounded")),
                      "model": resp.get("model"), "source": "services_prompt"})]
    block = resp.get("retrieval") if isinstance(resp.get("retrieval"), dict) else resp
    items = (block.get("chunks") or block.get("sources") or block.get("citations")
             or resp.get("sources") or [])
    if not isinstance(items, list):
        return []
    hits: list[GroundingHit] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        content = str(it.get("text") or it.get("content") or "")
        if not content:
            continue
        hits.append(GroundingHit(
            content=content,
            corpus_id=str(it.get("corpus_id") or corpus_id),
            chunk_id=str(it.get("chunk_id") or it.get("id") or ""),
            score=it.get("score") if isinstance(it.get("score"), (int, float)) else None,
            metadata={k: v for k, v in it.items()
                      if k not in ("text", "content", "chunk_id", "id", "score")},
        ))
    return hits


def active_remote_adapter() -> "RemoteAiarCorpusAdapter | None":
    """The configured remote adapter (corpus on a remote AIAR), or None for the
    local path.

    F116 makes the active AIAR connection authority canonical. A selected raw
    AIAR service wins here, including canonical ``aiar-connection.json`` and the
    legacy ``remote-aiar.json`` migration path exposed through the resolver. A
    remote Errorta sidecar is not a raw AIAR service and keeps using product
    route proxying instead of this instance API adapter.
    """
    # Resolve the SELECTED config without probing the network — this is a hot
    # path (corpus listing, coding grounding, retrieval) and must not block on a
    # slow/unreachable backend just to decide which adapter to construct. The
    # actual data calls carry their own httpx timeouts.
    try:
        from errorta_aiar_connection.resolver import resolve_aiar_config

        config, source = resolve_aiar_config()
        if source == "ambiguous_legacy":
            return None
        if config is not None:
            if config.kind == "aiar-service" and config.base_url:
                return RemoteAiarCorpusAdapter(
                    RemoteAiarConfig(
                        base_url=config.base_url,
                        token=config.token,
                        timeout_s=config.timeout_s,
                        verify=config.verify_tls,
                    )
                )
            # errorta-sidecar-remote / local-aiar / disconnected: not a raw AIAR
            # instance API, so no corpus adapter here.
            return None
    except Exception:
        pass
    cfg = remote_aiar_config()
    return RemoteAiarCorpusAdapter(cfg) if cfg is not None else None


__all__ = ["RemoteAiarConfig", "remote_aiar_config", "RemoteAiarCorpusAdapter",
           "active_remote_adapter"]
