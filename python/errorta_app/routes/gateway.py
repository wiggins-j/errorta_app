"""F034 — Gateway discovery + provider-keys routes.

Exposed under the existing sidecar at:

- ``GET    /gateway/providers``        — list registered providers
- ``GET    /gateway/routes``           — list all routes from all providers
- ``GET    /gateway/routes?provider=X``— routes for a single provider
- ``GET    /provider-keys``            — masked summary of configured keys
- ``PUT    /provider-keys/anthropic``  — set anthropic key
- ``PUT    /provider-keys/openai``     — set openai key
- ``PUT    /provider-keys/google``     — set google key
- ``DELETE /provider-keys/<fixed>``    — clear a fixed provider key
- ``PUT    /provider-keys/custom``     — upsert a custom entry by alias
- ``DELETE /provider-keys/custom?alias=…`` — clear one custom entry

The F033 room editor consumes ``/gateway/providers`` and
``/gateway/routes`` to populate dropdowns.
"""
from __future__ import annotations

import os
import time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from errorta_app import provider_keys, settings
from errorta_model_gateway.providers import async_registry

router = APIRouter()

# F040-01 — the subscription CLI providers (no key; the vendor CLI owns auth).
_CLI_PROVIDERS = frozenset({"claude_cli", "codex_cli", "cursor_cli"})

# F040-01 — vendor login/install metadata. Errorta computes the argv + URLs; the
# Tauri layer launches them (or the frontend copies the command). No execution
# happens in the backend.
_CLI_LOGIN_META: dict[str, dict[str, Any]] = {
    "claude_cli": {
        # `claude login` is NOT a subcommand — the subscription auth path is
        # `claude setup-token`. The bare name here is only the copy-fallback;
        # `_login_argv_for` substitutes the RESOLVED absolute binary path so the
        # copied command works even when the CLI lives outside the user's shell
        # PATH (e.g. ~/.local/bin, which a GUI .app and many shells don't carry).
        "login_argv": ["claude", "setup-token"],
        "install_url": "https://docs.anthropic.com/en/docs/claude-code/setup",
        "install_command": "npm install -g @anthropic-ai/claude-code",
    },
    "codex_cli": {
        "login_argv": ["codex", "login"],
        "install_url": "https://developers.openai.com/codex/cli/",
        "install_command": "npm install -g @openai/codex",
    },
    "cursor_cli": {
        "login_argv": ["agent", "login"],
        "install_url": "https://cursor.com/cli",
        "install_command": (
            "curl https://cursor.com/install -fsS | bash  "
            "# then 'agent login' (or set CURSOR_API_KEY)"
        ),
    },
}

# F040-01 — process-level cache of the last *live* auth probe per CLI provider.
# Advisory only (a hint for the room/role pickers): the live billable probe runs
# ONLY on the explicit Test route, never on the hot discovery/detect paths.
_PROBE_CACHE: dict[str, dict[str, Any]] = {}


def _require_tauri_origin(request: Request) -> None:
    # F147 S9a: accept ``cli`` alongside ``tauri-ui`` (both loopback-trusted) so a
    # CLI-initiated gateway mutation is distinguishable in logs/audit.
    from errorta_app.origin import require_ui_or_cli_origin

    require_ui_or_cli_origin(request)


def _cli_handler(provider: str):
    """Return the provider module exposing ``resolve_details`` / ``probe_auth``."""
    if provider == "claude_cli":
        from errorta_model_gateway.providers import async_claude_cli

        return async_claude_cli.ClaudeCliHandler()
    if provider == "codex_cli":
        from errorta_model_gateway.providers import async_codex_cli

        return async_codex_cli.CodexCliHandler()
    if provider == "cursor_cli":
        from errorta_model_gateway.providers import async_cursor_cli

        return async_cursor_cli.CursorCliHandler()
    return None


def _cli_status_payload(provider: str) -> dict[str, Any]:
    """Cheap detect (NO billable probe) + the cached live-probe result if any."""
    handler = _cli_handler(provider)
    if handler is None:
        raise HTTPException(status_code=404, detail=f"unknown cli provider: {provider!r}")
    override = settings.get_cli_binary(provider)
    details = handler.resolve_details(override_path=override)
    cached = _PROBE_CACHE.get(provider)
    connected, connected_at = _observed_connection(provider)
    details["connected"] = connected
    details["login"] = (cached or {}).get("login", details.get("login", ""))
    details["verified_at"] = (cached or {}).get("verified_at") or connected_at
    return details


