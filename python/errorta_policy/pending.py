"""Durable pending policy decisions for F041."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .types import PendingDecisionRequest, PolicyPhase, PolicyStateWrite

FORMAT_VERSION = 1
PENDING_STATES = frozenset({"pending", "approved", "rejected", "expired"})
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class PendingDecisionNotFound(FileNotFoundError):
    pass


class PendingDecisionConflict(RuntimeError):
    pass


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _assert_safe_id(value: str, *, name: str) -> None:
    if not value or not _SAFE_ID_RE.match(value) or ".." in value:
        raise ValueError(f"unsafe_{name}")


def _stable_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _stable_decision_id(request: PendingDecisionRequest) -> str:
    seed = {
        "run_id": request.run_id,
        "phase": request.phase.value,
        "reason_code": request.reason_code,
        "requester": request.requester,
        "safe_request": request.safe_request,
    }
    return "pd-" + hashlib.sha256(_stable_json(seed).encode("utf-8")).hexdigest()[:20]


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, sort_keys=True, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class PendingDecisionRecord:
    format_version: int
    decision_id: str
    run_id: str
    phase: PolicyPhase
    state: str
    reason_code: str
    requester: dict[str, Any]
    safe_request: dict[str, Any]
    state_writes_on_approve: tuple[PolicyStateWrite, ...]
    created_at: str
    risk_class: str | None = None
    created_by_policy_id: str | None = None
    resolved_at: str | None = None
    resolved_by: str | None = None
    applied_state_writes: tuple[PolicyStateWrite, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "format_version": self.format_version,
            "decision_id": self.decision_id,
            "run_id": self.run_id,
            "phase": self.phase.value,
            "state": self.state,
            "reason_code": self.reason_code,
            "requester": dict(self.requester),
            "safe_request": dict(self.safe_request),
            "state_writes_on_approve": [
                w.to_dict() for w in self.state_writes_on_approve
            ],
            "applied_state_writes": [
                w.to_dict() for w in self.applied_state_writes
            ],
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }
        if self.risk_class is not None:
            d["risk_class"] = self.risk_class
        if self.created_by_policy_id is not None:
            d["created_by_policy_id"] = self.created_by_policy_id
        if self.resolved_at is not None:
            d["resolved_at"] = self.resolved_at
        if self.resolved_by is not None:
            d["resolved_by"] = self.resolved_by
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PendingDecisionRecord":
        if int(raw.get("format_version", 0)) != FORMAT_VERSION:
            raise ValueError("unsupported_pending_decision_format_version")
        state = str(raw["state"])
        if state not in PENDING_STATES:
            raise ValueError("unknown_pending_decision_state")
        return cls(
            format_version=FORMAT_VERSION,
            decision_id=str(raw["decision_id"]),
            run_id=str(raw["run_id"]),
            phase=PolicyPhase(str(raw["phase"])),
            state=state,
            reason_code=str(raw["reason_code"]),
            requester=dict(raw.get("requester") or {}),
            safe_request=dict(raw.get("safe_request") or {}),
            state_writes_on_approve=tuple(
                PolicyStateWrite.from_dict(w)
                for w in raw.get("state_writes_on_approve") or []
            ),
            created_at=str(raw["created_at"]),
            risk_class=raw.get("risk_class"),
            created_by_policy_id=raw.get("created_by_policy_id"),
            resolved_at=raw.get("resolved_at"),
            resolved_by=raw.get("resolved_by"),
            applied_state_writes=tuple(
                PolicyStateWrite.from_dict(w)
                for w in raw.get("applied_state_writes") or []
            ),
            metadata=dict(raw.get("metadata") or {}),
        )

    def audit_projection(self) -> dict[str, Any]:
        """Event-safe projection: no raw provider/tool payloads."""
        return {
            "decision_id": self.decision_id,
            "phase": self.phase.value,
            "state": self.state,
            "reason_code": self.reason_code,
            "requester": dict(self.requester),
            "safe_request": dict(self.safe_request),
            "risk_class": self.risk_class,
        }


class PendingDecisionStore:
    """Run-scoped durable pending-decision store.

    Files live under ``<runs_dir>/pending-decisions/<run_id>/<decision_id>.json``.
    Keeping them below ``runs_dir`` preserves the council backup/recovery
    boundary without changing the existing flat run-log layout.
    """

    def __init__(self, *, runs_dir: Path) -> None:
        self._root = Path(runs_dir) / "pending-decisions"

    def _run_dir(self, run_id: str) -> Path:
        _assert_safe_id(run_id, name="run_id")
        return self._root / run_id

    def _path(self, run_id: str, decision_id: str) -> Path:
        _assert_safe_id(decision_id, name="decision_id")
        return self._run_dir(run_id) / f"{decision_id}.json"

    def create(self, request: PendingDecisionRequest) -> PendingDecisionRecord:
        decision_id = request.decision_id or _stable_decision_id(request)
        _assert_safe_id(decision_id, name="decision_id")
        path = self._path(request.run_id, decision_id)
        if path.exists():
            return self.get(request.run_id, decision_id)
        record = PendingDecisionRecord(
            format_version=FORMAT_VERSION,
            decision_id=decision_id,
            run_id=request.run_id,
            phase=request.phase,
            state="pending",
            reason_code=request.reason_code,
            requester=dict(request.requester),
            safe_request=dict(request.safe_request),
            state_writes_on_approve=tuple(request.state_writes_on_approve),
            created_at=_utcnow(),
            risk_class=request.risk_class,
            created_by_policy_id=request.created_by_policy_id,
            metadata=dict(request.metadata),
        )
        _atomic_write_json(path, record.to_dict())
        return record

    def get(self, run_id: str, decision_id: str) -> PendingDecisionRecord:
        path = self._path(run_id, decision_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise PendingDecisionNotFound(decision_id) from exc
        return PendingDecisionRecord.from_dict(raw)

    def list(
        self, run_id: str, *, state: str | None = None
    ) -> list[PendingDecisionRecord]:
        if state is not None and state not in PENDING_STATES:
            raise ValueError("unknown_pending_decision_state")
        run_dir = self._run_dir(run_id)
        if not run_dir.exists():
            return []
        records: list[PendingDecisionRecord] = []
        for path in sorted(run_dir.glob("*.json")):
            rec = PendingDecisionRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
            if state is None or rec.state == state:
                records.append(rec)
        records.sort(key=lambda r: (r.created_at, r.decision_id))
        return records

    def approve(
        self, run_id: str, decision_id: str, *, resolved_by: str = "user"
    ) -> PendingDecisionRecord:
        record = self.get(run_id, decision_id)
        if record.state == "approved":
            return record
        if record.state != "pending":
            raise PendingDecisionConflict(
                f"decision {decision_id} is {record.state}, not pending"
            )
        updated = PendingDecisionRecord(
            **{
                **record.to_dict(),
                "phase": record.phase,
                "state": "approved",
                "resolved_at": _utcnow(),
                "resolved_by": resolved_by,
                "state_writes_on_approve": record.state_writes_on_approve,
                "applied_state_writes": record.state_writes_on_approve,
            }
        )
        _atomic_write_json(self._path(run_id, decision_id), updated.to_dict())
        return updated

    def reject(
        self, run_id: str, decision_id: str, *, resolved_by: str = "user"
    ) -> PendingDecisionRecord:
        record = self.get(run_id, decision_id)
        if record.state == "rejected":
            return record
        if record.state != "pending":
            raise PendingDecisionConflict(
                f"decision {decision_id} is {record.state}, not pending"
            )
        updated = PendingDecisionRecord(
            **{
                **record.to_dict(),
                "phase": record.phase,
                "state": "rejected",
                "resolved_at": _utcnow(),
                "resolved_by": resolved_by,
                "state_writes_on_approve": record.state_writes_on_approve,
                "applied_state_writes": (),
            }
        )
        _atomic_write_json(self._path(run_id, decision_id), updated.to_dict())
        return updated
