"""F005 — folder watch + auto-ingest router.

Endpoints:
    POST   /watch/start                  begin watching a folder for a corpus
    POST   /watch/stop                   stop watching
    POST   /watch/pause                  pause polling (keep state)
    POST   /watch/resume                 resume polling
    POST   /watch/change-path            switch the watched folder
    POST   /watch/set-deletion-policy    "remove" or "mark_missing"
    POST   /watch/check-path             inspect a path for cloud-sync / supported files
    GET    /watch/status                 status (single corpus via ?corpus= or all)
"""
from __future__ import annotations

import os
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from errorta_watch import (
    DEFAULT_IGNORES,
    get_coordinator,
    is_cloud_sync_path,
)
from errorta_watch.ignore import DEFAULT_SUPPORTED_EXTS, is_ignored, is_supported

router = APIRouter(prefix="/watch", tags=["watch"])


# ---- request / response models -----------------------------------------

class StartWatchRequest(BaseModel):
    corpus: str = Field(..., min_length=1)
    watched_path: str = Field(..., min_length=1)
    deletion_policy: Literal["remove", "mark_missing"] = "remove"
    type_filter: list[str] = Field(default_factory=list)
    extra_ignores: list[str] = Field(default_factory=list)


class CorpusRequest(BaseModel):
    corpus: str = Field(..., min_length=1)


class ChangePathRequest(BaseModel):
    corpus: str = Field(..., min_length=1)
    watched_path: str = Field(..., min_length=1)


class DeletionPolicyRequest(BaseModel):
    corpus: str = Field(..., min_length=1)
    deletion_policy: Literal["remove", "mark_missing"]


class CheckPathRequest(BaseModel):
    path: str = Field(..., min_length=1)
    type_filter: list[str] = Field(default_factory=list)


# ---- helpers ------------------------------------------------------------

def _expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _scan_summary(path: str, type_filter: list[str]) -> dict:
    """Cheap pre-scan: count supported files + total bytes."""
    if not os.path.isdir(path):
        return {"file_count": 0, "total_bytes": 0, "exists": False}
    files = 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        dirnames[:] = [
            d for d in dirnames
            if not is_ignored(d)
            and not os.path.islink(os.path.join(dirpath, d))
        ]
        for name in filenames:
            if is_ignored(name):
                continue
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue
            if not is_supported(full, type_filter):
                continue
            try:
                total += os.path.getsize(full)
            except OSError:
                continue
            files += 1
    return {"file_count": files, "total_bytes": total, "exists": True}


# ---- endpoints ----------------------------------------------------------

@router.post("/start")
def start(req: StartWatchRequest) -> dict:
    path = _expand(req.watched_path)
    if not os.path.isdir(path):
        raise HTTPException(status_code=400, detail=f"not a directory: {path}")
    try:
        return get_coordinator().start(
            req.corpus,
            path,
            deletion_policy=req.deletion_policy,
            type_filter=req.type_filter,
            extra_ignores=req.extra_ignores,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/stop")
def stop(req: CorpusRequest) -> dict:
    ok = get_coordinator().stop(req.corpus)
    return {"stopped": ok, "corpus": req.corpus}


@router.post("/pause")
def pause(req: CorpusRequest) -> dict:
    ok = get_coordinator().pause(req.corpus)
    if not ok:
        raise HTTPException(status_code=404, detail=f"not watching: {req.corpus}")
    return {"paused": True, "corpus": req.corpus}


@router.post("/resume")
def resume(req: CorpusRequest) -> dict:
    ok = get_coordinator().resume(req.corpus)
    if not ok:
        raise HTTPException(status_code=404, detail=f"not watching: {req.corpus}")
    return {"paused": False, "corpus": req.corpus}


@router.post("/change-path")
def change_path(req: ChangePathRequest) -> dict:
    path = _expand(req.watched_path)
    if not os.path.isdir(path):
        raise HTTPException(status_code=400, detail=f"not a directory: {path}")
    try:
        return get_coordinator().change_path(req.corpus, path)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/set-deletion-policy")
def set_deletion_policy(req: DeletionPolicyRequest) -> dict:
    try:
        return get_coordinator().set_deletion_policy(req.corpus, req.deletion_policy)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/check-path")
def check_path(req: CheckPathRequest) -> dict:
    """Pre-flight check used by the folder-picker dialog.

    Returns existence, the cloud-sync provider (if any), a count of supported
    files, and the total bytes — enough for the dialog to show its "Found N
    supported files (X GB)" panel and its cloud-sync warning.
    """
    path = _expand(req.path)
    summary = _scan_summary(path, req.type_filter)
    return {
        "path": path,
        **summary,
        "cloud_sync_provider": is_cloud_sync_path(path),
        "default_ignores": list(DEFAULT_IGNORES),
        "supported_extensions": list(DEFAULT_SUPPORTED_EXTS),
    }


@router.get("/status")
def status(corpus: str | None = None) -> dict:
    coord = get_coordinator()
    if corpus:
        return coord.status(corpus)
    return {"watchers": coord.status_all()}


@router.post("/force-rescan")
def force_rescan(req: CorpusRequest) -> dict:
    """F005-PROD: kick an immediate reconciliation for a stale watcher."""
    try:
        return get_coordinator().force_rescan(req.corpus)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