def _observed_connection(provider: str) -> tuple[bool | None, float | None]:
    """Most-recent ``(connected, at)`` across the explicit Test cache and the
    shared observed-connectivity cache.

    The explicit ``_PROBE_CACHE`` records BOTH success and failure (with a
    ``verified_at``); the shared cache records only genuine ``connected``
    observations (from the engine preflight / active use). Whichever signal is
    NEWER wins — so a run that just succeeded shows ``connected`` even after a
    stale failed Test, while a fresh failing Test still overrides an old observed
    success. The shared cache can never falsely assert ``connected`` (positive
    observations only), and it never wins over a later negative Test.
    """
    signals: list[tuple[float, bool]] = []
    probe = _PROBE_CACHE.get(provider)
    if probe is not None:
        signals.append((float(probe.get("verified_at") or 0.0), bool(probe.get("connected"))))
    from errorta_model_gateway import connectivity

    observed_at = connectivity.observed_at(provider)
    if observed_at is not None:
        signals.append((observed_at, True))
    if not signals:
        return (None, None)
    signals.sort(key=lambda s: s[0])
    at, connected = signals[-1]
    return (connected, at or None)


def _cached_connected(provider: str) -> bool | None:
    return _observed_connection(provider)[0]


def _provider_configured(cls: str, keys: dict[str, Any]) -> bool:
    """Whether a provider is ready to select in the room editor.

    - ``local`` / ``fake``: never need a key.
    - ``claude_cli`` / ``codex_cli`` / ``cursor_cli``: subscription CLIs — "configured" when
      their binary is installed (the CLI owns the OAuth, Errorta holds no
      key). This is what makes them selectable instead of greyed out.
    - ``custom``: any custom entry on file.
    - everything else (anthropic/openai/google): an ``api_key`` on file.
    """
    if cls in ("local", "fake"):
        return True
    if cls in _CLI_PROVIDERS:
        # F040-01 — installed-check honoring the persisted binary override (read
        # in the app, passed into the gateway resolver). CHEAP: filesystem
        # resolution only — no `<cli> --version`, no billable model probe.
        override = settings.get_cli_binary(cls)
        if cls == "claude_cli":
            from errorta_model_gateway.providers import async_claude_cli

            return async_claude_cli.is_available(override_path=override)
        if cls == "codex_cli":
            from errorta_model_gateway.providers import async_codex_cli

            return async_codex_cli.is_available(override_path=override)
        from errorta_model_gateway.providers import async_cursor_cli

        return async_cursor_cli.is_available(override_path=override)
    if cls == "custom":
        return bool(keys.get("custom"))
    return bool((keys.get(cls) or {}).get("api_key"))


# ----------------------------------------------------------------------
# Gateway discovery
# ----------------------------------------------------------------------


@router.get("/gateway/providers")
def list_providers() -> dict[str, Any]:
    """Return the registered providers + their configured status.

    Used by the F033 room editor to populate the provider dropdown.
    The order of the response is alphabetical (``async_registry.list_provider_classes``
    sorts).
    """
    async_registry.ensure_bootstrapped()
    classes = async_registry.list_provider_classes()
    keys = provider_keys.load_all()
    out: list[dict[str, Any]] = []
    for cls in classes:
        handler = async_registry.get_handler(cls)
        if handler is None:
            continue
        configured = _provider_configured(cls, keys)
        entry: dict[str, Any] = {
            "provider_class": cls,
            "display_name": handler.display_name,
            "configured": configured,
        }
        # F040-01 — for CLI providers expose the SEPARATE cached `connected`
        # field (null until the user explicitly Tests). `configured` stays the
        # cheap installed-check; this endpoint NEVER runs a live billable probe.
        if cls in _CLI_PROVIDERS:
            entry["connected"] = _cached_connected(cls)
        out.append(entry)
    return {"providers": out}


