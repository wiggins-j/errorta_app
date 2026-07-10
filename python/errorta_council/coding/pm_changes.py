"""F145 Slice 3 — the "PM Changes" consent surface.

Every setting the PM changes is applied immediately, then recorded as a reviewable
**PM Changes** set: a before→after diff plus a *restore* payload. The user
**Accept**s (keep — drop the restore) or **Decline**s (revert — re-apply the prior
values through the right setter). A change also carries a ``surface`` flag —
``pop`` (user-directed: show the review) vs ``log`` (PM-initiated during an
accepted autonomous run: Team-Log only) — and, for a change that turns the run
autonomous, an autonomy warning + an optional total-call cap for the UI.

Restore is generic: a change names a ``target`` config domain and the prior
``value`` of exactly the fields it touched; Decline dispatches to that domain's
setter. Storage is a per-project ``pm-changes.json`` projection (full-rewrite
under the project lock), mirroring the other coding ledgers.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .ledger import _atomic_write_json, _now

RESTORE_TARGETS = ("autonomy", "run_config", "governance", "guardrail", "task")
SURFACES = ("pop", "log")


class PmChangeError(Exception):
    pass


@dataclass
class PmChange:
    change_id: str
    project_id: str
    summary: str
    details: list[dict[str, Any]]           # [{field, before, after}]
    restore_target: str
    restore_value: dict[str, Any]
    surface: str = "pop"
    autonomy: dict[str, Any] | None = None  # {warning: bool, suggested_cap: int|None}
    status: str = "pending"                 # pending | accepted | declined
    at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PmChange":
        return cls(
            change_id=str(raw["change_id"]),
            project_id=str(raw.get("project_id") or ""),
            summary=str(raw.get("summary") or ""),
            details=[dict(d) for d in raw.get("details", [])],
            restore_target=str(raw.get("restore_target") or ""),
            restore_value=dict(raw.get("restore_value") or {}),
            surface=str(raw.get("surface") or "pop"),
            autonomy=(dict(raw["autonomy"]) if raw.get("autonomy") else None),
            status=str(raw.get("status") or "pending"),
            at=str(raw.get("at") or ""),
        )


def _path(store: Any) -> Path:
    return store.dir / "pm-changes.json"


def _load(store: Any) -> dict[str, dict[str, Any]]:
    path = _path(store)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def record_change(
    store: Any, *, summary: str, details: list[dict[str, Any]],
    restore_target: str, restore_value: dict[str, Any],
    surface: str = "pop", autonomy: dict[str, Any] | None = None,
) -> PmChange:
    """Record an already-applied change as a reviewable PM Changes set."""
    if restore_target not in RESTORE_TARGETS:
        raise PmChangeError(f"invalid restore_target: {restore_target!r}")
    if surface not in SURFACES:
        raise PmChangeError(f"invalid surface: {surface!r}")
    change = PmChange(
        change_id=f"pmc-{uuid.uuid4().hex[:12]}",
        project_id=getattr(store, "project_id", ""),
        summary=summary, details=list(details),
        restore_target=restore_target, restore_value=dict(restore_value),
        surface=surface, autonomy=autonomy, status="pending", at=_now(),
    )
    with store.lock:
        data = _load(store)
        data[change.change_id] = change.to_dict()
        _atomic_write_json(_path(store), data)
    return change


def get_change(store: Any, change_id: str) -> PmChange | None:
    raw = _load(store).get(change_id)
    return PmChange.from_dict(raw) if raw else None


def list_changes(store: Any, *, status: str | None = None) -> list[PmChange]:
    out = [PmChange.from_dict(v) for v in _load(store).values()]
    if status is not None:
        out = [c for c in out if c.status == status]
    return sorted(out, key=lambda c: c.at)


def _set_status(store: Any, change_id: str, status: str) -> PmChange:
    with store.lock:
        data = _load(store)
        raw = data.get(change_id)
        if raw is None:
            raise PmChangeError("change_not_found")
        raw["status"] = status
        data[change_id] = raw
        _atomic_write_json(_path(store), data)
        return PmChange.from_dict(raw)


def accept(store: Any, change_id: str) -> PmChange:
    """Keep the change (the applied state stands)."""
    change = get_change(store, change_id)
    if change is None:
        raise PmChangeError("change_not_found")
    if change.status != "pending":
        return change
    return _set_status(store, change_id, "accepted")


def decline(store: Any, change_id: str) -> PmChange:
    """Revert: re-apply the prior values through the target's setter, then mark
    declined."""
    change = get_change(store, change_id)
    if change is None:
        raise PmChangeError("change_not_found")
    if change.status != "pending":
        return change
    _apply_restore(store, change.restore_target, change.restore_value)
    return _set_status(store, change_id, "declined")


def _apply_restore(store: Any, target: str, value: dict[str, Any]) -> None:
    if target == "autonomy":
        from .autonomy import load_policy, policy_from_dict, policy_to_dict, save_policy

        merged = {**policy_to_dict(load_policy(store)), **value}
        save_policy(store, policy_from_dict(merged))
    elif target == "run_config":
        # value = the prior {room_id, members}; set_run_config overwrites those keys
        store.set_run_config(**value)
    elif target == "governance":
        from .governance import GovernanceStore

        GovernanceStore.for_ledger(store).update_state(**value)
    elif target == "guardrail":
        from .skills import load_guardrail, save_guardrail

        current = load_guardrail(store)
        from dataclasses import replace

        save_guardrail(store, replace(current, enabled=bool(value.get("enabled", current.enabled))))
    elif target == "task":
        # Revert a PM-created task by DROPPING it (no hard delete exists; "dropped"
        # is the ledger's terminal removed state — it leaves the board).
        tid = str(value.get("task_id") or "").strip()
        if tid:
            store.update_task(tid, state="dropped")
    else:  # pragma: no cover - guarded at record time
        raise PmChangeError(f"unknown restore target: {target!r}")


__all__ = [
    "PmChange", "PmChangeError", "RESTORE_TARGETS", "SURFACES",
    "record_change", "get_change", "list_changes", "accept", "decline",
]
