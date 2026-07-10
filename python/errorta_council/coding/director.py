"""F118 — Director: an optional supervisor tier above the per-project PMs.

A Director owns a set of coding projects, aggregates their open F117 attention
signals into one queue, and (F118-02) offers a grounded chat that can route an
instruction down to a project's PM. It is a *room-less agent* — it never runs a
governance pipeline; on a chat turn it answers from a read-only briefing.

Storage: a per-Director directory ``${ERRORTA_HOME}/council/directors/<id>/``
holding ``director.json`` (atomic, 0600) + ``chat.jsonl`` — a directory, not a
bare file, so config + transcript never collide.

Ownership invariant: **at most one Director per project**. The reverse index
(project_id → director_id) is **derived on write** by scanning every
``director.json`` at validation time (not persisted, so it can't desync), and is
enforced on BOTH create and update. Reads tolerate dangling project ids (a
deleted project is skipped), so deleting a project needs no write-time scrub.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any

from errorta_council.coding import attention
from errorta_council.coding.ledger import (
    LedgerError,
    LedgerStore,
    _append_capped_jsonl,
    _atomic_write_json,
    _now,
    _read_jsonl,
)


class DirectorError(ValueError):
    """Raised on an invalid Director create/update (fail-loud)."""


_WRITE_LOCK = RLock()


@dataclass(frozen=True)
class Director:
    id: str
    name: str
    agent: dict[str, Any]            # room-less agent config (gateway_route_id, …)
    project_ids: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "agent": self.agent,
            "project_ids": list(self.project_ids),
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Director":
        return cls(
            id=str(raw["id"]), name=str(raw.get("name", "")),
            agent=dict(raw.get("agent") or {}),
            project_ids=[str(p) for p in (raw.get("project_ids") or [])],
            created_at=str(raw.get("created_at", "")),
            updated_at=str(raw.get("updated_at", "")),
        )


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def _root() -> Path:
    from errorta_app.paths import errorta_home

    return errorta_home() / "council" / "directors"


def _dir(director_id: str) -> Path:
    from errorta_export.safe_path import UnsafePathError, safe_segment

    try:
        safe_segment(director_id)
    except UnsafePathError as exc:
        raise DirectorError(f"invalid director id: {director_id!r}") from exc
    return _root() / director_id


def _json_path(director_id: str) -> Path:
    return _dir(director_id) / "director.json"


def _chat_path(director_id: str) -> Path:
    return _dir(director_id) / "chat.jsonl"


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #
def list_directors() -> list[Director]:
    root = _root()
    if not root.is_dir():
        return []
    out: list[Director] = []
    for child in sorted(root.iterdir()):
        path = child / "director.json"
        if not path.exists():
            continue
        try:
            import json
            out.append(Director.from_dict(json.loads(path.read_text("utf-8"))))
        except (OSError, ValueError, KeyError):
            continue  # tolerate an unreadable/partial director
    return out


def get_director(director_id: str) -> Director | None:
    path = _json_path(director_id)
    if not path.exists():
        return None
    try:
        import json
        return Director.from_dict(json.loads(path.read_text("utf-8")))
    except (OSError, ValueError, KeyError):
        return None


def _owner_index(*, exclude_id: str | None = None) -> dict[str, str]:
    """project_id -> director_id, derived by scanning all directors (minus
    ``exclude_id``). Computed on demand so it can never desync."""
    index: dict[str, str] = {}
    for d in list_directors():
        if d.id == exclude_id:
            continue
        for pid in d.project_ids:
            index[pid] = d.id
    return index


# --------------------------------------------------------------------------- #
# Write (ownership-validated)
# --------------------------------------------------------------------------- #
def _validate_ownership(project_ids: list[str], *, exclude_id: str | None) -> None:
    from errorta_export.safe_path import UnsafePathError, safe_segment

    for pid in project_ids:
        try:
            safe_segment(pid)
        except UnsafePathError as exc:
            raise DirectorError(f"invalid project id: {pid!r}") from exc
    index = _owner_index(exclude_id=exclude_id)
    for pid in project_ids:
        owner = index.get(pid)
        if owner is not None:
            raise DirectorError(
                f"project {pid!r} is already supervised by director {owner!r}")


def _write(director: Director) -> Director:
    _atomic_write_json(_json_path(director.id), director.to_dict())
    return director


def create_director(
    *, name: str, agent: dict[str, Any] | None = None,
    project_ids: list[str] | None = None,
) -> Director:
    project_ids = [str(p) for p in (project_ids or [])]
    if len(project_ids) != len(set(project_ids)):
        raise DirectorError("duplicate project_ids")
    with _WRITE_LOCK:
        _validate_ownership(project_ids, exclude_id=None)
        now = _now()
        director = Director(
            id=f"dir-{uuid.uuid4().hex[:12]}", name=name.strip() or "Director",
            agent=dict(agent or {}), project_ids=project_ids,
            created_at=now, updated_at=now,
        )
        return _write(director)


def update_director(
    director_id: str, *, name: str | None = None,
    agent: dict[str, Any] | None = None, project_ids: list[str] | None = None,
) -> Director:
    with _WRITE_LOCK:
        current = get_director(director_id)
        if current is None:
            raise DirectorError(f"unknown director: {director_id}")
        new_projects = current.project_ids if project_ids is None else [
            str(p) for p in project_ids]
        if len(new_projects) != len(set(new_projects)):
            raise DirectorError("duplicate project_ids")
        # remove-then-add: the reverse index excludes THIS director, so removed
        # projects are freed and any added project owned elsewhere is rejected (409).
        _validate_ownership(new_projects, exclude_id=director_id)
        updated = Director(
            id=current.id,
            name=current.name if name is None else (name.strip() or current.name),
            agent=current.agent if agent is None else dict(agent),
            project_ids=new_projects,
            created_at=current.created_at, updated_at=_now(),
        )
        return _write(updated)


def delete_director(director_id: str) -> bool:
    """Remove the Director's directory. Owned projects are untouched (the linkage
    lived only here). Returns False if it didn't exist."""
    with _WRITE_LOCK:
        d = _dir(director_id)
        if not d.is_dir():
            return False
        import shutil
        shutil.rmtree(d, ignore_errors=True)
        return True


