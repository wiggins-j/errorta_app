"""Child-run controller and first scripted worker for F042."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from .inbox import AsyncInbox
from .schema import ChildRunRecord
from .store import ChildRunStore


@dataclass(frozen=True)
class ScriptedChildWorker:
    """Deterministic child worker used by the first F042 slice and tests."""

    def run(self, *, task_kind: str, title: str, prompt: str, result: str | None = None) -> str:
        if result is not None and result.strip():
            return result.strip()
        task = title.strip() or task_kind
        body = prompt.strip()
        if body:
            return f"{task}: {body}"
        return f"{task}: completed"


class ChildRunController:
    def __init__(self, *, store: ChildRunStore, inbox: AsyncInbox) -> None:
        self._store = store
        self._inbox = inbox

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
        return self._store.create(
            parent_run_id=parent_run_id,
            member_id=member_id,
            task_kind=task_kind,
            title=title,
            prompt=prompt,
            worker_kind=worker_kind,
            metadata=metadata,
        )

    def start(self, record: ChildRunRecord) -> ChildRunRecord:
        if record.status == "running":
            return record
        return self._store.mark_running(record)

    def run_scripted(
        self,
        *,
        record: ChildRunRecord,
        prompt: str,
        result: str | None = None,
        artifact_refs: list[dict[str, Any]] | None = None,
    ) -> tuple[ChildRunRecord, dict[str, Any]]:
        running = self.start(record)
        summary = ScriptedChildWorker().run(
            task_kind=running.task_kind,
            title=running.title,
            prompt=prompt,
            result=result,
        )
        msg = self._inbox.append_payload(
            parent_run_id=running.parent_run_id,
            child_run_id=running.child_run_id,
            message_kind="summary",
            payload=summary,
            artifact_refs=artifact_refs,
            metadata={"task_kind": running.task_kind},
        )
        summary_ref = {
            "class_": "child_run_summary",
            "child_run_id": running.child_run_id,
            "message_id": msg.message_id,
            "content_sha256": msg.payload_sha256,
            "preview_sha256": hashlib.sha256(
                msg.payload_preview.encode("utf-8")
            ).hexdigest(),
            "payload_bytes": msg.payload_bytes,
            "payload_preview": msg.payload_preview,
        }
        return running, summary_ref

    def cancel_outstanding(self, *, parent_run_id: str) -> list[ChildRunRecord]:
        return self._store.cancel_outstanding(parent_run_id)
