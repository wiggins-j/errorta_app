"""F031-01 + F031-02 — Council rooms + runs API.

Surface:
- ``POST /council/rooms`` / ``GET`` / ``PUT`` / ``DELETE``
- ``POST /council/rooms/validate`` (unsaved draft)
- ``POST /council/rooms/{id}/validate`` (saved)
- ``POST /council/rooms/{id}/clone``
- ``POST /council/runs`` (with ``dry_fake_members`` flag)
- ``GET /council/runs`` / ``GET /council/runs/{id}``
- ``GET /council/runs/{id}/events?after_sequence=N``
- ``POST /council/runs/{id}/cancel``

Cancel-against-terminal returns 409 (architecture-spec OQ#2).
"""
from __future__ import annotations

import asyncio
import threading
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from errorta_council import paths as council_paths
from errorta_council.callouts.ids import new_callout_id
from errorta_council.callouts.policy import find_target, resolve_callout_policy
from errorta_council.callouts.queue import CalloutQueue, CalloutRecord
from errorta_council.children import AsyncInbox, ChildRunNotFound, ChildRunStore
from errorta_council.context.citations import CitationRegistry, citation_registry_path
from errorta_council.context.manifest_store import ContextManifestStore
from errorta_council.control import DecisionNotApplicable, RunControl, TerminalRunError
from errorta_council.engine import build_and_run
from errorta_council.fake_run import run_fake_council
from errorta_council.gateway_local import LocalGateway
from errorta_council.gateway_meta import FakeGatewayMeta, RealGatewayMeta
from errorta_council.inspection_audit import (
    build_run_audit_summary,
    build_turn_audit,
)
from errorta_council.limits import SchedulerPolicy
from errorta_council.resources import LocalResourceGuard
from errorta_council.room_store import (
    RevisionConflict,
    RoomNotFound,
    RoomStore,
)
from errorta_council.run_store import (
    RunNotFound,
    RunStore,
)
from errorta_council.schema import (
    TERMINAL_RUN_STATUSES,
    CouncilRoom,
    EventStatus,
    EventType,
    UnsupportedFormatVersion,
)
from errorta_council.steward.packet import build_deterministic_packet
from errorta_council.steward.store import StewardPacketNotFound, StewardPacketStore
from errorta_council.validation import validate_room
from errorta_policy import (
    PendingDecisionConflict,
    PendingDecisionNotFound,
    PendingDecisionStore,
)

router = APIRouter(prefix="/council", tags=["council"])


def _alpha_enforce_not_locked() -> None:
    # Keep the import lazy: errorta_council itself must never depend on the
    # disclosed app-level alpha egress package.
    from errorta_alpha.state import enforce_not_locked

    enforce_not_locked()


class _ModelCatalogPut(BaseModel):
    overrides: dict[str, dict[str, Any]]


def _all_gateway_route_ids() -> list[str]:
    from errorta_app.routes.gateway import list_routes

    routes = list_routes(None).get("routes", [])
    return sorted({str(route.get("route_id")) for route in routes if route.get("route_id")})


def _model_catalog_response() -> dict[str, Any]:
    from errorta_council.coding.model_catalog import (
        catalog_revision,
        load_catalog,
        load_overrides,
    )

    overrides = load_overrides()
    route_ids = sorted(set(_all_gateway_route_ids()).union(overrides))
    catalog = load_catalog(route_ids)
    return {
        "revision": catalog_revision(catalog),
        "entries": [catalog[route_id].to_dict() for route_id in route_ids],
        "overrides": overrides,
    }


@router.get("/model-catalog")
def get_model_catalog(request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    return _model_catalog_response()


@router.put("/model-catalog")
def put_model_catalog(body: _ModelCatalogPut, request: Request) -> dict[str, Any]:
    _require_tauri_origin(request)
    from errorta_council.coding.model_catalog import save_overrides

    try:
        save_overrides(body.overrides)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _model_catalog_response()


@router.get("/mobile-activity")
def mobile_activity() -> dict:
    """F074 — the most recent run a paired phone touched (started or messaged),
    so the desktop Council pane can auto-surface it. `seq` is a monotonic
    counter the client polls against; `run_id` is null when there's been none."""
    from errorta_mobile import activity as mobile_activity_mod

    return mobile_activity_mod.latest()


def _room_store() -> RoomStore:
    return RoomStore(rooms_dir=council_paths.rooms_dir(),
                     deleted_dir=council_paths.deleted_rooms_dir())


def _run_store() -> RunStore:
    return RunStore(runs_dir=council_paths.runs_dir())


def _gateway_meta() -> RealGatewayMeta:
    # F034 (2026-06-12) — RealGatewayMeta now bridges the async-provider
    # registry to recognize anthropic / openai / google / local / custom
    # routes. Falls back to None for unregistered prefixes, which the
    # validator surfaces as ``unknown_gateway_route``.
    return RealGatewayMeta(catalog_version="2026-06-12")


def _tool_gateway():
    """The F039 ToolGateway for a run. Registry-backed; the scheduler still
    gates every tool by tool_policy + F041 consent, so providing it does not
    change behavior for rooms that grant no tools."""
    from errorta_tools.builtins import register_builtins
    from errorta_tools.gateway import DefaultToolGateway

    register_builtins()
    return DefaultToolGateway()


def _require_tauri_origin(request: Request) -> None:
    # tauri-ui ONLY (stricter than the shared cli+tauri-ui guard). R3:
    # additionally validate the per-sidecar bearer token (origin policy
    # unchanged; token auth layered on).
    origin = request.headers.get("x-errorta-origin", "").lower()
    if origin != "tauri-ui":
        raise HTTPException(status_code=403, detail="origin_not_authorized")
    from errorta_app.origin import validate_sidecar_token

    validate_sidecar_token(request)


def _pending_decision_store(runs: RunStore) -> PendingDecisionStore:
    return PendingDecisionStore(runs_dir=runs.runs_dir)


# Kept as a fallback for unit tests that still inject the old fake.
def _fake_gateway_meta() -> FakeGatewayMeta:
    return FakeGatewayMeta(
        known_routes={
            "fake.local.deterministic": {"kind": "local", "priced": False},
        },
        catalog_version="2026-06-11",
    )


def _parse_room_or_422(raw: dict[str, Any]) -> CouncilRoom:
    try:
        return CouncilRoom.from_dict(raw)
    except UnsupportedFormatVersion as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"malformed room: {exc}")


