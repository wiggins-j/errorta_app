"""Durable child-run store for F042."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from .schema import FORMAT_VERSION, ChildRunRecord

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class ChildRunNotFound(FileNotFoundError):
    pass


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _assert_safe_id(value: str, *, name: str) -> None:
    if not value or not _SAFE_ID_RE.match(value) or ".." in value:
        raise ValueError(f"unsafe_{name}")


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


class ChildRunStore:
    """Parent-run scoped child task records.

    The main run store is flat (`<run_id>.jsonl` + meta), so child records live
    in a side directory under the same runs root:
    `<runs_dir>/children/<parent_run_id>/<child_run_id>.json`.
    """

    def __init__(self, *, runs_dir: Path) -> None:
        self._root = Path(runs_dir) / "children"

    def _parent_dir(self, parent_run_id: str) -> Path:
        _assert_safe_id(parent_run_id, name="parent_run_id")
        return self._root / parent_run_id

    def _path(self, parent_run_id: str, child_run_id: str) -> Path:
        _assert_safe_id(child_run_id, name="child_run_id")
        return self._parent_dir(parent_run_id) / f"{child_run_id}.json"

    def create(
        self,
        *,
        parent_run_id: str,
        member_id: str,
        task_kind: str,
        title: str,
        prompt: str,
        worker_kind: str = "scripted",
        metadata: dict[str, Any] | None = None,
    ) -> ChildRunRecord:
        child_run_id = "cr-" + uuid.uuid4().hex[:16]
        now = _utcnow()
        record = ChildRunRecord(
            format_version=FORMAT_VERSION,
            parent_run_id=parent_run_id,
            child_run_id=child_run_id,
            member_id=member_id,
            task_kind=task_kind,
            status="queued",
            title=title,
            prompt_sha256=_sha(prompt),
            worker_kind=worker_kind,
            created_at=now,
            updated_at=now,
            metadata=dict(metadata or {}),
        )
        _atomic_write_json(self._path(parent_run_id, child_run_id), record.to_dict())
        return record

    def get(self, parent_run_id: str, child_run_id: str) -> ChildRunRecord:
        path = self._path(parent_run_id, child_run_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ChildRunNotFound(child_run_id) from exc
        return ChildRunRecord.from_dict(raw)

    def list(self, parent_run_id: str) -> list[ChildRunRecord]:
        parent_dir = self._parent_dir(parent_run_id)
        if not parent_dir.exists():
            return []
        records: list[ChildRunRecord] = []
        for path in sorted(parent_dir.glob("*.json")):
            try:
                records.append(
                    ChildRunRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
                )
            except Exception:
                continue
        records.sort(key=lambda r: (r.created_at, r.child_run_id))
        return records

    def update(self, record: ChildRunRecord, **updates: Any) -> ChildRunRecord:
        if "status" in updates:
            # Let ChildRunRecord validate the status below.
            updates["status"] = str(updates["status"])
        updated = replace(record, updated_at=_utcnow(), **updates)
        ChildRunRecord.from_dict(updated.to_dict())
        _atomic_write_json(
            self._path(updated.parent_run_id, updated.child_run_id),
            updated.to_dict(),
        )
        return updated

    def mark_running(self, record: ChildRunRecord) -> ChildRunRecord:
        return self.update(record, status="running", started_at=_utcnow())

    def mark_completed(
        self,
        record: ChildRunRecord,
        *,
        summary_ref: dict[str, Any],
        artifact_refs: list[dict[str, Any]] | None = None,
    ) -> ChildRunRecord:
        return self.update(
            record,
            status="completed",
            finished_at=_utcnow(),
            summary_ref=dict(summary_ref),
            artifact_refs=list(artifact_refs or record.artifact_refs),
            failure=None,
        )

    def mark_failed(
        self, record: ChildRunRecord, *, reason_code: str, detail: str | None = None
    ) -> ChildRunRecord:
        failure = {"reason_code": reason_code}
        if detail:
            failure["detail"] = detail[:500]
        return self.update(
            record,
            status="failed",
            finished_at=_utcnow(),
            failure=failure,
        )

    def mark_cancelled(
        self, record: ChildRunRecord, *, reason_code: str = "parent_cancelled"
    ) -> ChildRunRecord:
        return self.update(
            record,
            status="cancelled",
            finished_at=_utcnow(),
            failure={"reason_code": reason_code},
        )

    def cancel_outstanding(self, parent_run_id: str) -> list[ChildRunRecord]:
        cancelled: list[ChildRunRecord] = []
        for record in self.list(parent_run_id):
            if record.status in {"queued", "running"}:
                cancelled.append(self.mark_cancelled(record))
        return cancelled
