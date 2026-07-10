"""Helpers for proxying data-plane routes to the active residency sidecar."""
from __future__ import annotations

from typing import Any

import httpx
from fastapi import HTTPException

_REMOTE_TIMEOUT_S = 20.0


def active_remote_base() -> tuple[str, dict[str, str]] | None:
    """Return the active remote sidecar base URL, if residency is remote."""
    try:
        from errorta_residency import config as residency_config
    except Exception:
        return None

    try:
        state = residency_config.load()
    except Exception:
        return None

    if state.mode == "ssh-remote":
        port = state.local_tunnel_port
        if not port:
            raise HTTPException(
                status_code=503,
                detail="SSH-remote mode is selected but no local tunnel port is available.",
            )
        return f"http://127.0.0.1:{port}", {}

    if state.mode == "cloud":
        raise HTTPException(
            status_code=501,
            detail="Cloud data-residency mode is not enabled until token auth ships.",
        )

    return None


def refuse_local_dataplane_if_remote(path: str) -> None:
    """Fail-closed for a LOCAL-disk data-plane path under remote residency.

    F086 Slice E. A route that materializes corpora/briefs on local disk
    (corpus upload/ingest, brief collect, bundle import, …) would, under remote
    residency, silently leave the data on the laptop while the judge runs
    remotely — a residency-promise violation. Until such a path is fully proxied
    to the remote sidecar (multipart/SSE proxying is the heavy follow-up), refuse
    it explicitly with a structured 409 rather than execute locally.

    No-op in local mode. ``active_remote_base`` already raises 501 (cloud) / 503
    (ssh-remote without a tunnel) — those are also refusals.
    """
    remote = active_remote_base()
    if remote is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "residency_unsupported_path",
                "path": path,
                "message": (
                    "This action writes local data and is not available in "
                    "remote data-residency mode yet."
                ),
            },
        )


def proxy_json_if_remote(
    method: str,
    path: str,
    *,
    json_body: Any | None = None,
    timeout_s: float = _REMOTE_TIMEOUT_S,
) -> dict[str, Any] | None:
    """Proxy a JSON request to the active sidecar, or return None in local mode."""
    remote = active_remote_base()
    if remote is None:
        return None

    base, headers = remote
    url = f"{base}{path}"
    try:
        with httpx.Client(timeout=timeout_s) as client:
            response = client.request(method, url, headers=headers, json=json_body)
    except (httpx.HTTPError, OSError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Active residency sidecar is unreachable for {path}: {exc}",
        ) from exc

    if not 200 <= response.status_code < 300:
        try:
            detail: Any = response.json()
        except ValueError:
            detail = response.text
        raise HTTPException(status_code=response.status_code, detail=detail)

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Active residency sidecar returned non-JSON response for {path}.",
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=502,
            detail=f"Active residency sidecar returned malformed response for {path}.",
        )
    return payload