# ---- room endpoints --------------------------------------------------------

@router.post("/rooms")
def create_room(raw: dict[str, Any]) -> dict[str, Any]:
    room = _parse_room_or_422(raw)
    store = _room_store()
    saved = store.create(room)
    return {"room": saved.to_dict(),
            "validation": validate_room(saved, _gateway_meta()).__dict__}


# ---- F047 declarative council profiles ------------------------------------

def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _available_provider_classes() -> set[str]:
    from errorta_app import provider_keys
    from errorta_app.routes.gateway import _provider_configured
    from errorta_model_gateway.providers import async_registry

    async_registry.ensure_bootstrapped()
    keys = provider_keys.load_all()
    return {
        cls for cls in async_registry.list_provider_classes()
        if _provider_configured(cls, keys)
    }


def _available_tool_ids() -> set[str]:
    from errorta_tools import catalog as tool_catalog

    return {m.tool_id for m in tool_catalog.all_metadata() if m.backend == "builtin"}


@router.get("/profiles/examples")
def list_profile_examples() -> dict[str, Any]:
    from errorta_council.profiles import examples, profile_to_yaml

    out = []
    for slug, profile in examples.all_examples().items():
        out.append({"slug": slug, "profile": profile, "yaml": profile_to_yaml(profile)})
    return {"examples": out}


@router.get("/rooms/{room_id}/profile")
def export_room_profile(room_id: str) -> dict[str, Any]:
    from errorta_council.profiles import export_room_to_profile, profile_to_yaml

    store = _room_store()
    try:
        room = store.get(room_id)
    except RoomNotFound:
        raise HTTPException(status_code=404, detail="room not found")
    profile = export_room_to_profile(room.to_dict())
    return {"profile": profile, "yaml": profile_to_yaml(profile)}


class _ProfileImportBody(BaseModel):
    profile: dict[str, Any] | None = None
    yaml: str | None = None


@router.post("/profiles/validate")
def validate_profile(body: _ProfileImportBody) -> dict[str, Any]:
    """Parse + validate a profile and return a DRAFT room (NOT saved) plus a
    validation report (missing providers/tools). Nothing runs or persists."""
    from errorta_council.profiles import import_profile_to_room_draft
    from errorta_council.profiles.schema import ProfileError, parse_profile_yaml

    if body.profile is not None:
        profile = body.profile
    elif body.yaml is not None:
        try:
            profile = parse_profile_yaml(body.yaml)
        except ProfileError as exc:
            raise HTTPException(status_code=422, detail={"code": str(exc)})
    else:
        raise HTTPException(status_code=422, detail={"code": "profile_or_yaml_required"})

    try:
        result = import_profile_to_room_draft(
            profile,
            available_provider_classes=_available_provider_classes(),
            available_tool_ids=_available_tool_ids(),
            now=_now_iso(),
        )
    except ProfileError as exc:
        raise HTTPException(status_code=422, detail={"code": str(exc)})
    return result


@router.get("/rooms")
def list_rooms() -> dict[str, Any]:
    summaries = _room_store().list()
    return {"rooms": [s.__dict__ for s in summaries]}


@router.get("/rooms/{room_id}")
def get_room(room_id: str) -> dict[str, Any]:
    try:
        room = _room_store().get(room_id)
    except RoomNotFound:
        raise HTTPException(status_code=404, detail="room not found")
    return {"room": room.to_dict(),
            "validation": validate_room(room, _gateway_meta()).__dict__}


class _PutRoom(BaseModel):
    expected_revision: int
    room: dict[str, Any]


@router.put("/rooms/{room_id}")
def update_room(room_id: str, body: _PutRoom) -> dict[str, Any]:
    try:
        updated = _room_store().update(
            room_id, expected_revision=body.expected_revision,
            mutate=lambda raw: {**raw, **body.room},
        )
    except RoomNotFound:
        raise HTTPException(status_code=404, detail="room not found")
    except RevisionConflict as exc:
        raise HTTPException(status_code=409,
                            detail={"code": "revision_conflict",
                                    "expected": exc.expected, "actual": exc.actual})
    except (TypeError, ValueError, KeyError) as exc:
        # The merged room failed to deserialize (e.g. a malformed member dict).
        # Return a clean 422 instead of letting it bubble to an unhandled 500:
        # a 500 from the outer error middleware carries NO CORS headers, so the
        # webview blocks the response and the UI shows a misleading
        # "sidecar_unreachable" instead of the real validation failure.
        raise HTTPException(status_code=422,
                            detail={"code": "invalid_room", "error": str(exc)})
    return {"room": updated.to_dict(),
            "validation": validate_room(updated, _gateway_meta()).__dict__}


@router.delete("/rooms/{room_id}")
def delete_room(room_id: str) -> dict[str, Any]:
    try:
        _room_store().delete(room_id)
    except RoomNotFound:
        raise HTTPException(status_code=404, detail="room not found")
    return {"ok": True}


