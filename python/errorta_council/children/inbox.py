"""Append-only async inbox from child runs to their parent run."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from .schema import FORMAT_VERSION, ChildRunMessage

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
DEFAULT_PREVIEW_BYTES = 2048


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _assert_safe_id(value: str, *, name: str) -> None:
    if not value or not _SAFE_ID_RE.match(value) or ".." in value:
        raise ValueError(f"unsafe_{name}")


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _preview(data: bytes, *, max_preview_bytes: int) -> str:
    clipped = data[:max_preview_bytes]
    text = clipped.decode("utf-8", errors="replace")
    if len(data) > max_preview_bytes:
        text += "\n[truncated]"
    return text


class AsyncInbox:
    """Append-only message log keyed by parent and child run ids."""

    def __init__(self, *, runs_dir: Path, max_preview_bytes: int = DEFAULT_PREVIEW_BYTES) -> None:
        self._root = Path(runs_dir) / "child-inbox"
        self._max_preview_bytes = max_preview_bytes

    def _child_dir(self, parent_run_id: str, child_run_id: str) -> Path:
        _assert_safe_id(parent_run_id, name="parent_run_id")
        _assert_safe_id(child_run_id, name="child_run_id")
        return self._root / parent_run_id / child_run_id

    def _path(self, parent_run_id: str, child_run_id: str) -> Path:
        return self._child_dir(parent_run_id, child_run_id) / "messages.jsonl"

    def append_payload(
        self,
        *,
        parent_run_id: str,
        child_run_id: str,
        message_kind: str,
        payload: str | bytes,
        artifact_refs: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ChildRunMessage:
        data = payload if isinstance(payload, bytes) else payload.encode("utf-8")
        message = ChildRunMessage(
            format_version=FORMAT_VERSION,
            message_id="crm-" + uuid.uuid4().hex[:16],
            parent_run_id=parent_run_id,
            child_run_id=child_run_id,
            message_kind=message_kind,
            payload_preview=_preview(data, max_preview_bytes=self._max_preview_bytes),
            payload_sha256=_sha_bytes(data),
            payload_bytes=len(data),
            created_at=_utcnow(),
            artifact_refs=list(artifact_refs or []),
            metadata=dict(metadata or {}),
        )
        self.append(message)
        return message

    def append(self, message: ChildRunMessage) -> ChildRunMessage:
        path = self._path(message.parent_run_id, message.child_run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(message.to_dict(), sort_keys=True, separators=(",", ":"))
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        return message

    def list(self, parent_run_id: str, child_run_id: str) -> list[ChildRunMessage]:
        path = self._path(parent_run_id, child_run_id)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        messages: list[ChildRunMessage] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                messages.append(ChildRunMessage.from_dict(json.loads(line)))
            except Exception:
                continue
        return messages
