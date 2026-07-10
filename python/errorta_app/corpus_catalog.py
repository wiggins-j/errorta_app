"""F095: the single residency-aware corpus catalog.

ONE place answers "what corpora exist?" for every surface — Knowledge -> Corpus,
the Council room editor, the Coding Team grounding dropdown, and onboarding. Each
entry carries a ``source`` and a normalized ``status`` so all pickers render
identically.

Backend precedence is delegated to F116's AIAR connection authority:

  1. **AIAR service** — corpora are AIAR instances on the selected server.
  2. **remote Errorta sidecar** — data plane lives on a remote sidecar; local
     reads fail closed until product-route proxying is available for this route.
  3. **local AIAR / disconnected** — on-disk corpora under
     ``~/.errorta/corpora/``.

This lives at the **app layer** (not ``errorta_corpus``) on purpose: the residency
guard + proxy are HTTP concerns and ``active_remote_adapter`` is in
``errorta_project_grounding`` — ``errorta_corpus`` is lower-level and must import
neither. Routes delegate here so the local-vs-remote decision is made once.

NOTE (F095/F096/F116): F095's catalog shape remains intact; F116 supplies the
backend decision so Knowledge, Council, Coding, Judge, and diagnostics agree on
which AIAR runtime is active. ``resolve_corpus_backend`` plus
``/healthz.retrieval_coordinated`` report whether listing and retrieval resolve
to the same backend.
"""
from __future__ import annotations

from typing import Any

LOCAL_CAPABILITIES = {
    "list_files": True,
    "upload_files": True,
    "folder_watch": True,
    "refresh_preview": True,
    "remote_ingest": False,
}

REMOTE_SUMMARY_CAPABILITIES = {
    "list_files": False,
    "upload_files": False,
    "folder_watch": False,
    "refresh_preview": False,
    "remote_ingest": False,
}


def _status_local(file_count: int, ready_count: int) -> str:
    if file_count <= 0:
        return "empty"
    if ready_count >= file_count:
        return "ready"
    return "indexing"


def _normalize_local(c: Any) -> dict[str, Any]:
    fc = int(getattr(c, "file_count", 0) or 0)
    rc = int(getattr(c, "ready_count", 0) or 0)
    return {
        "name": str(getattr(c, "name", "") or ""),
        "file_count": fc,
        "ready_count": rc,
        "status": _status_local(fc, rc),
        "source": "local",
        "unit": "files",
        "capabilities": dict(LOCAL_CAPABILITIES),
    }


def _normalize_remote(inst: dict[str, Any]) -> dict[str, Any]:
    name = str(inst.get("name") or inst.get("display_name") or "")
    try:
        chunks = int(inst.get("chunk_count") or 0)
    except (TypeError, ValueError):
        chunks = 0
    published = bool(inst.get("published"))
    if chunks <= 0:
        status = "empty"
    elif published:
        status = "ready"
    else:
        status = "indexing"
    return {
        "name": name,
        # The remote AIAR tracks chunks, not files; surface chunk_count as the
        # unit so the picker shows a meaningful "ready/total" instead of 0/0
        # (the raw instance dict has no file_count/ready_count keys).
        "file_count": chunks,
        "ready_count": chunks if published else 0,
        "status": status,
        "source": "remote",
        "unit": "chunks",
        "capabilities": dict(REMOTE_SUMMARY_CAPABILITIES),
    }


def resolve_corpus_backend() -> dict[str, Any]:
    """Resolve which backend the corpus catalog reads from, without listing.

    Returns ``{"kind": "remote_aiar"|"residency_remote"|"local", "detail": ...}``.
    ``detail`` is a redaction-safe descriptor (never a token).
    """
    try:
        from errorta_query.backend import resolve_aiar_backend

        backend = resolve_aiar_backend()
    except Exception:  # pragma: no cover — defensive
        backend = None
    if backend is not None:
        if backend.catalog_kind == "remote_aiar":
            return {
                "kind": "remote_aiar",
                "detail": {
                    "base_url": backend.catalog_base_url,
                    "backend_id": backend.catalog_base_url,
                },
            }
        if backend.catalog_kind in {"ssh-remote", "cloud"}:
            return {
                "kind": "residency_remote",
                "detail": {
                    "mode": backend.catalog_kind,
                    "base_url": backend.catalog_base_url,
                    "backend_id": backend.catalog_base_url,
                },
            }

    return {"kind": "local", "detail": {}}


def list_all_corpora() -> dict[str, Any]:
    """The residency-aware corpus catalog.

    Returns ``{"corpora": [{name, file_count, ready_count, status, source}],
    "source": "local"|"remote"}``. Fail-safe: a remote-AIAR listing failure
    yields an empty list (``list_instances`` already swallows transport errors),
    never a 5xx, so every picker degrades gracefully.
    """
    from errorta_project_grounding.remote_adapter import active_remote_adapter

    remote = active_remote_adapter()
    if remote is not None:
        instances = remote.list_instances()  # fail-safe to [] on transport error
        return {
            "corpora": [_normalize_remote(i) for i in instances],
            "source": "remote",
        }

    # Residency-remote: a local-disk read would be misleading, so fail closed
    # (mirroring the existing coding/grounding hardening). No-op under local
    # residency. Proxying the remote catalog instead is a deliberate follow-up.
    from errorta_app.routes._residency_proxy import refuse_local_dataplane_if_remote

    refuse_local_dataplane_if_remote("/corpora")
    from errorta_corpus.listing import list_corpora

    return {
        "corpora": [_normalize_local(c) for c in list_corpora()],
        "source": "local",
    }