@router.post("/rooms/validate")
def validate_unsaved_room(raw: dict[str, Any]) -> dict[str, Any]:
    room = _parse_room_or_422(raw)
    return validate_room(room, _gateway_meta()).__dict__


@router.post("/rooms/{room_id}/validate")
def validate_saved_room(room_id: str) -> dict[str, Any]:
    try:
        room = _room_store().get(room_id)
    except RoomNotFound:
        raise HTTPException(status_code=404, detail="room not found")
    return validate_room(room, _gateway_meta()).__dict__


class _CloneRoom(BaseModel):
    new_id: str
    new_name: str


@router.post("/rooms/{room_id}/clone")
def clone_room(room_id: str, body: _CloneRoom) -> dict[str, Any]:
    try:
        cloned = _room_store().clone(room_id, new_id=body.new_id,
                                     new_name=body.new_name)
    except RoomNotFound:
        raise HTTPException(status_code=404, detail="room not found")
    return {"room": cloned.to_dict(),
            "validation": validate_room(cloned, _gateway_meta()).__dict__}


# ---- run endpoints ---------------------------------------------------------


class _CreateRun(BaseModel):
    room_id: str
    prompt: str
    corpus_ids: list[str] | None = None
    conversation_id: str | None = None
    conversation_turn_id: str | None = None
    dry_fake_members: bool = False


@router.post("/runs")
async def create_run(body: _CreateRun) -> dict[str, Any]:
    _alpha_enforce_not_locked()
    rooms = _room_store()
    try:
        room = rooms.get(body.room_id)
    except RoomNotFound:
        raise HTTPException(status_code=404, detail="room not found")
    runs = _run_store()
    member_ids = [m.id for m in room.members if m.enabled]
    # QA P1 #2 lock: when the request omits corpus_ids, fall back to whatever
    # the room itself was bound to. Without this fallback, the Phase 5
    # "Seed demo room" affordance silently drops the welcome corpus on every
    # subsequent run, and the inspection drawer reports zero retrieved
    # snippets even though the room had one attached. F095 promotes the
    # room-level binding to a typed ``corpus_ids`` field; ``effective_corpus_ids``
    # still tolerates the legacy ``_extras`` home for pre-F095 rooms.
    room_default_corpus_ids = (
        room.effective_corpus_ids()
        if hasattr(room, "effective_corpus_ids")
        else list((getattr(room, "_extras", {}) or {}).get("corpus_ids") or [])
    )
    effective_corpus_ids = (
        list(body.corpus_ids)
        if body.corpus_ids is not None
        else room_default_corpus_ids
    )
    if body.dry_fake_members:
        # Phase 0 sync path: drive a deterministic fake transcript in-process.
        meta = runs.create_run(
            room_id=room.id,
            room_snapshot={"name": room.name,
                           "topology_kind": room.topology.kind,
                           "member_count": len(room.members),
                           "room_format_version": room.format_version},
            prompt=body.prompt, corpus_ids=list(effective_corpus_ids),
            conversation_id=body.conversation_id,
            conversation_turn_id=body.conversation_turn_id,
        )
        run_fake_council(runs, meta.id, member_ids=member_ids)
        new_meta, events = runs.read_run(meta.id)
        return {"run": new_meta.to_dict(),
                "events": [e.to_dict() for e in events]}

    # ---- Phase 1 readiness gate (P1 — invariant 4: fail closed) ------------
    validation = validate_room(room, _gateway_meta())
    # Only "ready" rooms may launch. blocked_by_policy is a hard rejection too —
    # full_context_not_allowed and remote_member_zero_budget both indicate the
    # room cannot run as configured.
    if validation.status != "ready":
        raise HTTPException(
            status_code=422,
            detail={
                "code": "room_not_runnable",
                "status": validation.status,
                "errors": validation.errors,
            },
        )

    # Phase 1 engine-backed path: snapshot the full room and run in background.
    snapshot = _room_dict_with_provider_hint(room)
    meta = runs.create_run(
        room_id=body.room_id,
        room_snapshot=snapshot,
        prompt=body.prompt,
        corpus_ids=list(effective_corpus_ids),
        conversation_id=body.conversation_id,
        conversation_turn_id=body.conversation_turn_id,
    )
    budget = snapshot.get("budget_policy") or {}
    topology = snapshot.get("topology") or {}
    # validate_room above guarantees both caps are present + positive when
    # status == "ready". Reading them here without a fallback preserves the
    # "caps are explicit" contract from F031-09 — silent normalization is gone.
    max_rounds_val = topology.get("max_rounds") or budget.get("max_rounds")
    max_per_member = (
        topology.get("max_messages_per_member")
        or budget.get("max_messages_per_member")
    )
    if max_rounds_val is None or max_per_member is None:
        # Defensive: if a future code path bypasses validate_room and lands
        # here without caps, fail closed instead of inventing defaults.
        raise HTTPException(
            status_code=422,
            detail={
                "code": "missing_required_caps",
                "errors": [{"path": "$.topology", "code": "missing_required_caps"}],
            },
        )
    # Per-turn timeout: 120s default (was 30s, too tight for big local
    # models like qwen3.5:9b which routinely need 30-60s per turn).
    # ERRORTA_PER_TURN_TIMEOUT_SECONDS env var lets the operator
    # override for very slow models.
    import os as _os
    _per_turn = int(_os.environ.get("ERRORTA_PER_TURN_TIMEOUT_SECONDS") or "120")
    policy = SchedulerPolicy(
        max_rounds=int(max_rounds_val),
        max_messages_per_member=int(max_per_member),
        max_total_member_messages=topology.get("max_total_turns"),
        per_turn_timeout_seconds=_per_turn,
    )

    # Drive the scheduler on a dedicated daemon thread. This lets the
    # background run survive across TestClient request boundaries (each
    # TestClient request uses its own short-lived event loop).
    def _runner() -> None:
        try:
            asyncio.run(
                build_and_run(
                    run_store=runs,
                    run_meta=meta,
                    policy=policy,
                    gateway_meta=LocalGateway(),
                    hardware_scan_present=False,
                    tool_gateway=_tool_gateway(),
                )
            )
        except Exception as exc:
            # P1: do NOT swallow scheduler errors. Emit RUN_FAILED so the run
            # surfaces as terminal in the UI. Best-effort: tolerate the
            # secondary failure mode where the runs dir was removed mid-write
            # (e.g. pytest teardown races).
            try:
                _emit_terminal_failure_from_thread(runs, meta.id, exc)
            except Exception:
                pass
    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    _ACTIVE_SCHEDULER_THREADS.append(t)
    return {"run": meta.to_dict(), "events": []}


