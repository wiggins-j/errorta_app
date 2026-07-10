"""Mobile companion API facade.

F056 exposes health/version publicly and keeps every data/control route locked
behind the future F057 paired-device guard.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from errorta_app import __version__ as app_version
from errorta_council import paths as council_paths
from errorta_council.control import RunControl, TerminalRunError
from errorta_council.room_store import RoomNotFound, RoomStore
from errorta_council.run_store import (
    RunNotFound,
    RunStore,
    TerminalRunRejected,
    WriterAlreadyHeld,
)
from errorta_council.schema import (
    NON_TERMINAL_RUN_STATUSES,
    EventStatus,
    EventType,
)
from errorta_policy import (
    PendingDecisionConflict,
    PendingDecisionNotFound,
    PendingDecisionStore,
)

from . import MOBILE_API_VERSION
from . import activity as mobile_activity
from . import auth as mobile_auth
from . import config as mobile_config
from . import devices as mobile_devices
from . import inbox as mobile_inbox
from . import pairing as mobile_pairing
from .coding_projection import (
    CodingProjectNotFound,
    activity_projection,
    board_projection,
    pr_projection,
    project_detail,
    project_summaries,
    test_run_projection,
)
from .projections import (
    attention_run_projection,
    event_projections,
    pending_decision_projection,
    run_projection,
)

router = APIRouter(prefix="/mobile/v1", tags=["mobile"])


class PairingCompleteRequest(BaseModel):
    pairing_token: str = Field(min_length=1)
    # Optional: None when TLS isn't in use (e.g. loopback dev). When the
    # session has a cert fingerprint, it must match.
    tls_cert_sha256: str | None = None
    display_name: str = "iPhone"
    platform: str = "ios"
    public_key: str = Field(min_length=1)


class PairingStatusRequest(BaseModel):
    session_id: str = Field(min_length=1)
    pairing_token: str = Field(min_length=1)


class PairingVerifyPinRequest(BaseModel):
    session_id: str = Field(min_length=1)
    pairing_token: str = Field(min_length=1)
    pin: str = Field(min_length=1, max_length=32)


class DecisionResolutionRequest(BaseModel):
    client_request_id: str | None = None
    decision_revision: int | None = None
    confirmation: bool | None = None
    reason: str | None = Field(default=None, max_length=280)


class InboxItemCreateRequest(BaseModel):
    kind: Literal["url", "text"]
    text: str = Field(min_length=1)
    title: str | None = None
    source_app: str | None = None


class MobileRunCreateRequest(BaseModel):
    prompt: str | None = Field(default=None, max_length=20_000)
    room_id: str | None = None
    corpus_ids: list[str] = Field(default_factory=list)
    source_inbox_item_id: str | None = None
    client_request_id: str | None = None
    dry_fake_members: bool = False


class MobileFollowUpRequest(BaseModel):
    message: str | None = Field(default=None, max_length=20_000)
    source_inbox_item_id: str | None = None
    client_request_id: str | None = None


class MobileCancelRequest(BaseModel):
    reason: str | None = Field(default="Cancelled from mobile.", max_length=280)
    client_request_id: str | None = None


@router.get("/health")
def health() -> dict[str, Any]:
    cfg = mobile_config.load()
    enabled = bool(cfg.get("enabled")) and cfg.get("bind_mode") != "disabled"
    return {
        "ok": True,
        "status": "ready" if enabled else "disabled",
        "mobile_api_version": MOBILE_API_VERSION,
        "mobile_connector": mobile_config.public_status(cfg),
    }


@router.get("/version")
def version() -> dict[str, Any]:
    return {
        "app_version": app_version,
        "mobile_api_version": MOBILE_API_VERSION,
        "min_supported_mobile_api_version": 1,
    }


@router.get("/capabilities")
def capabilities(request: Request) -> dict[str, Any]:
    device = mobile_auth.require_paired_device(request)
    return {
        "device_id": device.get("device_id"),
        "capabilities": dict(device.get("capabilities") or {}),
    }


@router.get("/connection-info")
def connection_info(request: Request) -> dict[str, Any]:
    """F076 — the desktop's CURRENT reachable hosts (LAN + Tailscale, if enabled)
    + the pinned cert fingerprint. The phone calls this on each connect and
    refreshes its stored host list, so enabling Tailscale later is picked up
    WITHOUT re-pairing (the cert is stable) — pair once, roam LAN↔Tailscale."""
    mobile_auth.require_paired_device(request)
    cfg = mobile_config.load()
    return {
        "hosts": mobile_pairing._host_candidates(cfg),
        "port": int(cfg.get("port") or 8788),
        "cert_sha256": mobile_pairing.current_cert_fingerprint(),
    }


@router.get("/runs")
def runs(
    request: Request,
    status: Literal["active", "recent"] = "active",
) -> dict[str, Any]:
    mobile_auth.require_capability(request, "read_runs")
    store = RunStore(runs_dir=council_paths.runs_dir())
    summaries = []
    for run_id in store.list_run_ids():
        meta, events = store.read_run(run_id)
        if status == "active" and meta.status not in NON_TERMINAL_RUN_STATUSES:
            continue
        summaries.append(run_projection(meta, events))
        if len(summaries) >= 50:
            break
    return {"runs": summaries}


@router.get("/rooms")
def rooms(request: Request) -> dict[str, Any]:
    """Rooms available for starting a new run from the phone.

    Mobile can select among desktop-authored rooms, but it cannot create or
    mutate them. Gate on start_runs because this list is only used to launch
    new prompts.
    """
    mobile_auth.require_capability(request, "start_runs")
    store = RoomStore(
        rooms_dir=council_paths.rooms_dir(),
        deleted_dir=council_paths.deleted_rooms_dir(),
    )
    return {
        "rooms": [
            {
                "room_id": room.id,
                "name": room.name,
                "status_hint": room.status_hint,
                "updated_at": room.updated_at,
                "revision": room.revision,
            }
            for room in store.list()
        ]
    }


@router.get("/coding-projects")
def coding_projects(request: Request) -> dict[str, Any]:
    mobile_auth.require_capability(request, "read_coding_projects")
    return {"projects": project_summaries()}


@router.get("/coding-projects/{project_id}")
def coding_project_detail(project_id: str, request: Request) -> dict[str, Any]:
    mobile_auth.require_capability(request, "read_coding_projects")
    return {"project": _mobile_coding_project(project_id, project_detail)}


@router.get("/coding-projects/{project_id}/board")
def coding_project_board(project_id: str, request: Request) -> dict[str, Any]:
    mobile_auth.require_capability(request, "read_coding_projects")
    return _mobile_coding_project(project_id, board_projection)


@router.get("/coding-projects/{project_id}/prs")
def coding_project_prs(
    project_id: str,
    request: Request,
    limit: int = 100,
) -> dict[str, Any]:
    mobile_auth.require_capability(request, "read_coding_projects")
    return _mobile_coding_project(project_id, lambda store: pr_projection(store, limit=limit))


@router.get("/coding-projects/{project_id}/test-runs")
def coding_project_test_runs(
    project_id: str,
    request: Request,
    limit: int = 100,
) -> dict[str, Any]:
    mobile_auth.require_capability(request, "read_coding_projects")
    return _mobile_coding_project(project_id, lambda store: test_run_projection(store, limit=limit))


@router.get("/coding-projects/{project_id}/activity")
def coding_project_activity(
    project_id: str,
    request: Request,
    limit: int = 100,
) -> dict[str, Any]:
    mobile_auth.require_capability(request, "read_coding_activity")
    return _mobile_coding_project(project_id, lambda store: activity_projection(store, limit=limit))


@router.post("/runs")
async def create_mobile_run(
    body: MobileRunCreateRequest,
    request: Request,
) -> dict[str, Any]:
    device = mobile_auth.require_capability(request, "start_runs")
    device_id = str(device.get("device_id"))
    prompt = _message_from_body_or_inbox(
        device_id=device_id,
        explicit_text=body.prompt,
        source_inbox_item_id=body.source_inbox_item_id,
    )
    room_id = _resolve_room_id(body.room_id)
    from errorta_app.routes import council as council_routes

    try:
        result = await council_routes.create_run(
            council_routes._CreateRun(
                room_id=room_id,
                prompt=prompt,
                corpus_ids=body.corpus_ids,
                conversation_id=None,
                conversation_turn_id=body.client_request_id,
                dry_fake_members=body.dry_fake_members,
            )
        )
    except HTTPException:
        raise
    except RoomNotFound as exc:
        raise HTTPException(status_code=404, detail="mobile_room_not_found") from exc
    if body.source_inbox_item_id:
        mobile_inbox.archive(
            device_id=device_id,
            inbox_item_id=body.source_inbox_item_id,
        )
    run_obj = result.get("run") or {}
    started_run_id = str(run_obj.get("run_id") or run_obj.get("id") or "")
    if started_run_id:
        mobile_activity.record(started_run_id, "start")  # F074 desktop auto-surface
    return {
        "run": result.get("run"),
        "events": result.get("events", []),
        "client_request_id": body.client_request_id,
        "source_inbox_item_id": body.source_inbox_item_id,
    }


@router.get("/runs/{run_id}")
def run_detail(run_id: str, request: Request) -> dict[str, Any]:
    mobile_auth.require_capability(request, "read_runs")
    store = RunStore(runs_dir=council_paths.runs_dir())
    try:
        meta, events = store.read_run(run_id)
    except RunNotFound as exc:
        raise HTTPException(status_code=404, detail="mobile_run_not_found") from exc
    return {"run": run_projection(meta, events)}


@router.get("/runs/{run_id}/events")
def run_events(
    run_id: str,
    request: Request,
    after_sequence: int = 0,
    max_events: int = 100,
) -> dict[str, Any]:
    mobile_auth.require_capability(request, "read_runs")
    store = RunStore(runs_dir=council_paths.runs_dir())
    try:
        meta, events = store.read_run(run_id)
    except RunNotFound as exc:
        raise HTTPException(status_code=404, detail="mobile_run_not_found") from exc
    filtered = event_projections(
        events,
        after_sequence=max(0, after_sequence),
        max_events=max(1, min(max_events, 500)),
    )
    return {
        "run": run_projection(meta, events),
        "events": filtered,
        "last_sequence": events[-1].sequence if events else 0,
    }


@router.get("/runs/{run_id}/events/stream")
def run_events_stream(
    run_id: str,
    request: Request,
    after_sequence: int = 0,
) -> StreamingResponse:
    mobile_auth.require_capability(request, "read_runs")
    store = RunStore(runs_dir=council_paths.runs_dir())
    try:
        store.read_run(run_id)
    except RunNotFound as exc:
        raise HTTPException(status_code=404, detail="mobile_run_not_found") from exc

    async def _stream():
        cursor = max(0, after_sequence)
        idle_polls = 0
        while idle_polls < 30:
            try:
                meta, events = store.read_run(run_id)
            except RunNotFound:
                yield _sse({"type": "error", "detail": "mobile_run_not_found"})
                return
            projected = event_projections(events, after_sequence=cursor, max_events=100)
            if projected:
                cursor = events[-1].sequence if events else cursor
                idle_polls = 0
                yield _sse(
                    {
                        "type": "events",
                        "run": run_projection(meta, events),
                        "events": projected,
                        "last_sequence": cursor,
                    }
                )
                if meta.status not in NON_TERMINAL_RUN_STATUSES:
                    return
            else:
                idle_polls += 1
                yield ": keepalive\n\n"
            await asyncio.sleep(0.25)

    return StreamingResponse(_stream(), media_type="text/event-stream")


@router.post("/runs/{run_id}/messages")
async def send_run_message(
    run_id: str,
    body: MobileFollowUpRequest,
    request: Request,
) -> dict[str, Any]:
    device = mobile_auth.require_capability(request, "send_messages")
    device_id = str(device.get("device_id"))
    message = _message_from_body_or_inbox(
        device_id=device_id,
        explicit_text=body.message,
        source_inbox_item_id=body.source_inbox_item_id,
    )
    store = RunStore(runs_dir=council_paths.runs_dir())
    try:
        store.read_run(run_id)
    except RunNotFound as exc:
        raise HTTPException(status_code=404, detail="mobile_run_not_found") from exc
    # A mobile follow-up IS a live user interjection (F049): route it through the
    # SAME RunControl.submit_interjection mechanism the desktop uses, so the next
    # council member actually picks it up. (A bespoke MOBILE_MESSAGE event would
    # never reach the context router and would silently do nothing.)
    control = RunControl(run_store=store, run_id=run_id)
    try:
        _meta, event = await control.submit_interjection(
            text=message, requested_by=f"mobile_device:{device_id}",
        )
    except TerminalRunError as exc:
        raise HTTPException(status_code=409, detail="mobile_run_terminal") from exc
    except ValueError as exc:  # empty_interjection_text
        raise HTTPException(status_code=422, detail="mobile_message_required") from exc
    if body.source_inbox_item_id:
        mobile_inbox.archive(
            device_id=device_id,
            inbox_item_id=body.source_inbox_item_id,
        )
    mobile_activity.record(run_id, "message")  # F074 desktop auto-surface
    return {
        "accepted": True,
        "event": event.to_dict() if event is not None else None,
        "client_request_id": body.client_request_id,
        "source_inbox_item_id": body.source_inbox_item_id,
    }


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    request: Request,
    body: MobileCancelRequest | None = None,
) -> dict[str, Any]:
    device = mobile_auth.require_capability(request, "cancel_runs")
    reason = (body.reason if body else None) or "Cancelled from mobile."
    control = RunControl(
        run_id=run_id,
        run_store=RunStore(runs_dir=council_paths.runs_dir()),
    )
    try:
        meta, event = await control.request_cancel(
            requested_by=f"mobile_device:{device.get('device_id')}",
            reason=reason,
        )
    except RunNotFound as exc:
        raise HTTPException(status_code=404, detail="mobile_run_not_found") from exc
    except TerminalRunError as exc:
        raise HTTPException(status_code=409, detail="mobile_run_terminal") from exc
    return {
        "run": meta.to_dict(),
        "event": event.to_dict() if event is not None else None,
        "client_request_id": body.client_request_id if body else None,
    }


@router.get("/pending-decisions")
def pending_decisions(
    request: Request,
    run_id: str | None = None,
) -> dict[str, Any]:
    device = mobile_auth.require_paired_device(request)
    runs = RunStore(runs_dir=council_paths.runs_dir())
    store = PendingDecisionStore(runs_dir=runs.runs_dir)
    run_ids = [run_id] if run_id else runs.list_run_ids()
    decisions = []
    for rid in run_ids:
        try:
            if run_id:
                runs.read_run(rid)
            records = store.list(rid, state="pending")
        except RunNotFound as exc:
            raise HTTPException(status_code=404, detail="mobile_run_not_found") from exc
        for record in records:
            decisions.append(pending_decision_projection(record, device=device))
    return {"decisions": decisions}


@router.get("/attention")
def attention(request: Request) -> dict[str, Any]:
    mobile_auth.require_capability(request, "read_runs")
    runs = RunStore(runs_dir=council_paths.runs_dir())
    decisions = PendingDecisionStore(runs_dir=runs.runs_dir)
    items = []
    total_pending_decisions = 0
    for run_id in runs.list_run_ids():
        meta, _events = runs.read_run(run_id)
        pending_count = len(decisions.list(run_id, state="pending"))
        total_pending_decisions += pending_count
        projected = attention_run_projection(
            meta,
            pending_decision_count=pending_count,
        )
        if projected is not None:
            items.append(projected)
    return {
        "needs_attention": bool(items),
        "attention_count": len(items),
        "pending_decision_count": total_pending_decisions,
        "runs": items,
    }


@router.get("/inbox-items")
def inbox_items(
    request: Request,
    status: Literal["pending", "archived"] | None = "pending",
) -> dict[str, Any]:
    device = mobile_auth.require_paired_device(request)
    return {
        "items": mobile_inbox.list_items(
            device_id=str(device.get("device_id")),
            status=status,
        )
    }


@router.post("/inbox-items")
def create_inbox_item(
    body: InboxItemCreateRequest,
    request: Request,
) -> dict[str, Any]:
    device = mobile_auth.require_capability(request, "send_messages")
    try:
        item = mobile_inbox.create(
            device_id=str(device.get("device_id")),
            kind=body.kind,
            text=body.text,
            title=body.title,
            source_app=body.source_app,
        )
    except mobile_inbox.InboxError as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc
    return {"item": mobile_inbox.public_projection(item)}


@router.post("/pending-decisions/{run_id}/{decision_id}/approve")
def approve_pending_decision(
    run_id: str,
    decision_id: str,
    request: Request,
    body: DecisionResolutionRequest | None = None,
) -> dict[str, Any]:
    return _resolve_pending_decision(
        run_id=run_id,
        decision_id=decision_id,
        request=request,
        body=body,
        action="approve",
    )


@router.post("/pending-decisions/{run_id}/{decision_id}/reject")
def reject_pending_decision(
    run_id: str,
    decision_id: str,
    request: Request,
    body: DecisionResolutionRequest | None = None,
) -> dict[str, Any]:
    return _resolve_pending_decision(
        run_id=run_id,
        decision_id=decision_id,
        request=request,
        body=body,
        action="reject",
    )


@router.post("/pairing/complete")
def complete_pairing(body: PairingCompleteRequest, request: Request) -> dict[str, Any]:
    cfg = mobile_auth.require_enabled()
    if not cfg.get("pairing_enabled"):
        raise HTTPException(status_code=400, detail="mobile_pairing_disabled")
    try:
        # F065: submits the phone's device draft; the desktop must approve
        # before a device/token is issued. Returns {session_id, state,
        # requires_pin}.
        return mobile_pairing.complete_pairing(
            pairing_token=body.pairing_token,
            tls_cert_sha256_value=body.tls_cert_sha256,
            display_name=body.display_name,
            platform=body.platform,
            public_key=body.public_key,
            source=request.client.host if request.client else "unknown",
        )
    except mobile_pairing.PairingError as exc:
        status = 429 if exc.code == "pairing_rate_limited" else 400
        raise HTTPException(status_code=status, detail=exc.code) from exc


@router.post("/pairing/verify-pin")
def verify_pairing_pin(
    body: PairingVerifyPinRequest,
    request: Request,
) -> Any:
    cfg = mobile_auth.require_enabled()
    if not cfg.get("pairing_enabled"):
        raise HTTPException(status_code=400, detail="mobile_pairing_disabled")
    try:
        return mobile_pairing.verify_pin(
            session_id=body.session_id,
            pairing_token=body.pairing_token,
            pin=body.pin,
        )
    except mobile_pairing.PairingError as exc:
        if exc.code == "pairing_pin_mismatch":
            return JSONResponse(
                status_code=401,
                content={
                    "detail": exc.code,
                    "attempts_remaining": int(exc.meta.get("attempts_remaining", 0)),
                },
            )
        if exc.code == "pairing_pin_locked":
            return JSONResponse(status_code=429, content={"detail": exc.code})
        status = {
            "pairing_token_expired": 400,
            "pairing_not_awaiting_approval": 409,
            "pairing_pin_not_required": 409,
            "pairing_session_not_found": 404,
        }.get(exc.code, 400)
        raise HTTPException(status_code=status, detail=exc.code) from exc


@router.post("/pairing/status")
def pairing_status(body: PairingStatusRequest, request: Request) -> dict[str, Any]:
    """The phone polls for its pairing outcome. On the first poll after the
    desktop approves, the session token is returned exactly once."""
    mobile_auth.require_enabled()
    try:
        return mobile_pairing.poll_status(
            session_id=body.session_id,
            pairing_token=body.pairing_token,
            source=request.client.host if request.client else "unknown",
        )
    except mobile_pairing.PairingError as exc:
        status = 429 if exc.code == "pairing_rate_limited" else 400
        raise HTTPException(status_code=status, detail=exc.code) from exc


def _mobile_coding_project(project_id: str, projector: Any) -> dict[str, Any]:
    from errorta_council.coding.ledger import LedgerError, LedgerStore

    try:
        return projector(LedgerStore(project_id))
    except CodingProjectNotFound as exc:
        raise HTTPException(status_code=404, detail="mobile_coding_project_not_found") from exc
    except LedgerError as exc:
        raise HTTPException(status_code=422, detail="mobile_coding_project_invalid") from exc


def _resolve_pending_decision(
    *,
    run_id: str,
    decision_id: str,
    request: Request,
    body: DecisionResolutionRequest | None,
    action: Literal["approve", "reject"],
) -> dict[str, Any]:
    device = mobile_auth.require_paired_device(request)
    runs = RunStore(runs_dir=council_paths.runs_dir())
    try:
        runs.read_run(run_id)
    except RunNotFound as exc:
        raise HTTPException(status_code=404, detail="mobile_run_not_found") from exc
    store = PendingDecisionStore(runs_dir=runs.runs_dir)
    try:
        current = store.get(run_id, decision_id)
    except PendingDecisionNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail="mobile_pending_decision_not_found",
        ) from exc
    current_projection = pending_decision_projection(current, device=device)
    expected_revision = body.decision_revision if body else None
    if expected_revision is not None and expected_revision != current_projection["revision"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "mobile_decision_revision_conflict",
                "decision": current_projection,
            },
        )
    required_capability = current_projection["actions"]["required_capability"]
    try:
        mobile_devices.require_capability(device, required_capability)
    except mobile_devices.DeviceAuthError as exc:
        raise HTTPException(status_code=403, detail=exc.code) from exc
    resolved_by = f"mobile_device:{device.get('device_id')}"
    try:
        if action == "approve":
            updated = store.approve(run_id, decision_id, resolved_by=resolved_by)
        else:
            updated = store.reject(run_id, decision_id, resolved_by=resolved_by)
    except PendingDecisionConflict as exc:
        latest = store.get(run_id, decision_id)
        raise HTTPException(
            status_code=409,
            detail={
                "code": "mobile_pending_decision_conflict",
                "decision": pending_decision_projection(latest, device=device),
            },
        ) from exc
    return {
        "decision": pending_decision_projection(updated, device=device),
        "client_request_id": body.client_request_id if body else None,
    }


def _resolve_room_id(room_id: str | None) -> str:
    store = RoomStore(
        rooms_dir=council_paths.rooms_dir(),
        deleted_dir=council_paths.deleted_rooms_dir(),
    )
    if room_id:
        try:
            store.get(room_id)
        except RoomNotFound as exc:
            raise HTTPException(status_code=404, detail="mobile_room_not_found") from exc
        return room_id
    rooms = store.list()
    if not rooms:
        raise HTTPException(status_code=404, detail="mobile_room_not_found")
    return rooms[0].id


def _message_from_body_or_inbox(
    *,
    device_id: str,
    explicit_text: str | None,
    source_inbox_item_id: str | None,
) -> str:
    text = (explicit_text or "").strip()
    if text:
        return text
    if not source_inbox_item_id:
        raise HTTPException(status_code=422, detail="mobile_message_required")
    item = mobile_inbox.get_item(
        device_id=device_id,
        inbox_item_id=source_inbox_item_id,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="mobile_inbox_item_not_found")
    item_text = str(item.get("text") or "").strip()
    if not item_text:
        raise HTTPException(status_code=422, detail="mobile_message_required")
    return item_text


def _append_mobile_event(
    store: RunStore,
    run_id: str,
    spec: dict[str, Any],
) -> dict[str, Any] | None:
    try:
        token = store.acquire_writer(run_id)
    except WriterAlreadyHeld:
        store.push_pending_control_event(run_id, event_spec=spec)
        return None
    try:
        event = store.append_event(
            run_id,
            type=EventType(spec["type"]),
            status=EventStatus(spec["status"]),
            payload=dict(spec.get("payload") or {}),
            writer=token,
        )
        return event.to_dict()
    except TerminalRunRejected as exc:
        raise HTTPException(status_code=409, detail="mobile_run_terminal") from exc
    finally:
        store.release_writer(token)


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, sort_keys=True)}\n\n"


__all__ = ["router"]