@router.get("/gateway/routes")
def list_routes(provider: str | None = Query(None)) -> dict[str, Any]:
    """List routes from the registered handlers.

    Without ``provider``: returns every route from every handler. With
    ``provider=anthropic``: returns just that provider's routes (404 if
    unknown).
    """
    async_registry.ensure_bootstrapped()
    keys = provider_keys.load_all()

    def _is_configured(cls: str) -> bool:
        return _provider_configured(cls, keys)

    if provider is not None:
        handler = async_registry.get_handler(provider)
        if handler is None:
            raise HTTPException(status_code=404, detail=f"unknown provider: {provider!r}")
        routes = handler.list_routes(configured=_is_configured(provider))
        return {
            "provider_class": provider,
            "routes": [
                {"route_id": r.route_id, "label": r.label, "family": r.family}
                for r in routes
            ],
        }

    # All providers.
    out: list[dict[str, Any]] = []
    for cls in async_registry.list_provider_classes():
        handler = async_registry.get_handler(cls)
        if handler is None:
            continue
        routes = handler.list_routes(configured=_is_configured(cls))
        for r in routes:
            out.append({
                "route_id": r.route_id,
                "label": r.label,
                "family": r.family,
                "provider_class": cls,
            })
    return {"routes": out}


@router.get("/gateway/model-availability")
def model_availability() -> dict[str, Any]:
    """F129 live, fail-closed route eligibility projection for UI and runtime."""
    from errorta_council.coding.model_availability import resolve_route_availability

    routes = list_routes(None).get("routes", [])
    route_ids = [str(route.get("route_id") or "") for route in routes if route.get("route_id")]
    projection = resolve_route_availability(route_ids)
    return {"routes": [projection[route_id].to_dict() for route_id in route_ids]}


# ----------------------------------------------------------------------
# F040-01 — subscription CLI status / binary override / login metadata
# ----------------------------------------------------------------------


@router.get("/gateway/providers/{provider}/cli-status")
def get_cli_status(provider: str, request: Request) -> dict[str, Any]:
    """3-state CLI detect: provenance + version + cached live-probe result.

    CHEAP — runs ``<cli> --version`` at most, never the billable model probe.
    The live ``connected`` flag (and ``verified_at``) come from the cache that
    only the explicit Test route populates.
    """
    _require_tauri_origin(request)
    return _cli_status_payload(provider)


class _CliBinaryBody(BaseModel):
    path: str = Field(min_length=1)


@router.put("/provider-keys/{provider}/cli-binary")
def put_cli_binary(provider: str, body: _CliBinaryBody, request: Request) -> dict[str, Any]:
    """Persist a CLI binary override (validated as an existing executable file).

    Tauri-origin guarded. The stored path is honored ahead of PATH by the
    gateway resolver (passed in as ``override_path=`` — the gateway never reads
    settings.json).
    """
    _require_tauri_origin(request)
    if provider not in _CLI_PROVIDERS:
        raise HTTPException(status_code=404, detail=f"unknown cli provider: {provider!r}")
    candidate = (body.path or "").strip()
    if not candidate or not os.path.isabs(candidate):
        raise HTTPException(status_code=422, detail="path must be an absolute path")
    if not os.path.isfile(candidate):
        raise HTTPException(status_code=422, detail="path is not an existing file")
    if not os.access(candidate, os.X_OK):
        raise HTTPException(status_code=422, detail="path is not executable")
    try:
        settings.set_cli_binary(provider, candidate)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # A new override may change resolution — drop BOTH the stale explicit-Test
    # probe AND the observed-connectivity signal, so `connected` isn't reported
    # true for a binary path that hasn't itself been verified.
    from errorta_model_gateway import connectivity
    _PROBE_CACHE.pop(provider, None)
    connectivity.clear(provider)
    return _cli_status_payload(provider)


@router.delete("/provider-keys/{provider}/cli-binary")
def delete_cli_binary(provider: str, request: Request) -> dict[str, Any]:
    """Clear a persisted CLI binary override. Tauri-origin guarded."""
    _require_tauri_origin(request)
    if provider not in _CLI_PROVIDERS:
        raise HTTPException(status_code=404, detail=f"unknown cli provider: {provider!r}")
    from errorta_model_gateway import connectivity
    settings.clear_cli_binary(provider)
    _PROBE_CACHE.pop(provider, None)
    connectivity.clear(provider)
    return _cli_status_payload(provider)