def _emit_terminal_failure_from_thread(
    runs: "RunStore", run_id: str, exc: BaseException
) -> None:
    """Emit a terminal run_failed event from a scheduler-thread crash.

    The scheduler thread holds the writer for the lifetime of build_and_run().
    When it raises before reaching its own ``finally`` cleanup, the writer
    may still be reserved; release it so this terminal write can acquire it.
    """
    from dataclasses import replace as _replace

    # Drop any stale writer reservation held by the crashed scheduler.
    from errorta_council.run_store import _WRITERS, _WRITERS_GUARD  # noqa: SLF001
    from errorta_council.run_store import (
        TerminalRunRejected as _TermRejected,
    )
    with _WRITERS_GUARD:
        _WRITERS.pop(runs._writers_key(run_id), None)  # noqa: SLF001

    try:
        token = runs.acquire_writer(run_id)
    except Exception:
        return
    try:
        try:
            runs.append_event(
                run_id,
                type=EventType.RUN_FAILED,
                status=EventStatus.FAILED,
                payload={
                    "reason": "gateway_error",
                    "detail": type(exc).__name__,
                },
                writer=token,
            )
        except _TermRejected:
            # Already terminal — recovery has nothing to do.
            return
    finally:
        runs.release_writer(token)

    # Mirror terminal_reason into the meta projection.
    try:
        meta, _ = runs.read_run(run_id)
        runs.write_meta(_replace(meta, terminal_reason="gateway_error"))
    except Exception:
        pass


_ACTIVE_SCHEDULER_THREADS: list[threading.Thread] = []


def drain_scheduler_threads(timeout: float = 5.0) -> None:
    """Join all background scheduler threads (called from test teardown)."""
    pending = list(_ACTIVE_SCHEDULER_THREADS)
    _ACTIVE_SCHEDULER_THREADS.clear()
    for t in pending:
        t.join(timeout=timeout)


@router.get("/runs")
def list_runs(room_id: str | None = None, status: str | None = None,
              limit: int = 50, offset: int = 0) -> dict[str, Any]:
    summaries = _run_store().list_runs(
        room_id=room_id, status=status, limit=limit, offset=offset,
    )
    return {"runs": [s.__dict__ for s in summaries]}


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    try:
        meta, events = _run_store().read_run(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="run not found")
    return {"run": meta.to_dict(),
            "events": [e.to_dict() for e in events]}


@router.get("/runs/{run_id}/citations")
def get_run_citations(run_id: str) -> dict[str, Any]:
    try:
        _run_store().read_run(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="run not found")
    registry = CitationRegistry(
        path=citation_registry_path(run_id, council_root=council_paths.council_root())
    )
    return {
        "run_id": run_id,
        "citations": [asdict(entry) for entry in registry.list()],
    }


@router.get("/runs/{run_id}/efficiency")
def get_run_efficiency(run_id: str) -> dict[str, Any]:
    try:
        _, events = _run_store().read_run(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="run not found")
    manifests = ContextManifestStore(
        root=council_paths.council_root() / "context-manifests"
    ).list_by_run(run_id)
    inline_sources = 0
    stubbed_sources = 0
    estimated_input_tokens = 0
    cache_read_input_tokens = 0
    cache_write_input_tokens = 0
    digest_messages = 0
    dialect_fallbacks = 0
    compaction_segments = 0
    for manifest in manifests:
        estimate = manifest.get("token_estimate") or {}
        if isinstance(estimate.get("input"), int):
            estimated_input_tokens += estimate["input"]
        for ref in manifest.get("source_refs") or []:
            if not isinstance(ref, dict):
                continue
            if ref.get("packed") == "stub":
                stubbed_sources += 1
            elif ref.get("class_") == "retrieved_snippet":
                inline_sources += 1
        compaction = manifest.get("compaction") or {}
        if isinstance(compaction.get("segments"), list):
            compaction_segments += len(compaction["segments"])
    for event in events:
        payload = event.payload or {}
        if isinstance(payload.get("digest"), dict):
            digest_messages += 1
        if payload.get("dialect_fallback"):
            dialect_fallbacks += 1
        usage = event.usage or {}
        if isinstance(usage.get("cache_read_input_tokens"), int):
            cache_read_input_tokens += usage["cache_read_input_tokens"]
        if isinstance(usage.get("cache_write_input_tokens"), int):
            cache_write_input_tokens += usage["cache_write_input_tokens"]
    return {
        "run_id": run_id,
        "manifest_count": len(manifests),
        "estimated_input_tokens": estimated_input_tokens,
        "inline_sources": inline_sources,
        "stubbed_sources": stubbed_sources,
        "digest_messages": digest_messages,
        "dialect_fallbacks": dialect_fallbacks,
        "compaction_segments": compaction_segments,
        "cache_read_input_tokens": cache_read_input_tokens,
        "cache_write_input_tokens": cache_write_input_tokens,
    }


