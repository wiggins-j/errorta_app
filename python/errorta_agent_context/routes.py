"""FastAPI routes for F035 agent-context capsules."""
from __future__ import annotations

import datetime as _dt
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from errorta_app.paths import agent_context_dir, errorta_home
from errorta_agent_context.pack import pack_capsule
from errorta_agent_context.refs import ReferenceResolver
from errorta_agent_context.schema import AgentContextCapsule, AgentContextDelta
from errorta_agent_context.store import AgentContextStore

router = APIRouter(prefix="/agent-context", tags=["agent-context"])


class CreateCapsuleBody(BaseModel):
    capsule: dict[str, Any] | None = None
    task: dict[str, Any] | None = None
    scope: dict[str, Any] | None = None
    state: dict[str, Any] | None = None
    refs: list[dict[str, Any]] | None = None
    policy: dict[str, Any] | None = None
    kind: str = "micro"


class CreateDeltaBody(BaseModel):
    delta: dict[str, Any]


class PackBody(BaseModel):
    capsule_id: str | None = None
    capsule: dict[str, Any] | None = None
    resolution: str = "micro"
    destination_scope: str = "local"
    max_tokens: int = 1200
    include_ref_summaries: bool = True


def _store() -> AgentContextStore:
    return AgentContextStore(agent_context_dir())


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolver() -> ReferenceResolver:
    return ReferenceResolver(repo_root=_repo_root(), errorta_home=errorta_home())


@router.post("/capsules")
def create_capsule(body: CreateCapsuleBody) -> dict[str, Any]:
    try:
        if body.capsule is not None:
            capsule = AgentContextCapsule.from_dict(body.capsule)
        else:
            now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            capsule = AgentContextCapsule.from_dict({
                "format": "errorta.agent_context_capsule.v1",
                "capsule_id": f"cap_{uuid.uuid4().hex[:16]}",
                "kind": body.kind,
                "created_at": now,
                "task": body.task or {},
                "scope": body.scope or {},
                "state": body.state or {},
                "refs": body.refs or [],
                "policy": body.policy or {},
            })
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    _store().write_capsule(capsule)
    data = capsule.to_dict()
    return {
        "capsule": data,
        "canonical_sha256": data["digest"]["canonical_sha256"],
    }


@router.get("/capsules")
def list_capsules() -> dict[str, Any]:
    return {"capsules": _store().list_capsules()}


@router.get("/refs/summary")
def summarize_ref(uri: str) -> dict[str, Any]:
    summary = _resolver().summarize(uri)
    return {
        "uri": summary.uri,
        "ok": summary.ok,
        "summary": summary.summary,
        "sha256": summary.sha256,
        "reason": summary.reason,
    }


@router.get("/capsules/{capsule_id}")
def get_capsule(capsule_id: str) -> dict[str, Any]:
    try:
        capsule = _store().materialize(capsule_id)
    except Exception:
        raise HTTPException(status_code=404, detail="capsule not found")
    return {"capsule": capsule.to_dict()}


@router.post("/capsules/{capsule_id}/delta")
def create_delta(capsule_id: str, body: CreateDeltaBody) -> dict[str, Any]:
    raw = dict(body.delta)
    raw.setdefault("parent_id", capsule_id)
    try:
        delta = AgentContextDelta.from_dict(raw)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    _store().write_delta(delta)
    return {"delta": delta.to_dict()}


@router.post("/capsules/{capsule_id}/materialize")
def materialize(capsule_id: str) -> dict[str, Any]:
    try:
        capsule = _store().materialize(capsule_id)
    except Exception:
        raise HTTPException(status_code=404, detail="capsule not found")
    return {"capsule": capsule.to_dict()}


@router.post("/pack")
def pack(body: PackBody) -> dict[str, Any]:
    try:
        capsule = (
            AgentContextCapsule.from_dict(body.capsule)
            if body.capsule is not None
            else _store().materialize(str(body.capsule_id))
        )
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    packed = pack_capsule(
        capsule,
        resolver=_resolver(),
        resolution=body.resolution,
        destination_scope=body.destination_scope,
        max_tokens=body.max_tokens,
        include_ref_summaries=body.include_ref_summaries,
    )
    return {
        "text": packed.text,
        "included_refs": packed.included_refs,
        "omitted_refs": packed.omitted_refs,
        "estimated_tokens": packed.estimated_tokens,
    }


@router.post("/validate")
def validate_capsule(raw: dict[str, Any]) -> dict[str, Any]:
    try:
        capsule = AgentContextCapsule.from_dict(raw)
    except Exception as exc:
        return {"ok": False, "errors": [str(exc)]}
    return {"ok": True, "canonical_sha256": capsule.canonical_sha256(), "errors": []}


__all__ = ["router"]