@router.get("/provider-keys/{provider}/login-command")
def get_login_command(provider: str, request: Request) -> dict[str, Any]:
    """Vendor login argv + install URL/command for a CLI provider.

    Metadata ONLY — Errorta computes these; the frontend copies the command (or,
    once the launcher capability lands, launches it). No execution here.
    """
    _require_tauri_origin(request)
    meta = _CLI_LOGIN_META.get(provider)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"unknown cli provider: {provider!r}")
    return {
        "login_argv": _login_argv_for(provider, meta),
        "install_url": meta["install_url"],
        "install_command": meta["install_command"],
    }


def _login_argv_for(provider: str, meta: dict[str, Any]) -> list[str]:
    """Resolve the copy-fallback login argv for a CLI provider.

    For ``cursor_cli`` the correct argv depends on the RESOLVED binary: the
    app-bundle ``cursor`` launcher needs ``cursor agent login`` while a direct
    ``agent`` / ``cursor-agent`` install needs ``<bin> login``. We compute it
    from the resolved Cursor command (``argv_prefix`` + ``["login"]``), honoring
    the saved binary override with the same precedence as cli-status/Test, so
    the copied command matches what actually works. Falls back to the static
    ``["agent", "login"]`` only when nothing resolves.

    For ``claude_cli`` / ``codex_cli`` we likewise substitute the RESOLVED
    absolute binary path for the bare name, so the copied command runs even when
    the CLI is installed outside the user's interactive shell PATH (a common
    case: ``claude`` in ``~/.local/bin``, which neither the GUI .app nor a
    default zsh carries). The subcommand stays from ``meta`` (``setup-token`` for
    claude, ``login`` for codex). Falls back to the bare name only when the
    binary can't be resolved.
    """
    if provider == "cursor_cli":
        from errorta_model_gateway.providers import async_cursor_cli

        resolved = async_cursor_cli.resolve_cursor_command_detailed(
            override_path=settings.get_cli_binary(provider)
        )
        if resolved is not None:
            command, _source = resolved
            return [*command.argv_prefix, "login"]
    if provider in ("claude_cli", "codex_cli"):
        argv = list(meta["login_argv"])
        override = settings.get_cli_binary(provider)
        if provider == "claude_cli":
            from errorta_model_gateway.providers.async_claude_cli import (
                resolve_claude_binary,
            )

            binary = override or resolve_claude_binary()
        else:
            from errorta_model_gateway.providers.async_codex_cli import (
                resolve_codex_binary,
            )

            binary = override or resolve_codex_binary()
        if binary:
            argv[0] = binary
        return argv
    return list(meta["login_argv"])


# ----------------------------------------------------------------------
# Provider keys
# ----------------------------------------------------------------------


@router.get("/provider-keys")
def get_provider_keys_masked() -> dict[str, Any]:
    """Return the masked keys summary safe for the Settings UI.

    Raw keys NEVER appear here — only ``"…<last4>"`` previews + a
    ``configured`` flag per provider.
    """
    return provider_keys.mask_all()


class _FixedKeyBody(BaseModel):
    api_key: str = Field(min_length=1)


_FixedProvider = Literal["anthropic", "openai", "google"]


class _CustomEntryBody(BaseModel):
    alias: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    api_style: Literal["openai_chat_completions", "anthropic_messages", "raw"]
    auth_header: str = "Authorization"
    auth_prefix: str = "Bearer "
    model: str | None = None


# IMPORTANT: register /provider-keys/custom BEFORE the dynamic
# /provider-keys/{provider} routes so FastAPI matches the literal
# string first. Otherwise the Literal validation on
# ``_FixedProvider`` would 422 on the string "custom".


@router.put("/provider-keys/custom")
def put_custom_entry(body: _CustomEntryBody, request: Request) -> dict[str, Any]:
    """Upsert one custom-provider entry by alias. Returns masked state."""
    _require_tauri_origin(request)
    entry: dict[str, Any] = {
        "alias": body.alias,
        "base_url": body.base_url,
        "api_key": body.api_key,
        "api_style": body.api_style,
        "auth_header": body.auth_header,
        "auth_prefix": body.auth_prefix,
    }
    if body.model:
        entry["model"] = body.model
    try:
        provider_keys.upsert_custom(entry)  # type: ignore[arg-type]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return provider_keys.mask_all()