class _ApplyAcceptBody(BaseModel):
    confirm: bool = False
    allow_conflicts: bool = False


def _apply_workspace_or_404(run_id: str):
    """Resolve the run's auto-apply workspace, or raise the right HTTP error."""
    from errorta_tools.runner.apply_workspace import ApplyWorkspace

    try:
        _run_store().read_run(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="run_not_found")
    aw = ApplyWorkspace(run_id=run_id)
    if not aw.exists():
        raise HTTPException(status_code=404, detail="apply_workspace_not_found")
    return aw


@router.get("/runs/{run_id}/apply-workspace")
def get_apply_workspace(run_id: str) -> dict[str, Any]:
    """Preview the proposed auto-apply patch + conflicts (no writes)."""
    from errorta_tools.runner.apply_workspace import ApplyWorkspaceError

    aw = _apply_workspace_or_404(run_id)
    try:
        return {"run_id": run_id, **aw.merge_back_preview()}
    except ApplyWorkspaceError as exc:
        # Source tree gone / unrecorded -> the patch can't be located.
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/runs/{run_id}/apply-workspace/accept")
def accept_apply_workspace(
    run_id: str, body: _ApplyAcceptBody, request: Request
) -> dict[str, Any]:
    """Human-accepted merge-back of the proposed patch into the user's tree.

    Writing to the user's files is a UI-originated, explicitly-confirmed action
    — fail closed without both the Tauri origin and ``confirm: true``.
    """
    _require_tauri_origin(request)
    from errorta_tools.runner.apply_workspace import ApplyWorkspaceError

    aw = _apply_workspace_or_404(run_id)
    if not body.confirm:
        raise HTTPException(status_code=400, detail="confirmation_required")
    try:
        result = aw.merge_back(allow_conflicts=body.allow_conflicts)
    except ApplyWorkspaceError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if not result.get("applied"):
        # Conflicts and the caller didn't opt into clobbering -> 409, no writes.
        raise HTTPException(status_code=409, detail=result)
    return {"run_id": run_id, **result}


@router.get("/runs/{run_id}/steward-packets")
def list_steward_packets(run_id: str) -> dict[str, Any]:
    runs = _run_store()
    try:
        runs.read_run(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="run_not_found")
    packets = StewardPacketStore(runs_dir=runs.runs_dir).list(run_id)
    return {
        "run_id": run_id,
        "packets": packets,
        "packet_count": len(packets),
    }


@router.get("/runs/{run_id}/children")
def list_child_runs(run_id: str) -> dict[str, Any]:
    runs = _run_store()
    try:
        runs.read_run(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="run_not_found")
    records = ChildRunStore(runs_dir=runs.runs_dir).list(run_id)
    return {
        "run_id": run_id,
        "children": [r.to_dict() for r in records],
        "child_count": len(records),
    }


@router.get("/runs/{run_id}/children/{child_run_id}/messages")
def list_child_run_messages(run_id: str, child_run_id: str) -> dict[str, Any]:
    runs = _run_store()
    try:
        runs.read_run(run_id)
        ChildRunStore(runs_dir=runs.runs_dir).get(run_id, child_run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="run_not_found")
    except (ChildRunNotFound, ValueError):
        raise HTTPException(status_code=404, detail="child_run_not_found")
    messages = AsyncInbox(runs_dir=runs.runs_dir).list(run_id, child_run_id)
    return {
        "run_id": run_id,
        "child_run_id": child_run_id,
        "messages": [m.to_dict() for m in messages],
        "message_count": len(messages),
    }


@router.get("/runs/{run_id}/steward-packets/{packet_id}")
def get_steward_packet(run_id: str, packet_id: str) -> dict[str, Any]:
    runs = _run_store()
    try:
        runs.read_run(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="run_not_found")
    try:
        packet = StewardPacketStore(runs_dir=runs.runs_dir).read(run_id, packet_id)
    except (StewardPacketNotFound, ValueError):
        raise HTTPException(status_code=404, detail="steward_packet_not_found")
    return {"run_id": run_id, "packet": packet}


@router.post("/runs/{run_id}/steward-packets/rebuild")
def rebuild_steward_packet(run_id: str, request: Request) -> dict[str, Any]:
    # State-mutating POST — require UI origin, matching the callout + decision
    # routes (a stray cross-origin POST should not write packet artifacts).
    origin = request.headers.get("x-errorta-origin", "").lower()
    if origin != "tauri-ui":
        raise HTTPException(status_code=403, detail="origin_not_authorized")
    runs = _run_store()
    try:
        meta, events = runs.read_run(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="run_not_found")
    try:
        packet = build_deterministic_packet(run_meta=meta, events=events)
        path = StewardPacketStore(runs_dir=runs.runs_dir).write(run_id, packet)
    except FileExistsError:
        raise HTTPException(status_code=409, detail="steward_packet_exists")
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "steward_packet_rebuild_failed",
                "detail": type(exc).__name__,
            },
        )
    return {"run_id": run_id, "packet": packet, "path": str(path)}


@router.get("/runs/{run_id}/events")
def get_run_events(run_id: str, after_sequence: int = 0) -> dict[str, Any]:
    try:
        meta, events = _run_store().read_run(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="run not found")
    filtered = [e.to_dict() for e in events if e.sequence > after_sequence]
    return {
        "run_id": run_id, "events": filtered,
        "last_sequence": meta.last_sequence,
        "terminal": meta.status in TERMINAL_RUN_STATUSES,
    }


class _CancelRun(BaseModel):
    reason: str = "ui_stop_button"
    requested_by: str = "user"


