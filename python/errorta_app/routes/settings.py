"""Sidecar settings routes."""
from __future__ import annotations

import os
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from errorta_aiar_connection.config import (
    AiarConnectionConfig,
    load_canonical,
    save_canonical,
)
from errorta_app import settings as app_settings
from errorta_mobile import config as mobile_config
from errorta_mobile import devices as mobile_devices
from errorta_mobile import netif as mobile_netif
from errorta_mobile import pairing as mobile_pairing
from errorta_project_grounding import remote_config as remote_aiar_config

router = APIRouter(tags=["settings"])

MobileBindMode = Literal["disabled", "loopback_dev", "lan", "tailscale", "explicit_host"]
MobileNetwork = Literal["lan", "tailscale"]


class LogLevelRequest(BaseModel):
    level: Literal["info", "debug"]


class ToolsSettingsRequest(BaseModel):
    searxng_url: str | None = None


class ModelFamiliesSettingsRequest(BaseModel):
    families: list[str] | None = None


class MobileConnectorSettingsRequest(BaseModel):
    enabled: bool | None = None
    bind_mode: MobileBindMode | None = None
    explicit_host: str | None = None
    lan_bind_address: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    require_tls: bool | None = None
    pairing_enabled: bool | None = None
    pairing_pin_required: bool | None = None
    allowed_networks: list[MobileNetwork] | None = None
    max_event_streams: int | None = Field(default=None, ge=1, le=32)
    # F071 — also bind/advertise the Tailscale IP for off-LAN reach.
    also_tailscale: bool | None = None
    tailscale_bind_address: str | None = None


class MobilePairingStartRequest(BaseModel):
    desktop_name: str = "Errorta Desktop"
    ttl_seconds: int = Field(default=300, ge=30, le=300)


class MobileCapabilitiesRequest(BaseModel):
    capabilities: dict[str, bool]


class RemoteAiarSettingsRequest(BaseModel):
    base_url: str | None = None
    tunnel_port: int | None = Field(default=None, ge=1, le=65535)
    token: str | None = None
    timeout_s: float | None = Field(default=None, gt=0, le=600)
    verify: bool | None = None
    clear: bool = False
    clear_token: bool = False
    # F089 managed-tunnel mode (Errorta owns the ssh -N -L to the host alias).
    ssh_host: str | None = None
    remote_host: str | None = None
    remote_port: int | None = Field(default=None, ge=1, le=65535)
    ssh_port: int | None = Field(default=None, ge=1, le=65535)
    ssh_username: str | None = None
    ssh_key_path: str | None = None
    auto_start: bool | None = None


def _require_tauri_origin(request: Request) -> None:
    origin = request.headers.get("x-errorta-origin", "").lower()
    if origin != "tauri-ui":
        raise HTTPException(status_code=403, detail="tauri origin required")


def _tools_settings_response(saved: dict[str, str]) -> dict[str, Any]:
    env_url = os.environ.get("ERRORTA_SEARXNG_URL", "").strip()
    return {
        "searxng_url": saved.get("searxng_url", ""),
        "configured": bool(saved.get("searxng_url") or env_url),
        "env_configured": bool(env_url),
    }


