"""Thin ``httpx`` client over the Errorta sidecar (F147 spec §4.1).

The load-bearing invariant: **every** request carries the static origin header
``x-errorta-origin: tauri-ui``. That header is the *only* guard on coding /
gateway mutations (``coding.py:_require_tauri_origin`` → 403 if absent); there is
no token and no crypto. Reads don't need it, but sending it universally is
simplest and harmless.

HTTP status + known sidecar error bodies are mapped to the typed exceptions in
``errors.py`` so the command layer never sees a raw ``httpx`` response and CI
gets stable exit codes.
"""
from __future__ import annotations

from typing import Any

import httpx

from .errors import (
    AlphaLocked,
    CliError,
    LockBusy,
    NotFound,
    OriginDenied,
    PreflightFailed,
    ResidencyRefused,
    SetupRequired,
    SidecarUnreachable,
)

# The header the desktop app sends and the sidecar checks.
ORIGIN_HEADER = "x-errorta-origin"
ORIGIN_VALUE = "tauri-ui"

_DEFAULT_TIMEOUT = 30.0


class SidecarClient:
    """A minimal request helper bound to one sidecar base URL.

    Callers use :meth:`get_json` / :meth:`post_json` / :meth:`put_json` /
    :meth:`delete_json`; every call attaches the origin header and translates
    error responses into typed :class:`~errorta_cli.errors.CliError` subclasses.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # A dedicated httpx.Client so callers can close it; ``transport`` lets
        # tests inject httpx.MockTransport without a real socket.
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            headers={ORIGIN_HEADER: ORIGIN_VALUE},
        )

    # -- context management --------------------------------------------------
    def __enter__(self) -> SidecarClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # -- core request --------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Issue a request with the origin header; map errors to CliError.

        The header is set on the client *and* re-asserted per request so it can
        never be dropped by a caller passing custom headers later.
        """
        try:
            resp = self._http.request(
                method,
                path,
                json=json,
                params=params,
                headers={ORIGIN_HEADER: ORIGIN_VALUE},
            )
        except httpx.HTTPError as exc:
            raise SidecarUnreachable(
                f"could not reach the sidecar at {self.base_url}: {exc}"
            ) from exc
        _raise_for_status(resp)
        return resp

    def get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return _json_body(self.request("GET", path, params=params))

    def post_json(
        self,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return _json_body(self.request("POST", path, json=json, params=params))

    def put_json(
        self,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return _json_body(self.request("PUT", path, json=json, params=params))

    def delete_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return _json_body(self.request("DELETE", path, params=params))


def _json_body(resp: httpx.Response) -> Any:
    """Return the parsed JSON body, or ``None`` for an empty 2xx response."""
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        return resp.text


def _detail(resp: httpx.Response) -> Any:
    """Extract FastAPI's ``detail`` (str or dict), falling back to raw text."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text
    if isinstance(body, dict) and "detail" in body:
        return body["detail"]
    return body


def _raise_for_status(resp: httpx.Response) -> None:
    """Translate a non-2xx sidecar response into a typed CliError."""
    if resp.is_success:
        return
    status = resp.status_code
    detail = _detail(resp)
    code, message = _classify(detail)

    if status == 403:
        if code == "alpha_locked":
            raise AlphaLocked(message or "alpha build is locked", code=code)
        raise OriginDenied(
            message or "origin not authorized", code=code or "origin_not_authorized"
        )
    if status == 404:
        raise NotFound(message or "not found", code=code)
    if status == 409:
        if code in ("residency_unsupported_path",):
            raise ResidencyRefused(message or "action unavailable in remote mode", code=code)
        if code == "member_health_preflight_failed":
            # coding.py:2291 — carry the unhealthy provider list for rendering.
            unhealthy = detail.get("unhealthy") if isinstance(detail, dict) else None
            raise PreflightFailed(
                message or "a provider isn't ready", code=code, unhealthy=unhealthy
            )
        if code == "run_setup_required":
            # coding.py:2237 — the readiness gate hasn't been confirmed.
            raise SetupRequired(
                message or "run setup hasn't been confirmed", code=code
            )
        # The run-lock 409 ("a run is already in progress") + the resume/continue
        # run-state 409s ("run is not recoverable" / "run is not continuable" /
        # "workspace_integrity_failed") — all run-state conflicts. The real detail
        # string (message) is preserved so the rendered error is specific.
        raise LockBusy(message or "a run is already in progress", code=code)
    if status in (501, 503):
        # active_remote_base() raises these for cloud / ssh-remote-without-tunnel.
        raise ResidencyRefused(
            message or "the remote data plane is not reachable", code=code
        )
    raise CliError(f"sidecar returned {status}: {message or detail!r}", code=code)


def _classify(detail: Any) -> tuple[str | None, str | None]:
    """Pull ``(code, message)`` out of a FastAPI ``detail`` payload.

    Handles both string details (``"origin_not_authorized"``) and the structured
    dict details (``{"code": ..., "message": ...}`` / ``{"error": ...}``).
    """
    if isinstance(detail, str):
        return None, detail
    if isinstance(detail, dict):
        code = detail.get("code") or detail.get("error")
        message = detail.get("message") or detail.get("reason")
        return (str(code) if code else None), (str(message) if message else None)
    return None, None