class _DecisionBody(BaseModel):
    choice: str
    scope: str
    requested_by: str = "user"


class _PendingDecisionResolution(BaseModel):
    resolved_by: str = "user"


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, body: _CancelRun | None = None) -> dict[str, Any]:
    runs = _run_store()
    try:
        runs.read_run(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="run not found")
    reason = body.reason if body else "ui_stop_button"
    requested_by = body.requested_by if body else "user"
    control = RunControl(run_store=runs, run_id=run_id)
    try:
        new_meta, ev = await control.request_cancel(
            requested_by=requested_by, reason=reason
        )
    except TerminalRunError as exc:
        raise HTTPException(
            status_code=exc.http_status,
            detail={"code": "already_terminal"},
        )
    return {
        "run": new_meta.to_dict(),
        "event": (ev.to_dict() if ev is not None else None),
    }


@router.post("/runs/{run_id}/pause")
async def pause_run(run_id: str) -> dict[str, Any]:
    runs = _run_store()
    try:
        runs.read_run(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="run not found")
    control = RunControl(run_store=runs, run_id=run_id)
    try:
        new_meta = await control.request_pause(requested_by="user")
    except TerminalRunError as exc:
        raise HTTPException(status_code=exc.http_status, detail="terminal_run")
    return {"run": new_meta.to_dict()}


@router.post("/runs/{run_id}/resume")
async def resume_run(run_id: str) -> dict[str, Any]:
    runs = _run_store()
    try:
        runs.read_run(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="run not found")
    control = RunControl(run_store=runs, run_id=run_id)
    try:
        new_meta = await control.request_resume(requested_by="user")
    except TerminalRunError as exc:
        raise HTTPException(status_code=exc.http_status, detail="terminal_run")
    return {"run": new_meta.to_dict()}


class _InterjectionBody(BaseModel):
    text: str
    requested_by: str = "user"


@router.post("/runs/{run_id}/interjection")
async def interject_run(
    run_id: str, body: _InterjectionBody, request: Request
) -> dict[str, Any]:
    """F049: send a live user message into a running (or paused) run."""
    _require_tauri_origin(request)
    runs = _run_store()
    try:
        runs.read_run(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="run not found")
    control = RunControl(run_store=runs, run_id=run_id)
    try:
        new_meta, ev = await control.submit_interjection(
            text=body.text, requested_by=body.requested_by,
        )
    except TerminalRunError:
        raise HTTPException(status_code=409, detail="terminal_run")
    except ValueError as exc:
        # empty_interjection_text
        raise HTTPException(status_code=400, detail=str(exc))
    event_payload: dict[str, Any] | None = None
    if ev is not None:
        event_payload = {
            "sequence": ev.sequence,
            "type": ev.type.value,
            "payload": ev.payload,
        }
    return {"run": new_meta.to_dict(), "event": event_payload}


@router.post("/runs/{run_id}/decision")
async def decide_run(run_id: str, body: _DecisionBody, request: Request) -> dict[str, Any]:
    # F031-27 prep: ask-class decisions require UI origin.
    _require_tauri_origin(request)
    runs = _run_store()
    try:
        runs.read_run(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="run not found")
    control = RunControl(run_store=runs, run_id=run_id)
    try:
        new_meta, ev = await control.submit_decision(
            choice=body.choice, scope=body.scope, requested_by=body.requested_by,
        )
    except TerminalRunError as exc:
        raise HTTPException(status_code=exc.http_status, detail="terminal_run")
    except DecisionNotApplicable:
        # F031-09: decisions only resolve an ask-pause.
        raise HTTPException(status_code=409, detail="decision_not_applicable")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    event_payload: dict[str, Any] | None = None
    if ev is not None:
        event_payload = {
            "sequence": ev.sequence,
            "type": ev.type.value,
            "payload": ev.payload,
        }
    return {
        "run": new_meta.to_dict(),
        "event": event_payload,
    }


# ---- F041 pending policy decisions ----------------------------------------

@router.get("/runs/{run_id}/pending-decisions")
def list_pending_decisions(
    run_id: str, state: str | None = None
) -> dict[str, Any]:
    runs = _run_store()
    try:
        runs.read_run(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="run not found")
    try:
        decisions = _pending_decision_store(runs).list(run_id, state=state)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail={"code": "unknown_pending_decision_state"},
        )
    return {
        "run_id": run_id,
        "decisions": [d.to_dict() for d in decisions],
    }


def _resolve_pending_decision(
    *,
    run_id: str,
    decision_id: str,
    action: str,
    request: Request,
    body: _PendingDecisionResolution | None,
) -> dict[str, Any]:
    _require_tauri_origin(request)
    runs = _run_store()
    try:
        runs.read_run(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="run not found")
    resolved_by = body.resolved_by if body else "user"
    store = _pending_decision_store(runs)
    try:
        if action == "approve":
            decision = store.approve(
                run_id, decision_id, resolved_by=resolved_by
            )
        else:
            decision = store.reject(
                run_id, decision_id, resolved_by=resolved_by
            )
    except PendingDecisionNotFound:
        raise HTTPException(
            status_code=404,
            detail={"code": "pending_decision_not_found"},
        )
    except PendingDecisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "pending_decision_conflict", "detail": type(exc).__name__},
        )
    return {"run_id": run_id, "decision": decision.to_dict()}


@router.post("/runs/{run_id}/pending-decisions/{decision_id}/approve")
def approve_pending_decision(
    run_id: str,
    decision_id: str,
    request: Request,
    body: _PendingDecisionResolution | None = None,
) -> dict[str, Any]:
    return _resolve_pending_decision(
        run_id=run_id,
        decision_id=decision_id,
        action="approve",
        request=request,
        body=body,
    )