def _validate_optional_url(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return ""
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(
            status_code=422,
            detail="searxng_url must be an http(s) URL",
        )
    return cleaned


@router.get("/settings")
def get_settings() -> dict[str, str]:
    return app_settings.load()


@router.put("/settings/log-level")
def put_log_level(body: LogLevelRequest) -> dict[str, str]:
    current = app_settings.load()
    current["log_level"] = app_settings.apply_log_level(body.level)
    return app_settings.save(current)


@router.get("/settings/tools")
def get_tools_settings(request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    return _tools_settings_response(app_settings.get_tools_settings())


@router.put("/settings/tools")
def put_tools_settings(
    body: ToolsSettingsRequest,
    request: Request,
) -> dict[str, Any]:
    _require_tauri_origin(request)
    saved = app_settings.update_tools_settings(
        searxng_url=_validate_optional_url(body.searxng_url),
    )
    return _tools_settings_response(saved)


def _configured_model_families() -> list[str]:
    from errorta_app import provider_keys
    from errorta_app.routes.gateway import _provider_configured
    from errorta_model_gateway.providers import async_registry

    async_registry.ensure_bootstrapped()
    keys = provider_keys.load_all()
    return sorted(
        cls for cls in async_registry.list_provider_classes()
        if _provider_configured(cls, keys)
    )


def _model_families_response() -> dict[str, Any]:
    configured = _configured_model_families()
    explicit = app_settings.get_model_family_allowlist()
    effective = configured if explicit is None else [f for f in explicit if f in configured]
    return {
        "configured": configured,
        "allowlist": explicit,
        "effective": effective,
        "derived": explicit is None,
    }


@router.get("/settings/model-families")
def get_model_families(request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    return _model_families_response()


@router.put("/settings/model-families")
def put_model_families(
    body: ModelFamiliesSettingsRequest,
    request: Request,
) -> dict[str, Any]:
    _require_tauri_origin(request)
    app_settings.set_model_family_allowlist(body.families)
    return _model_families_response()


@router.get("/settings/remote-aiar")
def get_remote_aiar_settings(request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    out = remote_aiar_config.masked()
    if not out.get("configured"):
        canonical = load_canonical()
        if canonical is not None and canonical.kind == "aiar-service":
            out.update(
                {
                    "configured": True,
                    "managed": False,
                    "base_url": canonical.base_url,
                    "timeout_s": canonical.timeout_s,
                    "verify": canonical.verify_tls,
                    "token_configured": bool(canonical.token),
                    "token_preview": "..." if canonical.token else None,
                    "updated_at": canonical.updated_at,
                    "canonical": True,
                }
            )
    return out


@router.put("/settings/remote-aiar")
def put_remote_aiar_settings(
    body: RemoteAiarSettingsRequest,
    request: Request,
) -> dict[str, Any]:
    _require_tauri_origin(request)
    if body.clear:
        save_canonical(
            AiarConnectionConfig(kind="disconnected", created_from="settings/remote-aiar")
        )
        return remote_aiar_config.masked(remote_aiar_config.clear())
    try:
        existing = remote_aiar_config.load_raw()
        token = body.token if body.token is not None else existing.token
        if not token and not body.clear_token:
            raise ValueError("token is required")
        saved = remote_aiar_config.update(
            base_url=body.base_url,
            tunnel_port=body.tunnel_port,
            token=body.token,
            timeout_s=body.timeout_s,
            verify=body.verify,
            clear_token=body.clear_token,
            ssh_host=body.ssh_host,
            remote_host=body.remote_host,
            remote_port=body.remote_port,
            ssh_port=body.ssh_port,
            ssh_username=body.ssh_username,
            ssh_key_path=body.ssh_key_path,
            auto_start=body.auto_start,
        )
        # F089: if now in managed mode, eagerly bring the tunnel up so the UI
        # reflects live state immediately after Save (non-blocking).
        canonical_base_url = saved.base_url
        if saved.managed:
            try:
                from errorta_tunnels import tunnel_manager as _tunnels

                spec = remote_aiar_config.tunnel_spec(saved)
                if spec is not None:
                    local_port = _tunnels.ensure(spec, wait=False)
                    if local_port:
                        canonical_base_url = f"http://127.0.0.1:{local_port}"
            except Exception:  # noqa: BLE001 - surfaced via tunnel state, not here
                pass
        save_canonical(
            AiarConnectionConfig(
                kind="aiar-service",
                display_name=saved.ssh_host or None,
                base_url=canonical_base_url,
                token=saved.token,
                timeout_s=saved.timeout_s,
                verify_tls=saved.verify,
                created_from="settings/remote-aiar",
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return remote_aiar_config.masked(saved)


@router.post("/settings/remote-aiar/tunnel/reconnect")
def reconnect_remote_aiar_tunnel(request: Request) -> dict[str, Any]:
    """F089: force the managed SSH tunnel to reconnect now (operator 'kick it')."""
    _require_tauri_origin(request)
    state = remote_aiar_config.load_raw()
    if not state.managed:
        raise HTTPException(status_code=409, detail="remote AIAR is not in managed-tunnel mode")
    from errorta_tunnels import tunnel_manager as _tunnels

    spec = remote_aiar_config.tunnel_spec(state)
    if spec is None or not _tunnels.reconnect(spec):
        # No live tunnel yet -> bring it up.
        if spec is not None:
            _tunnels.ensure(spec, wait=False)
    return remote_aiar_config.masked(state)


@router.get("/settings/mobile-connector")
def get_mobile_connector_settings() -> dict[str, Any]:
    out = mobile_config.desktop_settings()
    out["devices"] = mobile_devices.list_public()
    from errorta_app import mobile_lifecycle

    lan = mobile_lifecycle.status()
    if lan.get("running"):
        lan["cert_sha256"] = mobile_pairing.current_cert_fingerprint()
    out["lan_listener"] = lan
    return out


@router.get("/settings/mobile-connector/lan-addresses")
def get_mobile_lan_addresses(request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    return {"addresses": mobile_netif.lan_ipv4_candidates()}


@router.put("/settings/mobile-connector")
def put_mobile_connector_settings(
    body: MobileConnectorSettingsRequest,
    request: Request,
) -> dict[str, Any]:
    _require_tauri_origin(request)
    current = mobile_config.load()
    updates = body.model_dump(exclude_unset=True)
    if updates.get("pairing_pin_required") is False:
        effective_bind = updates.get("bind_mode", current.get("bind_mode"))
        if effective_bind not in {"disabled", "loopback_dev"}:
            raise HTTPException(
                status_code=400,
                detail="mobile_pairing_pin_required_for_non_loopback",
            )
    current.update(updates)
    try:
        saved = mobile_config.save(current)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # F065: bring the LAN listener up/down to match (generates the TLS cert on
    # enable). Surface the listener status + cert fingerprint so the desktop UI
    # can show the operator the value to confirm on the phone.
    from errorta_app import mobile_lifecycle

    lan = mobile_lifecycle.sync(saved)
    out = mobile_config.desktop_settings(saved)
    out["lan_listener"] = lan
    return out


@router.post("/settings/mobile-connector/pairing/start")
def start_mobile_pairing(
    body: MobilePairingStartRequest,
    request: Request,
) -> dict[str, Any]:
    _require_tauri_origin(request)
    try:
        return mobile_pairing.start_pairing(
            desktop_name=body.desktop_name,
            ttl_seconds=body.ttl_seconds,
        )
    except mobile_pairing.PairingError as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc


@router.post("/settings/mobile-connector/pairing/{session_id}/cancel")
def cancel_mobile_pairing(session_id: str, request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    try:
        return {"pairing": mobile_pairing.cancel_pairing(session_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="pairing_session_not_found") from exc


@router.get("/settings/mobile-connector/pairing/{session_id}")
def get_mobile_pairing_status(session_id: str, request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    try:
        return {"pairing": mobile_pairing.get_public(session_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="pairing_session_not_found") from exc


# F065: desktop owner-confirmation. A phone's complete_pairing leaves the
# session awaiting_approval; the operator approves/denies HERE, on the loopback
# sidecar (Tauri origin) — NEVER on the LAN listener (which mounts only
# /mobile/v1/*). This is the gate that stops a LAN peer from self-pairing.
@router.post("/settings/mobile-connector/pairing/{session_id}/approve")
def approve_mobile_pairing(session_id: str, request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    try:
        return {"pairing": mobile_pairing.approve_pairing(session_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="pairing_session_not_found") from exc
    except mobile_pairing.PairingError as exc:
        raise HTTPException(status_code=409, detail=exc.code) from exc


@router.post("/settings/mobile-connector/pairing/{session_id}/deny")
def deny_mobile_pairing(session_id: str, request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    try:
        return {"pairing": mobile_pairing.deny_pairing(session_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="pairing_session_not_found") from exc


@router.get("/settings/mobile-connector/devices")
def list_mobile_devices() -> dict[str, Any]:
    return {"devices": mobile_devices.list_public()}


@router.patch("/settings/mobile-connector/devices/{device_id}")
def update_mobile_device_capabilities(
    device_id: str,
    body: MobileCapabilitiesRequest,
    request: Request,
) -> dict[str, Any]:
    _require_tauri_origin(request)
    try:
        record = mobile_devices.update_capabilities(device_id, body.capabilities)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="mobile_device_not_found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"device": mobile_devices.public_projection(record)}


@router.post("/settings/mobile-connector/devices/{device_id}/revoke")
def revoke_mobile_device(device_id: str, request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    try:
        record = mobile_devices.revoke(device_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="mobile_device_not_found") from exc
    return {"device": mobile_devices.public_projection(record)}


@router.delete("/settings/mobile-connector/devices/{device_id}")
def delete_mobile_device(device_id: str, request: Request) -> dict[str, str]:
    """Forget a paired device entirely (drops the record from the list, not just
    a revoked tombstone). Tauri origin only."""
    _require_tauri_origin(request)
    try:
        mobile_devices.delete(device_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="mobile_device_not_found") from exc
    return {"device_id": device_id, "deleted": "true"}
