"""F009-01 — Service API pairing + token management."""

from __future__ import annotations

import os
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from errorta_app.auth import audit, pairing, store

router = APIRouter(prefix="/api/auth", tags=["auth"])


class PairRequest(BaseModel):
    app_slug: str
    app_name: str
    requested_corpora: list[str] = Field(default_factory=list)
    requested_scopes: list[str] = Field(default_factory=lambda: ["prompt", "meta"])


class PairResponse(BaseModel):
    session_id: str
    status: Literal["pending"]
    expires_at: str


class PairApproveRequest(BaseModel):
    corpora: list[str] | None = None
    scopes: list[str] | None = None


class TokenMetadata(BaseModel):
    id: str
    app_slug: str
    app_name: str
    corpora: list[str]
    scopes: list[str]
    issued_at: str
    last_used_at: Optional[str] = None


class TokenListResponse(BaseModel):
    tokens: list[TokenMetadata]


class TokenDeleteResponse(BaseModel):
    id: str
    status: Literal["revoked"]


def _client_host(request: Request) -> str:
    return (request.client.host if request.client else "").strip().lower()


def _require_loopback(request: Request) -> None:
    allowed = {"127.0.0.1", "::1", "localhost"}
    # Starlette's TestClient reports host "testclient"; accept it ONLY under
    # pytest so the frozen production sidecar never honors a test affordance in
    # this owner-only guard. (Not exploitable — request.client.host is set by the
    # server from the real socket — but defence-in-depth shouldn't ship test hosts.)
    if "PYTEST_CURRENT_TEST" in os.environ:
        allowed.add("testclient")
    host = _client_host(request)
    if host not in allowed:
        raise HTTPException(status_code=403, detail="loopback required")


def _require_tauri_origin(request: Request) -> None:
    origin = request.headers.get("x-errorta-origin", "").lower()
    if origin != "tauri-ui":
        raise HTTPException(status_code=403, detail="tauri origin required")


def _require_owner_request(request: Request) -> None:
    _require_loopback(request)
    _require_tauri_origin(request)


def _source_key(request: Request) -> str:
    return _client_host(request) or "unknown"


@router.post("/pair", response_model=PairResponse)
def pair(req: PairRequest, request: Request) -> PairResponse:
    try:
        session = pairing.start_pairing(
            app_slug=req.app_slug,
            app_name=req.app_name,
            requested_corpora=req.requested_corpora,
            requested_scopes=req.requested_scopes,
            source=_source_key(request),
        )
    except pairing.PairingError as exc:
        status = 429 if exc.code == "pairing_rate_limited" else 400
        headers = {}
        if exc.code == "pairing_rate_limited":
            headers["Retry-After"] = str(max(1, int(exc.meta.get("retry_after") or 1)))
        raise HTTPException(status_code=status, detail=exc.code, headers=headers) from exc
    return PairResponse(
        session_id=str(session["session_id"]),
        status="pending",
        expires_at=str(session["expires_at"]),
    )


@router.get("/pair-status/{session_id}")
def pair_status(session_id: str, request: Request) -> dict:
    try:
        return pairing.poll_status(session_id, source=_source_key(request))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown pairing session") from exc
    except pairing.PairingError as exc:
        status = 429 if exc.code == "pairing_rate_limited" else 400
        headers = {}
        if exc.code == "pairing_rate_limited":
            headers["Retry-After"] = str(max(1, int(exc.meta.get("retry_after") or 1)))
        raise HTTPException(status_code=status, detail=exc.code, headers=headers) from exc


@router.get("/pairs")
def list_pairing_requests(request: Request) -> dict:
    _require_owner_request(request)
    return {"pairs": pairing.list_public_sessions()}


@router.post("/pair/{session_id}/approve")
def approve_pairing(
    session_id: str,
    body: PairApproveRequest,
    request: Request,
) -> dict:
    _require_owner_request(request)
    try:
        return {"pairing": pairing.approve_pairing(
            session_id,
            corpora=body.corpora,
            scopes=body.scopes,
        )}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown pairing session") from exc
    except pairing.PairingError as exc:
        raise HTTPException(status_code=409, detail=exc.code) from exc


@router.post("/pair/{session_id}/deny")
def deny_pairing(session_id: str, request: Request) -> dict:
    _require_owner_request(request)
    try:
        return {"pairing": pairing.deny_pairing(session_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown pairing session") from exc


@router.get("/tokens", response_model=TokenListResponse)
def list_tokens(request: Request) -> TokenListResponse:
    _require_owner_request(request)
    return TokenListResponse(tokens=[TokenMetadata(**item) for item in store.list_public_tokens()])


@router.delete("/tokens/{token_id}", response_model=TokenDeleteResponse)
def delete_token(token_id: str, request: Request) -> TokenDeleteResponse:
    _require_owner_request(request)
    try:
        record = store.revoke_token(token_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="token_not_found") from exc
    audit.record_event(
        "token.revoked",
        token_id=token_id,
        app_slug=record.get("app_slug"),
        by="user",
    )
    return TokenDeleteResponse(id=token_id, status="revoked")