@router.post("/runs/{run_id}/pending-decisions/{decision_id}/reject")
def reject_pending_decision(
    run_id: str,
    decision_id: str,
    request: Request,
    body: _PendingDecisionResolution | None = None,
) -> dict[str, Any]:
    return _resolve_pending_decision(
        run_id=run_id,
        decision_id=decision_id,
        action="reject",
        request=request,
        body=body,
    )


# ---- F037 expert callouts -------------------------------------------------

class _CalloutRequest(BaseModel):
    target_id: str
    question: str = ""
    reason_code: str = "user_requested"
    requested_by: str = "user"


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@router.post("/runs/{run_id}/callouts")
async def request_callout(
    run_id: str, body: _CalloutRequest, request: Request
) -> dict[str, Any]:
    """Manual user callout: enqueue a request the running scheduler drains.

    Shape validation only (run live + target known + escalation enabled);
    budget/approval admission and all event emission happen in the scheduler
    under its writer token (invariant 2).
    """
    origin = request.headers.get("x-errorta-origin", "").lower()
    if origin != "tauri-ui":
        raise HTTPException(status_code=403, detail="origin_not_authorized")
    runs = _run_store()
    try:
        meta, _ = runs.read_run(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="run not found")
    if meta.status in TERMINAL_RUN_STATUSES:
        raise HTTPException(status_code=409, detail={"code": "run_terminal"})
    snapshot = meta.room_snapshot or {}
    policy = resolve_callout_policy(snapshot)
    if not policy.enabled:
        raise HTTPException(
            status_code=422, detail={"code": "escalation_disabled"}
        )
    target = find_target(snapshot, body.target_id)
    if target is None:
        raise HTTPException(
            status_code=404, detail={"code": "unknown_callout_target"}
        )
    callout_id = new_callout_id()
    queue = CalloutQueue(runs_dir=runs.runs_dir, run_id=run_id)
    queue.enqueue(CalloutRecord(
        callout_id=callout_id,
        target_id=body.target_id,
        reason_code=body.reason_code or "user_requested",
        question=body.question or "",
        requested_by={"type": "user", "actor": body.requested_by},
        state="requested",
        advisory=bool((target.callout or {}).get("advisory", True)),
        created_at=_utcnow_iso(),
    ))
    return {"run_id": run_id, "callout_id": callout_id, "status": "queued"}


@router.get("/runs/{run_id}/callouts")
def list_callouts(run_id: str) -> dict[str, Any]:
    runs = _run_store()
    try:
        runs.read_run(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="run not found")
    queue = CalloutQueue(runs_dir=runs.runs_dir, run_id=run_id)
    return {"run_id": run_id, "callouts": [r.to_dict() for r in queue.list()]}


def _resolve_callout_approval(
    run_id: str, callout_id: str, decision: str, request: Request
) -> dict[str, Any]:
    origin = request.headers.get("x-errorta-origin", "").lower()
    if origin != "tauri-ui":
        raise HTTPException(status_code=403, detail="origin_not_authorized")
    runs = _run_store()
    try:
        runs.read_run(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="run not found")
    queue = CalloutQueue(runs_dir=runs.runs_dir, run_id=run_id)
    rec = queue.get(callout_id)
    if rec is None:
        raise HTTPException(status_code=404, detail={"code": "unknown_callout"})
    if rec.state != "awaiting_approval":
        raise HTTPException(
            status_code=409,
            detail={"code": "callout_not_awaiting_approval", "state": rec.state},
        )
    updated = queue.update(callout_id, approval=decision)
    return {"run_id": run_id, "callout": updated.to_dict() if updated else None}


@router.post("/runs/{run_id}/callouts/{callout_id}/approve")
def approve_callout(run_id: str, callout_id: str, request: Request) -> dict[str, Any]:
    return _resolve_callout_approval(run_id, callout_id, "approved", request)


@router.post("/runs/{run_id}/callouts/{callout_id}/reject")
def reject_callout(run_id: str, callout_id: str, request: Request) -> dict[str, Any]:
    return _resolve_callout_approval(run_id, callout_id, "rejected", request)


def _room_dict_with_provider_hint(room: CouncilRoom) -> dict[str, Any]:
    """Convert room to dict and flatten member runtime fields.

    Phase 1 scheduler + resource guard read top-level ``provider``,
    ``model``, ``route_id``, ``max_output_tokens``, and ``temperature`` on
    each member dict. The CouncilMember schema nests these under
    ``turn_limits`` / ``generation`` / ``gateway_route_id``; this helper
    lifts them so per-member config is honored at runtime instead of
    silently falling back to scheduler defaults (P2 — drift fix).
    """
    raw = room.to_dict()
    members = list(raw.get("members") or [])
    for m in members:
        route_id = m.get("gateway_route_id") or ""
        if "provider" not in m:
            # Provider class is the route's first segment (split on . or /).
            # The earlier 'fake-or-local-always' branch silently flattened
            # F034 anthropic.* / openai.* / google.* / custom.* routes to
            # provider="local", which then crashed at the gateway boundary
            # as payload_route_mismatch (QA review 2026-06-12).
            head_dot = route_id.split(".", 1)[0]
            head_slash = route_id.split("/", 1)[0]
            prefix = head_dot if len(head_dot) <= len(head_slash) else head_slash
            m["provider"] = prefix or "local"
        if "model" not in m:
            m["model"] = m.get("model_display") or ""
        if "route_id" not in m:
            m["route_id"] = route_id
        turn_limits = m.get("turn_limits") or {}
        generation = m.get("generation") or {}
        if "max_output_tokens" not in m and turn_limits.get("max_output_tokens") is not None:
            m["max_output_tokens"] = int(turn_limits["max_output_tokens"])
        if "temperature" not in m and generation.get("temperature") is not None:
            m["temperature"] = float(generation["temperature"])
    raw["members"] = members
    return raw


