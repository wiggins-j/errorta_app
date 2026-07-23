"""Thin ``httpx`` client over the Errorta sidecar (F147 spec §4.1).

The load-bearing invariant: **every** request carries the static origin header
``x-errorta-origin: cli``. The sidecar's origin allowlist trusts both
``tauri-ui`` (the desktop webview) and ``cli`` equally (F147 S9a
``errorta_app.origin``) — both are loopback-only, so this is not a privilege
change; sending ``cli`` simply makes a CLI-initiated mutation *distinguishable in
audit/logs* from a GUI one now that a GUI and the CLI can co-drive one shared
sidecar (S9b). Reads don't need the header, but sending it universally is
simplest and harmless.

R3: the origin header is no longer sufficient on its own for a mutation (any
local process can spoof it). Every request additionally carries
``Authorization: Bearer <token>`` when a per-sidecar token is available (read
from the 0600 ``sidecar-token`` file at handle-resolution time). The sidecar
runs a grace mode during alpha: a valid bearer OR a trusted origin with no
bearer is accepted; a *present but invalid* bearer is rejected 403. So an old
CLI (no token) still works against a new sidecar, and a new CLI still works
against an old sidecar (which ignores the header).

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

# The origin header the sidecar checks. The desktop app sends ``tauri-ui``; the
# CLI sends ``cli`` (both are trusted, loopback-only mutation origins — S9a).
ORIGIN_HEADER = "x-errorta-origin"
ORIGIN_VALUE = "cli"

# R3 — the per-sidecar bearer token. The origin header alone no longer proves a
# mutation came from a trusted local front-end (any process can spoof it); the
# CLI additionally presents ``Authorization: Bearer <token>`` read from the 0600
# ``sidecar-token`` file. Omitted when no token is available (an old sidecar / a
# desktop-spawned one), and the sidecar's grace mode still accepts origin-only.
AUTH_HEADER = "authorization"

_DEFAULT_TIMEOUT = 30.0


def _auth_headers(token: str | None) -> dict[str, str]:
    """The per-request auth headers: always the origin, plus the bearer token
    when one is available."""
    headers = {ORIGIN_HEADER: ORIGIN_VALUE}
    if token:
        headers[AUTH_HEADER] = f"Bearer {token}"
    return headers


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
        token: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # R3: the per-sidecar bearer token (read from the 0600 token file by the
        # caller). None → origin-only (grace-mode compatible with old sidecars).
        self._token = token
        # A dedicated httpx.Client so callers can close it; ``transport`` lets
        # tests inject httpx.MockTransport without a real socket.
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            headers=_auth_headers(token),
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
        timeout: float | None = None,
    ) -> httpx.Response:
        """Issue a request with the origin header; map errors to CliError.

        The header is set on the client *and* re-asserted per request so it can
        never be dropped by a caller passing custom headers later. ``timeout``
        overrides the client default for this one call — needed for the
        synchronous ``pm-ask`` turn, which can run far longer than the 30s
        default (the sidecar waits up to ~120s for the PM model).
        """
        # httpx uses a sentinel for "no per-request override"; only pass timeout
        # when the caller set one, so the client default applies otherwise.
        extra: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
        try:
            resp = self._http.request(
                method,
                path,
                json=json,
                params=params,
                headers=_auth_headers(self._token),
                **extra,
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
        timeout: float | None = None,
    ) -> Any:
        return _json_body(
            self.request("POST", path, json=json, params=params, timeout=timeout)
        )

    def put_json(
        self,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return _json_body(self.request("PUT", path, json=json, params=params))

    def patch_json(
        self,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return _json_body(self.request("PATCH", path, json=json, params=params))

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