# --------------------------------------------------------------------------- #
# Aggregation + briefing
# --------------------------------------------------------------------------- #
def _existing_project_store(project_id: str) -> LedgerStore | None:
    try:
        store = LedgerStore(project_id)
        store.get_project()
    except (LedgerError, OSError, ValueError, KeyError):
        return None
    return store


def aggregate_attention(director_id: str) -> list[dict[str, Any]]:
    """Cross-project attention queue: the union of every owned project's OPEN
    signals, grouped by project, Problems before Alerts. Missing projects are
    skipped (dangling-id tolerant)."""
    director = get_director(director_id)
    if director is None:
        raise DirectorError(f"unknown director: {director_id}")
    groups: list[dict[str, Any]] = []
    for pid in director.project_ids:
        store = _existing_project_store(pid)
        if store is None:
            continue
        signals = attention.list_open(pid, store=store)
        if not signals:
            continue
        signals.sort(key=lambda s: (s.kind != "problem", s.created_at))
        groups.append({
            "project_id": pid,
            "signals": [s.to_dict() for s in signals],
        })
    return groups


def inbox(director_id: str) -> list[dict[str, Any]]:
    """The escalation inbox: every open signal across owned projects flattened
    into a single list, **blocking Problems first**, then other Problems, then
    Alerts — each carrying its project_id for a deep link into that project."""
    items: list[dict[str, Any]] = []
    for group in aggregate_attention(director_id):
        for sig in group["signals"]:
            items.append({"project_id": group["project_id"], "signal": sig})

    def _rank(item: dict[str, Any]) -> tuple[int, str]:
        sig = item["signal"]
        if sig.get("kind") == "problem" and sig.get("blocking"):
            return (0, sig.get("created_at", ""))
        if sig.get("kind") == "problem":
            return (1, sig.get("created_at", ""))
        return (2, sig.get("created_at", ""))

    items.sort(key=_rank)
    return items


def project_briefing(project_id: str) -> dict[str, Any]:
    """A grounded, read-only summary of one project for the Director (no
    transcripts — structured counts + status). Liveness is read from
    ``run_state.status`` (bounded staleness is fine for a summary); we do not
    reach into the route layer's ``_thread_alive`` registry."""
    from errorta_council.coding.governance_status import governance_status
    from errorta_council.coding.runner import members_by_coding_role

    store = LedgerStore(project_id)
    members = [m for m in (store.get_run_config().get("members") or [])
               if isinstance(m, dict)]
    by_role = members_by_coding_role(members)
    running = store.get_run_state().get("status") == "running"
    status = governance_status(store, by_role, run_active=running)
    open_signals = attention.list_open(project_id, store=store)
    return {
        "project_id": project_id,
        "stage": status.get("stage"),
        "status": status.get("status"),
        "headline": status.get("headline"),
        "needs_human": bool(status.get("needs_human")),
        "running": running,
        "open_problems": sum(1 for s in open_signals if s.kind == "problem"),
        "open_alerts": sum(1 for s in open_signals if s.kind == "alert"),
    }


# --------------------------------------------------------------------------- #
# Chat transcript
# --------------------------------------------------------------------------- #
def append_chat(director_id: str, *, role: str, text: str,
                extra: dict[str, Any] | None = None) -> dict[str, Any]:
    if get_director(director_id) is None:
        raise DirectorError(f"unknown director: {director_id}")
    rec = {"role": role, "text": text, "at": _now()}
    if extra:
        rec.update(extra)
    _append_capped_jsonl(_chat_path(director_id), rec)
    return rec


def load_chat(director_id: str) -> list[dict[str, Any]]:
    if get_director(director_id) is None:
        raise DirectorError(f"unknown director: {director_id}")
    return _read_jsonl(_chat_path(director_id))