@router.delete("/provider-keys/custom")
def delete_custom_entry(request: Request, alias: str = Query(...)) -> dict[str, Any]:
    """Clear one custom entry by alias."""
    _require_tauri_origin(request)
    if not alias:
        raise HTTPException(status_code=422, detail="alias is required")
    provider_keys.clear_custom(alias)
    return provider_keys.mask_all()


# Dynamic-path routes registered AFTER the literal-string `custom`
# routes above (FastAPI matches in registration order; literal wins).


@router.put("/provider-keys/{provider}")
def put_fixed_key(
    provider: _FixedProvider, body: _FixedKeyBody, request: Request
) -> dict[str, Any]:
    """Upsert one of anthropic / openai / google. Returns masked state."""
    _require_tauri_origin(request)
    try:
        provider_keys.upsert_fixed(provider, body.api_key)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return provider_keys.mask_all()


@router.delete("/provider-keys/{provider}")
def delete_fixed_key(provider: _FixedProvider, request: Request) -> dict[str, Any]:
    """Clear one fixed provider's key. Returns masked state."""
    _require_tauri_origin(request)
    try:
        provider_keys.clear_fixed(provider)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return provider_keys.mask_all()


# ----------------------------------------------------------------------
# Test-connection routes
# ----------------------------------------------------------------------


@router.post("/provider-keys/custom/test")
async def test_custom_alias(request: Request, alias: str = Query(...)) -> dict[str, Any]:
    """Probe ONE custom-provider entry. Returns {ok, detail, latency_ms}."""
    _require_tauri_origin(request)
    async_registry.ensure_bootstrapped()
    handler = async_registry.get_handler("custom")
    if handler is None:
        raise HTTPException(status_code=500, detail="custom handler not registered")
    result = await handler.test_alias(alias)  # type: ignore[attr-defined]
    return {"ok": result.ok, "detail": result.detail, "latency_ms": result.latency_ms}


@router.post("/provider-keys/{provider}/test")
async def test_provider_connection(provider: str, request: Request) -> dict[str, Any]:
    """Probe one provider. Returns {ok, detail, latency_ms}.

    For fixed providers (anthropic / openai / google) the key is
    resolved from the keys store. For local + custom the handler
    decides whether/how to authenticate.
    """
    _require_tauri_origin(request)
    async_registry.ensure_bootstrapped()
    handler = async_registry.get_handler(provider)
    if handler is None:
        raise HTTPException(status_code=404, detail=f"unknown provider: {provider!r}")
    api_key: str | None = None
    if provider in provider_keys.FIXED_PROVIDERS:
        api_key = provider_keys.get_fixed_key(provider)
    result = await handler.test_connection(api_key=api_key)
    detail = result.detail
    # F040-01 — this is the ONLY place the billable CLI probe runs. Cache the
    # connected-state so the room/role pickers (and cli-status) can consult it
    # without re-probing. `verified_at` lets the UI show "verified Nm ago". The
    # detail may carry raw CLI stderr, so redact it (defense-in-depth — the
    # credential boundary: no token ever reaches a response).
    if provider in _CLI_PROVIDERS:
        from errorta_model_gateway.providers._cli_common import classify_test_result

        # F120: classify the result so the panel shows the same wording the
        # run-loop uses — a logged-out CLI reads `logged_out` + a one-step
        # remediation, never a bare `claude_cli_failed: exit 1:`. classify_test_result
        # already redacts the detail.
        classified = classify_test_result(result)
        detail = classified["detail"]
        _PROBE_CACHE[provider] = {
            "connected": bool(result.ok),
            "state": classified["state"],
            "login": "",
            "verified_at": time.time(),
        }
        return {
            "ok": result.ok, "detail": detail, "latency_ms": result.latency_ms,
            "state": classified["state"], "remediation": classified["remediation"],
        }
    return {"ok": result.ok, "detail": detail, "latency_ms": result.latency_ms}


__all__ = ["router"]
