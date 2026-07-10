"""Value objects for F042 child runs.

Child runs are parent-owned task records, not a replacement for the Council
run log. They carry capped previews and hashes only; raw child output does not
enter the parent transcript through these records.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

FORMAT_VERSION = 1
CHILD_RUN_STATUSES = frozenset({"queued", "running", "completed", "failed", "cancelled"})


def _require_status(status: str) -> str:
    if status not in CHILD_RUN_STATUSES:
        raise ValueError(f"unknown_child_run_status: {status}")
    return status


@dataclass(frozen=True)
class ChildRunRecord:
    format_version: int
    parent_run_id: str
    child_run_id: str
    member_id: str
    task_kind: str
    status: str
    title: str
    prompt_sha256: str
    worker_kind: str
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    artifact_refs: list[dict[str, Any]] = field(default_factory=list)
    summary_ref: dict[str, Any] | None = None
    failure: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "format_version": self.format_version,
            "parent_run_id": self.parent_run_id,
            "child_run_id": self.child_run_id,
            "member_id": self.member_id,
            "task_kind": self.task_kind,
            "status": self.status,
            "title": self.title,
            "prompt_sha256": self.prompt_sha256,
            "worker_kind": self.worker_kind,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "artifact_refs": list(self.artifact_refs),
            "metadata": dict(self.metadata),
        }
        if self.started_at is not None:
            d["started_at"] = self.started_at
        if self.finished_at is not None:
            d["finished_at"] = self.finished_at
        if self.summary_ref is not None:
            d["summary_ref"] = dict(self.summary_ref)
        if self.failure is not None:
            d["failure"] = dict(self.failure)
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ChildRunRecord":
        if int(raw.get("format_version", 0)) != FORMAT_VERSION:
            raise ValueError("unsupported_child_run_format_version")
        status = _require_status(str(raw["status"]))
        return cls(
            format_version=FORMAT_VERSION,
            parent_run_id=str(raw["parent_run_id"]),
            child_run_id=str(raw["child_run_id"]),
            member_id=str(raw["member_id"]),
            task_kind=str(raw["task_kind"]),
            status=status,
            title=str(raw.get("title") or raw["task_kind"]),
            prompt_sha256=str(raw["prompt_sha256"]),
            worker_kind=str(raw.get("worker_kind") or "scripted"),
            created_at=str(raw["created_at"]),
            updated_at=str(raw["updated_at"]),
            started_at=raw.get("started_at"),
            finished_at=raw.get("finished_at"),
            artifact_refs=list(raw.get("artifact_refs") or []),
            summary_ref=(dict(raw["summary_ref"]) if raw.get("summary_ref") else None),
            failure=(dict(raw["failure"]) if raw.get("failure") else None),
            metadata=dict(raw.get("metadata") or {}),
        )

    def event_projection(self) -> dict[str, Any]:
        out = {
            "parent_run_id": self.parent_run_id,
            "child_run_id": self.child_run_id,
            "member_id": self.member_id,
            "task_kind": self.task_kind,
            "status": self.status,
            "title": self.title,
            "worker_kind": self.worker_kind,
            "artifact_refs": list(self.artifact_refs),
        }
        if self.summary_ref is not None:
            out["summary_ref"] = dict(self.summary_ref)
        if self.failure is not None:
            out["failure"] = dict(self.failure)
        return out


@dataclass(frozen=True)
class ChildRunMessage:
    format_version: int
    message_id: str
    parent_run_id: str
    child_run_id: str
    message_kind: str
    payload_preview: str
    payload_sha256: str
    payload_bytes: int
    created_at: str
    artifact_refs: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "message_id": self.message_id,
            "parent_run_id": self.parent_run_id,
            "child_run_id": self.child_run_id,
            "message_kind": self.message_kind,
            "payload_preview": self.payload_preview,
            "payload_sha256": self.payload_sha256,
            "payload_bytes": self.payload_bytes,
            "created_at": self.created_at,
            "artifact_refs": list(self.artifact_refs),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ChildRunMessage":
        if int(raw.get("format_version", 0)) != FORMAT_VERSION:
            raise ValueError("unsupported_child_message_format_version")
        return cls(
            format_version=FORMAT_VERSION,
            message_id=str(raw["message_id"]),
            parent_run_id=str(raw["parent_run_id"]),
            child_run_id=str(raw["child_run_id"]),
            message_kind=str(raw["message_kind"]),
            payload_preview=str(raw.get("payload_preview") or ""),
            payload_sha256=str(raw["payload_sha256"]),
            payload_bytes=int(raw.get("payload_bytes") or 0),
            created_at=str(raw["created_at"]),
            artifact_refs=list(raw.get("artifact_refs") or []),
            metadata=dict(raw.get("metadata") or {}),
        )
