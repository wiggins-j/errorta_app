"""F116 - AIAR connection authority routes."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from errorta_aiar_connection.config import (
    AiarConnectionConfig,
    load_canonical,
    save_canonical,
)
from errorta_aiar_connection.resolver import resolve_aiar_runtime
from errorta_aiar_connection.status import (
    probe_aiar_service,
    probe_local_aiar,
    probe_remote_sidecar,
)

router = APIRouter(prefix="/aiar", tags=["aiar"])


class ConnectionRequest(BaseModel):
    kind: Literal["local-aiar", "aiar-service", "errorta-sidecar-remote", "disconnected"]
    display_name: str | None = None
    base_url: str | None = None
    token: str | None = None
    timeout_s: float = Field(default=60.0, gt=0, le=600)
    verify_tls: bool = True
    preferred_model: str | None = None
    allow_disconnected: bool = False


class ModelRequest(BaseModel):
    model: str | None = None


def _require_tauri_origin(request: Request) -> None:
    # tauri-ui ONLY (stricter than the shared cli+tauri-ui guard). R3:
    # additionally validate the per-sidecar bearer token (origin policy
    # unchanged; token auth layered on).
    origin = request.headers.get("x-errorta-origin", "").lower()
    if origin != "tauri-ui":
        raise HTTPException(status_code=403, detail="tauri origin required")
    from errorta_app.origin import validate_sidecar_token

    validate_sidecar_token(request)


def _config_from_request(
    body: ConnectionRequest,
    *,
    created_from: str,
    preserve_token: str | None = None,
) -> AiarConnectionConfig:
    return AiarConnectionConfig(
        kind=body.kind,
        display_name=body.display_name,
        base_url=body.base_url,
        token=body.token if body.token is not None else preserve_token,
        timeout_s=body.timeout_s,
        verify_tls=body.verify_tls,
        preferred_model=body.preferred_model,
        created_from=created_from,
    )


def _token_to_preserve(body: ConnectionRequest) -> str | None:
    if body.token is not None:
        return None
    try:
        runtime = resolve_aiar_runtime()
    except Exception:
        return None
    if runtime.kind != body.kind:
        return None
    current = (runtime.base_url or "").rstrip("/")
    incoming = (body.base_url or "").rstrip("/")
    if current and incoming and current == incoming:
        return runtime.token
    return None


def _probe_config(config: AiarConnectionConfig) -> dict[str, Any]:
    if config.kind == "aiar-service":
        return probe_aiar_service(config, config_source="test").to_public_dict()
    if config.kind == "errorta-sidecar-remote":
        return probe_remote_sidecar(config, config_source="test").to_public_dict()
    if config.kind == "local-aiar":
        return probe_local_aiar(config_source="test").to_public_dict()
    return {
        "kind": "disconnected",
        "runtime_kind": "disconnected",
        "display_name": "AIAR disconnected",
        "connected": False,
        "capabilities": {},
        "config_source": "test",
    }


@router.get("/connection")
def get_connection() -> dict[str, Any]:
    config = load_canonical()
    if config is None:
        return {
            "configured": False,
            "canonical": None,
            "status": resolve_aiar_runtime().to_public_dict(),
        }
    return {
        "configured": True,
        "canonical": config.to_public_dict(),
        "status": resolve_aiar_runtime().to_public_dict(),
    }


@router.put("/connection")
def put_connection(body: ConnectionRequest, request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    config = _config_from_request(
        body,
        created_from="aiar/connection",
        preserve_token=_token_to_preserve(body),
    )
    if config.kind != "disconnected" or body.allow_disconnected:
        status = _probe_config(config)
        if (
            config.kind in {"aiar-service", "errorta-sidecar-remote"}
            and not status.get("connected")
        ):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": status.get("error_code") or "connection_test_failed",
                    "message": status.get("error_message") or "AIAR connection test failed.",
                    "status": status,
                },
            )
    else:
        raise HTTPException(status_code=400, detail="allow_disconnected required")
    try:
        saved = save_canonical(config)
    except ValueError as exc:
        # e.g. a malformed base_url on a non-probed kind (local-aiar/disconnected)
        # that the probe gate above didn't validate — a client error, not a 500.
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "configured": True,
        "canonical": saved.to_public_dict(),
        "status": resolve_aiar_runtime().to_public_dict(),
    }


@router.post("/connection/test")
def test_connection(body: ConnectionRequest, request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    return _probe_config(_config_from_request(body, created_from="test"))


@router.get("/status")
def get_status() -> dict[str, Any]:
    return resolve_aiar_runtime().to_public_dict()


@router.get("/capabilities")
def get_capabilities() -> dict[str, Any]:
    runtime = resolve_aiar_runtime()
    return {
        "runtime_kind": runtime.kind,
        "connected": runtime.connected,
        "capabilities": runtime.capabilities.to_dict(),
        "backend_id": runtime.backend_id,
    }


@router.get("/models")
def get_models() -> dict[str, Any]:
    runtime = resolve_aiar_runtime()
    return {
        "runtime_kind": runtime.kind,
        "connected": runtime.connected,
        "active_model": runtime.active_model,
        "active_model_ready": runtime.active_model_ready,
        "models": list(runtime.available_models),
        "capability": runtime.capabilities.model_catalog,
    }


@router.put("/model")
def put_model(body: ModelRequest, request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    runtime = resolve_aiar_runtime()
    if not runtime.capabilities.model_set_active:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "model_set_unsupported",
                "message": (
                    "The active AIAR backend does not advertise runtime model "
                    "selection. Change the model on that backend."
                ),
                "runtime_kind": runtime.kind,
                "active_model": runtime.active_model,
            },
        )
    raise HTTPException(status_code=501, detail="model setter is not implemented for this backend")