@router.post("/rooms/{room_id}/resource-check")
async def resource_check(room_id: str) -> dict[str, Any]:
    try:
        room = _room_store().get(room_id)
    except RoomNotFound:
        raise HTTPException(status_code=404, detail="room_not_found")
    guard = LocalResourceGuard(
        gateway=LocalGateway(),
        hardware_scan_present=False,
    )
    preview = await guard.preview(room=_room_dict_with_provider_hint(room))
    return {
        "ollama_reachable": preview.ollama_reachable,
        "hardware_scan_present": preview.hardware_scan_present,
        "per_member": preview.per_member,
    }


@router.post("/rooms/{room_id}/dry-run")
async def dry_run_room(room_id: str) -> dict[str, Any]:
    try:
        room = _room_store().get(room_id)
    except RoomNotFound:
        raise HTTPException(status_code=404, detail="room_not_found")
    validation = validate_room(room, _gateway_meta())
    guard = LocalResourceGuard(
        gateway=LocalGateway(),
        hardware_scan_present=False,
    )
    preview = await guard.preview(room=_room_dict_with_provider_hint(room))
    return {
        "room_validation": {
            "status": validation.status,
            "errors": validation.errors,
            "warnings": validation.warnings,
        },
        "local_resources": {
            "ollama_reachable": preview.ollama_reachable,
            "hardware_scan_present": preview.hardware_scan_present,
            "per_member": preview.per_member,
        },
    }


# ---- Phase 2 audit-subset endpoints ---------------------------------------


@router.get("/runs/{run_id}/audit-summary")
def get_run_audit_summary(run_id: str) -> dict[str, Any]:
    runs = _run_store()
    try:
        runs.read_run(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="run_not_found")
    summary = build_run_audit_summary(runs, run_id)
    return _audit_summary_to_dict(summary)


_MANIFEST_QUERY_KEYS = frozenset({
    "include_manifest", "include_sources", "include_redaction",
    "include_egress", "include_omissions", "include_visibility",
    "include_before_turn", "manifest",
})


@router.get("/runs/{run_id}/turns/{turn_id}/audit")
def get_turn_audit(run_id: str, turn_id: str, request: Request) -> dict[str, Any]:
    # P2: reject manifest-oriented query params with 410 (fail-closed,
    # docs/superpowers/specs/2026-06-11-F031-phase-2-ui-shell.md §audit-subset).
    for key in request.query_params.keys():
        if key in _MANIFEST_QUERY_KEYS:
            raise HTTPException(status_code=410, detail="inspection_phase_3_only")
    runs = _run_store()
    try:
        runs.read_run(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="run_not_found")
    try:
        overview, after = build_turn_audit(runs, run_id, turn_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="turn_not_found")
    return {
        "overview": asdict(overview),
        "after_turn": asdict(after),
    }


@router.get("/runs/{run_id}/turns/{turn_id}/inspection")
def get_turn_inspection(run_id: str, turn_id: str) -> dict[str, Any]:
    """F031-08 inspection drawer feed (Phase 3 Task 12b).

    Returns the ContextManifest(s) the router wrote for this turn —
    typically one, but the shape is a list so a future relay topology
    (multiple manifests per turn) wouldn't require a breaking change.
    Manifests carry only sha256s and counts; raw payload text is never
    persisted (F031-05 §"Auditability"), so this endpoint is safe to
    surface in the UI.
    """
    runs = _run_store()
    try:
        runs.read_run(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="run_not_found")
    from errorta_council.context.manifest_store import ContextManifestStore
    from errorta_council.paths import council_root

    store = ContextManifestStore(root=council_root() / "context-manifests")
    matches = [
        m for m in store.list_by_run(run_id) if m.get("turn_id") == turn_id
    ]
    if not matches:
        raise HTTPException(status_code=404, detail="turn_manifest_not_found")
    return {
        "run_id": run_id,
        "turn_id": turn_id,
        "manifests": matches,
        "manifest_count": len(matches),
    }


@router.get("/runs/{run_id}/rounds/{round_n}/inspection")
def get_round_inspection(run_id: str, round_n: int) -> dict[str, Any]:
    """Round-level inspection — returns all per-member manifests for a round.

    QA P1 #1 lock: the engine adapter writes one manifest per (member, turn)
    with turn_id ``f"{member_id}-r{round}"``. The Phase 5 drawer's compare
    view only triggers on ``manifests.length >= 2``, so the per-turn
    inspection endpoint never reaches it for round_robin runs. This route
    aggregates every manifest sharing the same ``-r{round}`` suffix so the
    UI's Inspect click can render the side-by-side compare strip.
    """
    runs = _run_store()
    try:
        runs.read_run(run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="run_not_found")
    from errorta_council.context.manifest_store import ContextManifestStore
    from errorta_council.paths import council_root

    store = ContextManifestStore(root=council_root() / "context-manifests")
    suffix = f"-r{round_n}"
    matches = [
        m for m in store.list_by_run(run_id)
        if str(m.get("turn_id") or "").endswith(suffix)
    ]
    if not matches:
        raise HTTPException(status_code=404, detail="round_manifests_not_found")
    return {
        "run_id": run_id,
        "round": round_n,
        "manifests": matches,
        "manifest_count": len(matches),
    }


def _audit_summary_to_dict(summary) -> dict[str, Any]:
    raw = asdict(summary)
    # asdict serializes nested dataclasses; we just emit them verbatim.
    return raw
