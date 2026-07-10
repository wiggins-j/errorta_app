"""Durable callout queue — a per-run sidecar JSON file.

The route layer enqueues a user-requested callout (it does not hold the
scheduler's writer token); the scheduler drains ``requested`` records at its
loop checkpoint, runs admission, and updates each record's state. Approval
decisions from the approve/reject routes are written back onto the record so
the awaiting scheduler observes them.

State machine:
    requested -> (admission) -> rejected | admitted | awaiting_approval
    awaiting_approval -> (approve/reject route) -> approved | rejected
    approved/admitted -> (scheduler executes) -> started -> completed | failed

Records are immutable history within the file (we overwrite the whole list
atomically on each update). Low frequency — a user clicking "Ask expert" —
so a coarse atomic-write strategy is sufficient.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Per-file locks. The route threads (enqueue/update via approve/reject) and the
# scheduler daemon thread (drain → update) are all in one process, so a
# threading.Lock keyed on the queue file path serializes the otherwise-racy
# load→mutate→atomic-write cycle. Mirrors RunStore's _LOCKS pattern.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(path)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[path] = lock
        return lock


@dataclass
class CalloutRecord:
    callout_id: str
    target_id: str
    reason_code: str
    question: str
    requested_by: dict[str, Any]
    state: str = "requested"
    advisory: bool = True
    created_at: str = ""
    # set by approve/reject routes; consumed by the scheduler while awaiting
    approval: str | None = None        # None | "approved" | "rejected"
    reject_reason: str | None = None
    answer_event_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CalloutRecord":
        fields = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in fields})


def _atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class CalloutQueue:
    def __init__(self, *, runs_dir: Path, run_id: str) -> None:
        self._path = Path(runs_dir) / f"{run_id}.callouts.json"
        self._lock = _lock_for(str(self._path.resolve()))

    def _load(self) -> list[CalloutRecord]:
        if not self._path.exists():
            return []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [CalloutRecord.from_dict(r) for r in raw or []]

    def _save(self, records: list[CalloutRecord]) -> None:
        _atomic_write(self._path, [r.to_dict() for r in records])

    def enqueue(self, record: CalloutRecord) -> None:
        with self._lock:
            records = self._load()
            records.append(record)
            self._save(records)

    def list(self) -> list[CalloutRecord]:
        return self._load()

    def get(self, callout_id: str) -> CalloutRecord | None:
        for r in self._load():
            if r.callout_id == callout_id:
                return r
        return None

    def requested(self) -> list[CalloutRecord]:
        return [r for r in self._load() if r.state == "requested"]

    def update(self, callout_id: str, **fields: Any) -> CalloutRecord | None:
        with self._lock:
            records = self._load()
            updated: CalloutRecord | None = None
            for r in records:
                if r.callout_id == callout_id:
                    for k, v in fields.items():
                        setattr(r, k, v)
                    updated = r
            if updated is not None:
                self._save(records)
            return updated
