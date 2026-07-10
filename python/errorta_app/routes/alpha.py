"""F-DIST-01 — local sidecar routes driving the alpha activation/lock UI.

These are the *local* endpoints the Tauri webview calls. They never expose the
license private key (there isn't one on this side) and never leak anything the
check-in service wouldn't already know. Activation is a mutation, so it requires
the Tauri origin header (same guard as the settings routes); status is read-only
and unguarded so the frontend hook can poll it freely.

The actual egress to ``api.errorta.app`` happens inside ``errorta_alpha.client``
— this module only orchestrates it and reports state.
"""
from __future__ import annotations

import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from errorta_alpha import client as alpha_client
from errorta_alpha import config as alpha_config
from errorta_alpha import state as alpha_state
from errorta_alpha import telemetry as alpha_telemetry

router = APIRouter(prefix="/alpha", tags=["alpha"])

# Transient store: a prepared feedback bundle awaiting the tester's explicit
# confirm. Keyed by an opaque id; the path is popped + the file deleted on submit
# so the "show exactly what will be sent" bytes are the bytes that get sent.
# Guarded by a lock: FastAPI runs sync routes on a threadpool, so concurrent
# preview/submit calls must not race the eviction/insert.
_PREPARED_FEEDBACK: dict[str, dict] = {}
_PREPARED_FEEDBACK_CAP = 8
_PREPARED_FEEDBACK_LOCK = threading.Lock()


def _require_tauri_origin(request: Request) -> None:
    origin = request.headers.get("x-errorta-origin", "").lower()
    if origin != "tauri-ui":
        raise HTTPException(status_code=403, detail="tauri origin required")


class ActivateRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)


@router.get("/status")
def status() -> dict:
    """Current alpha state (drives the activation screen / lock screen / chrome
    gating). Read-only; safe to poll."""
    return alpha_state.current_status().to_dict()


@router.post("/activate")
def activate(body: ActivateRequest, request: Request) -> dict:
    """Redeem an invite code against the check-in service and persist the license.

    On success returns the refreshed status. On a rejected code, surfaces the
    service's error code (``code_not_found`` / ``code_exhausted`` /
    ``code_disabled`` / ``code_expired`` / ``device_code_mismatch``) or
    ``offline`` as a 400 so the UI can render honest copy.
    """
    _require_tauri_origin(request)
    if not alpha_config.gate_enabled():
        raise HTTPException(status_code=409, detail="alpha gate is not enabled in this build")

    result = alpha_client.activate(body.code)
    if not result.ok:
        raise HTTPException(
            status_code=400,
            detail={"error": result.error_code or "activation_failed", "message": result.message},
        )
    return alpha_state.current_status().to_dict()


class TelemetryPut(BaseModel):
    extras_enabled: bool


@router.get("/telemetry")
def get_telemetry() -> dict:
    """Consent state for the Settings → Alpha telemetry panel."""
    return {
        "gate_enabled": alpha_config.gate_enabled(),
        "extras_enabled": alpha_telemetry.extras_enabled(),
    }


@router.put("/telemetry")
def put_telemetry(body: TelemetryPut, request: Request) -> dict:
    """Turn the Tier-2 extras on/off. The disclosed Tier-1 floor is not
    opt-out-able while enrolled (spec §9), so only ``extras_enabled`` is settable."""
    _require_tauri_origin(request)
    alpha_telemetry.set_extras_enabled(body.extras_enabled)
    return {"extras_enabled": alpha_telemetry.extras_enabled()}


@router.get("/telemetry/inspect")
def inspect_telemetry() -> dict:
    """The "see exactly what we send" payload: the pending floor deltas + the
    tail of the extras queue, verbatim. Nothing is ever sent that isn't visible
    here first."""
    return alpha_telemetry.inspector_snapshot()


class FeedbackPreviewRequest(BaseModel):
    kind: str = Field(default="bug", max_length=32)
    message: str = Field(default="", max_length=8000)


@router.post("/feedback/preview")
def feedback_preview(body: FeedbackPreviewRequest, request: Request) -> dict:
    """Build the redacted feedback bundle and return its manifest so the tester
    can review EXACTLY what will be sent before confirming. Reachable regardless
    of lock state — a locked/unactivated tester can still tell us what's wrong."""
    _require_tauri_origin(request)
    if not alpha_config.gate_enabled():
        raise HTTPException(status_code=409, detail="alpha gate is not enabled in this build")
    from errorta_alpha import feedback as alpha_feedback

    log_buffer = getattr(request.app.state, "log_buffer", None)
    result = alpha_feedback.prepare_feedback_bundle(user_note=body.message, log_buffer=log_buffer)

    prepared_id = uuid.uuid4().hex
    with _PREPARED_FEEDBACK_LOCK:
        # Bound the transient store; drop the OLDEST prepared bundle(s) + their
        # files. dicts preserve insertion order, so the first key is the oldest.
        while len(_PREPARED_FEEDBACK) >= _PREPARED_FEEDBACK_CAP:
            oldest = next(iter(_PREPARED_FEEDBACK))
            stale = _PREPARED_FEEDBACK.pop(oldest)
            Path(str(stale.get("path", ""))).unlink(missing_ok=True)
        _PREPARED_FEEDBACK[prepared_id] = {
            "path": str(result["path"]),
            "kind": body.kind,
            "message": body.message,
        }
    return {
        "prepared_id": prepared_id,
        "kind": body.kind,
        "message": body.message,
        "bundle": {
            "sha256": result.get("sha256"),
            "files": result.get("files"),
            "redaction": result.get("redaction_manifest"),
        },
    }


class FeedbackSubmitRequest(BaseModel):
    prepared_id: str = Field(min_length=1, max_length=64)


@router.post("/feedback/submit")
def feedback_submit(body: FeedbackSubmitRequest, request: Request) -> dict:
    """Send a previously-previewed feedback bundle. The prepared bundle is popped
    and its file deleted so the previewed bytes are exactly the sent bytes."""
    _require_tauri_origin(request)
    if not alpha_config.gate_enabled():
        raise HTTPException(status_code=409, detail="alpha gate is not enabled in this build")
    with _PREPARED_FEEDBACK_LOCK:
        prep = _PREPARED_FEEDBACK.pop(body.prepared_id, None)
    if prep is None:
        raise HTTPException(status_code=404, detail="unknown or expired prepared feedback")
    result = alpha_client.send_feedback(
        kind=prep["kind"], message=prep["message"], bundle_path=prep["path"]
    )
    Path(str(prep["path"])).unlink(missing_ok=True)
    if not result.ok:
        raise HTTPException(status_code=502, detail={"error": result.error or "send_failed"})
    return {"ticket_id": result.ticket_id}
