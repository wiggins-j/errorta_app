"""F031-02 — Council run + event store (Phase 0).

Per spec OQ#3 resolution: metadata extension is ``.meta.json``.

Storage layout::

    ${ERRORTA_HOME}/council/runs/
      {run_id}.jsonl         # append-only event log, one JSON object per line
      {run_id}.meta.json     # mutable metadata, atomic temp+rename writes

One writer per run (invariant 2). Phase 0 enforces with a per-run
``threading.Lock``; Phase 1's scheduler replaces this with a single
asyncio writer task. Sequence numbers start at 1 and are assigned by the
store — callers cannot provide them.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import secrets
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .schema import (
    FORMAT_VERSION,
    TERMINAL_RUN_STATUSES,
    CouncilEvent,
    CouncilEventError,
    EventStatus,
    EventType,
    MemberSnapshot,
    RunMeta,
    RunSummary,
)


class RunNotFound(LookupError):
    pass


class TerminalRunRejected(RuntimeError):
    def __init__(self, run_id: str) -> None:
        super().__init__(f"run {run_id} is terminal; refusing to append")
        self.run_id = run_id


@dataclass(frozen=True)
class RunWriterToken:
    """Single-writer ownership token for a Council run (invariant 2).

    The scheduler acquires this once at run start and keeps it for the
    lifetime of the writer task. External callers (control routes,
    recovery) acquire-and-release transiently.

    Fields:
      run_id: the run this token authorizes writes to.
      token:  a cryptographically random secret; never logged or persisted.
    """

    run_id: str
    token: str


class WriterAlreadyHeld(RuntimeError):
    """Raised when acquire_writer is called for a run whose token is outstanding."""


class NotAuthorizedWriter(PermissionError):
    """Raised when append_event is called without a valid token for the run."""


_TERMINAL_EVENT_TYPES = {
    EventType.RUN_COMPLETED, EventType.RUN_CANCELLED, EventType.RUN_FAILED,
}


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_terminal_reason(ev: "CouncilEvent") -> str | None:
    """Read a terminal reason from an event payload.

    The vocabulary is split across emitters:
    - ``scheduler._emit_terminal()`` uses ``payload["reason"]``;
    - ``fake_run.run_fake_council()`` uses ``payload["terminal_reason"]``.

    Both are valid Phase 0/1 emitters; surface whichever is present.
    """
    payload = ev.payload or {}
    return payload.get("terminal_reason") or payload.get("reason")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    # Unique tmp per write so concurrent writers (e.g. scheduler thread +
    # route-layer transient writer) don't collide on the same tmp file name.
    tmp = path.with_suffix(f"{path.suffix}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    fd = os.open(str(tmp), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_WRITERS_GUARD = threading.Lock()
_WRITERS: dict[tuple[str, str], str] = {}


class RunStore:
    def __init__(self, runs_dir: Path) -> None:
        self._runs_dir = runs_dir
        # Per-instance views over PROCESS-GLOBAL lock + writer registries —
        # keyed on (runs_dir, run_id) so two RunStore instances pointing at
        # the same on-disk dir share the same lock/writer state. Without
        # this, FastAPI route handlers (which each construct a fresh
        # RunStore) and the scheduler's daemon thread (which holds a
        # different RunStore instance) operate against disjoint locks,
        # making merge_meta_fields atomicity worthless and clobbering
        # control-state projections like last_decision (P2 follow-up).
        self._runs_key = str(runs_dir.resolve())

    def _writers_key(self, run_id: str) -> tuple[str, str]:
        return (self._runs_key, run_id)

    def acquire_writer(self, run_id: str) -> RunWriterToken:
        """Reserve single-writer ownership of `run_id`'s event log."""
        key = self._writers_key(run_id)
        with _WRITERS_GUARD:
            if key in _WRITERS:
                raise WriterAlreadyHeld(
                    f"writer_already_held: run_id={run_id}"
                )
            token_value = secrets.token_hex(16)
            _WRITERS[key] = token_value
            return RunWriterToken(run_id=run_id, token=token_value)

    def release_writer(self, token: RunWriterToken) -> None:
        """Release the writer reservation. No-op if the token is already stale."""
        key = self._writers_key(token.run_id)
        with _WRITERS_GUARD:
            current = _WRITERS.get(key)
            if current is not None and current == token.token:
                del _WRITERS[key]

    def _check_writer(self, run_id: str, writer: RunWriterToken | None) -> None:
        if writer is None:
            raise NotAuthorizedWriter(
                f"missing_writer_token: run_id={run_id}"
            )
        if writer.run_id != run_id:
            raise NotAuthorizedWriter(
                f"writer_token_run_id_mismatch: "
                f"token={writer.run_id} != target={run_id}"
            )
        key = self._writers_key(run_id)
        with _WRITERS_GUARD:
            current = _WRITERS.get(key)
            if current is None or current != writer.token:
                raise NotAuthorizedWriter(
                    f"writer_token_invalid_or_released: run_id={run_id}"
                )

    def write_meta(self, meta: RunMeta) -> None:
        """Atomically overwrite the meta JSON without appending an event.

        Used by RunControl (Task 3) to durably reflect paused / cancel_requested
        substate in the meta cache that list_runs reads. The immutable
        transition is still recorded in the event log; this method only updates
        the cached projection.
        """
        _atomic_write_json(self._meta_path(meta.id), meta.to_dict())

    def push_pending_control_event(
        self,
        run_id: str,
        *,
        event_spec: dict[str, Any],
    ) -> RunMeta:
        """Atomically append a control-event spec to meta.pending_control_events.

        Called from the route layer when ``RunControl`` cannot acquire the
        writer token because the scheduler thread holds it. The scheduler
        drains the queue at each checkpoint, emitting each entry as a real
        event under its writer (P1 — durable control-event audit trail).
        """
        from dataclasses import replace as _replace
        with self._lock(run_id):
            meta = self._load_meta(run_id)
            pending = list(meta.pending_control_events or [])
            pending.append(dict(event_spec))
            new_meta = _replace(meta, pending_control_events=pending)
            _atomic_write_json(self._meta_path(meta.id), new_meta.to_dict())
            return new_meta

    def pop_pending_control_events(self, run_id: str) -> list[dict[str, Any]]:
        """Atomically read-and-clear the pending control event queue.

        Called from the scheduler thread at each checkpoint. The returned
        list MUST be drained outside the lock (each entry becomes a real
        append_event call, which re-takes the per-run lock).
        """
        from dataclasses import replace as _replace
        with self._lock(run_id):
            meta = self._load_meta(run_id)
            pending = list(meta.pending_control_events or [])
            if not pending:
                return []
            new_meta = _replace(meta, pending_control_events=[])
            _atomic_write_json(self._meta_path(meta.id), new_meta.to_dict())
            return pending

    def merge_meta_fields(self, run_id: str, **updates: Any) -> RunMeta:
        """Lock-serialized read-then-merge-then-write of meta-only fields.

        Avoids races between an external writer (e.g. RunControl in a route
        handler) and the scheduler's writer-token thread, both of which can
        otherwise read-then-write meta independently and clobber each
        other's changes (e.g. last_decision vanishing right after a turn
        completes). Holds the per-run lock for the full read-merge-write.
        """
        from dataclasses import replace as _replace
        with self._lock(run_id):
            meta = self._load_meta(run_id)
            new_meta = _replace(meta, **updates)
            _atomic_write_json(self._meta_path(meta.id), new_meta.to_dict())
            return new_meta

    def _lock(self, run_id: str) -> threading.Lock:
        key = (self._runs_key, run_id)
        with _LOCKS_GUARD:
            lock = _LOCKS.get(key)
            if lock is None:
                lock = threading.Lock()
                _LOCKS[key] = lock
            return lock

    # ---- paths ------------------------------------------------------------

    def _meta_path(self, run_id: str) -> Path:
        return self._runs_dir / f"{run_id}.meta.json"

    def _log_path(self, run_id: str) -> Path:
        return self._runs_dir / f"{run_id}.jsonl"

    @property
    def runs_dir(self) -> Path:
        """Root directory for run-scoped side artifacts."""
        return self._runs_dir

    # ---- writes -----------------------------------------------------------

    def create_run(
        self,
        *,
        run_id: str | None = None,
        room_id: str,
        room_snapshot: dict[str, Any],
        prompt: str,
        corpus_ids: list[str],
        conversation_id: str | None = None,
        conversation_turn_id: str | None = None,
    ) -> RunMeta:
        rid = run_id or str(uuid.uuid4())
        meta = RunMeta(
            format_version=FORMAT_VERSION,
            id=rid, room_id=room_id, room_snapshot=dict(room_snapshot),
            conversation_id=conversation_id,
            conversation_turn_id=conversation_turn_id,
            prompt=prompt, corpus_ids=list(corpus_ids),
            status="created",
            created_at=_now(), started_at=None,
            updated_at=_now(), finished_at=None,
            last_sequence=0, event_count=0, terminal_event_id=None,
            resume_policy="mark_interrupted",
            costs={"remote_calls": 0, "local_calls": 0,
                   "input_tokens": 0, "output_tokens": 0, "estimated_usd": 0.0},
            capabilities={"streaming": False, "fake_members": True},
        )
        _atomic_write_json(self._meta_path(rid), meta.to_dict())
        return meta

    def append_event(
        self,
        run_id: str,
        *,
        type: EventType,
        status: EventStatus,
        payload: dict[str, Any],
        member_id: str | None = None,
        member_snapshot: MemberSnapshot | None = None,
        round: int | None = None,
        turn_index: int | None = None,
        parent_event_ids: list[str] | None = None,
        usage: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        audit: dict[str, Any] | None = None,
        error: CouncilEventError | None = None,
        writer: RunWriterToken | None = None,
    ) -> CouncilEvent:
        self._check_writer(run_id, writer)
        with self._lock(run_id):
            meta = self._load_meta(run_id)
            if meta.status in TERMINAL_RUN_STATUSES:
                raise TerminalRunRejected(run_id)
            seq = meta.last_sequence + 1
            ev = CouncilEvent(
                format_version=FORMAT_VERSION,
                id=str(uuid.uuid4()), run_id=run_id, sequence=seq,
                type=type, status=status, created_at=_now(),
                payload=dict(payload),
                member_id=member_id, member_snapshot=member_snapshot,
                round=round, turn_index=turn_index,
                parent_event_ids=list(parent_event_ids or []),
                usage=dict(usage) if usage is not None else None,
                context=dict(context) if context is not None else None,
                audit=dict(audit) if audit is not None else None,
                error=error,
            )
            self._append_line(run_id, ev)
            self._update_meta_after(meta, ev)
        return ev

    def _append_line(self, run_id: str, ev: CouncilEvent) -> None:
        log = self._log_path(run_id)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev.to_dict(), sort_keys=True))
            fh.write("\n")
            fh.flush()
            if ev.type in _TERMINAL_EVENT_TYPES or ev.type == EventType.RUN_CANCEL_REQUESTED:
                os.fsync(fh.fileno())

    def _update_meta_after(self, meta: RunMeta, ev: CouncilEvent) -> RunMeta:
        new_status = meta.status
        finished_at = meta.finished_at
        terminal_event_id = meta.terminal_event_id
        started_at = meta.started_at
        terminal_reason = meta.terminal_reason
        cancel_requested_at = meta.cancel_requested_at

        if ev.type == EventType.RUN_STARTED:
            new_status = "running"
            started_at = ev.created_at
        elif ev.type == EventType.RUN_COMPLETED:
            new_status = "completed"
            finished_at = ev.created_at
            terminal_event_id = ev.id
            terminal_reason = _read_terminal_reason(ev) or terminal_reason
        elif ev.type == EventType.RUN_CANCELLED:
            new_status = "cancelled"
            finished_at = ev.created_at
            terminal_event_id = ev.id
            terminal_reason = _read_terminal_reason(ev) or terminal_reason or "cancelled"
        elif ev.type == EventType.RUN_FAILED:
            new_status = "failed"
            finished_at = ev.created_at
            terminal_event_id = ev.id
            terminal_reason = _read_terminal_reason(ev) or terminal_reason or "failed"
        elif ev.type == EventType.RUN_CANCEL_REQUESTED:
            # Cancel requested but not yet terminal. Record the request
            # timestamp so polling consumers see the intent durably
            # (F031-09 — durable control-state projection).
            new_status = "running"
            cancel_requested_at = cancel_requested_at or ev.created_at

        # Phase 1 counter projection (P2): keep RunMeta in sync with the
        # event log so completed_messages_by_member / total_messages_completed
        # do not stay at zero after fake or real runs.
        completed_by_member = dict(meta.completed_messages_by_member)
        total_messages_completed = meta.total_messages_completed
        if (
            ev.type == EventType.MEMBER_MESSAGE
            and ev.member_id is not None
            # F037: callout answers are MEMBER_MESSAGE events but must not
            # advance deliberation meters (mirrors CounterRebuilder.from_events).
            and not (ev.payload or {}).get("is_callout")
        ):
            completed_by_member[ev.member_id] = (
                completed_by_member.get(ev.member_id, 0) + 1
            )
            total_messages_completed += 1

        updated = RunMeta(
            **{
                **meta.to_dict(),
                "status": new_status,
                "started_at": started_at,
                "updated_at": ev.created_at,
                "finished_at": finished_at,
                "last_sequence": ev.sequence,
                "event_count": meta.event_count + 1,
                "terminal_event_id": terminal_event_id,
                "completed_messages_by_member": completed_by_member,
                "total_messages_completed": total_messages_completed,
                "terminal_reason": terminal_reason,
                "cancel_requested_at": cancel_requested_at,
            }
        )
        _atomic_write_json(self._meta_path(meta.id), updated.to_dict())
        return updated

    def cancel_run(
        self, run_id: str, *, requested_by: str, reason: str
    ) -> tuple[RunMeta, CouncilEvent]:
        # Caller may be the route layer (no scheduler writer outstanding) OR
        # the scheduler itself. Acquire transiently when no writer is held;
        # otherwise rely on the caller passing through RunControl (Task 3).
        try:
            token = self.acquire_writer(run_id)
        except WriterAlreadyHeld:
            raise PermissionError(
                f"cancel_run called while scheduler writer holds run_id={run_id}; "
                "use RunControl.request_cancel from the route layer instead"
            )
        try:
            ev = self.append_event(
                run_id,
                type=EventType.RUN_CANCEL_REQUESTED,
                status=EventStatus.CANCEL_REQUESTED,
                payload={"requested_by": requested_by, "reason": reason,
                         "in_flight_event_id": None},
                writer=token,
            )
        finally:
            self.release_writer(token)
        meta = self._load_meta(run_id)
        return meta, ev

    # ---- reads ------------------------------------------------------------

    def _load_meta(self, run_id: str) -> RunMeta:
        path = self._meta_path(run_id)
        if not path.exists():
            raise RunNotFound(run_id)
        return RunMeta.from_dict(json.loads(path.read_text()))

    def read_run(self, run_id: str) -> tuple[RunMeta, list[CouncilEvent]]:
        meta = self._load_meta(run_id)
        events: list[CouncilEvent] = []
        log = self._log_path(run_id)
        if log.exists():
            for line in log.read_text().splitlines():
                if not line.strip():
                    continue
                events.append(CouncilEvent.from_dict(json.loads(line)))
        events.sort(key=lambda e: e.sequence)
        return meta, events

    def list_run_ids(self) -> list[str]:
        """Return all run ids visible under the runs dir."""
        if not self._runs_dir.exists():
            return []
        out: list[str] = []
        for child in self._runs_dir.iterdir():
            if child.is_file() and child.name.endswith(".meta.json"):
                out.append(child.name[: -len(".meta.json")])
        return out

    def list_runs(
        self,
        *,
        room_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RunSummary]:
        summaries: list[RunSummary] = []
        for child in sorted(self._runs_dir.iterdir()):
            if not child.is_file() or not child.name.endswith(".meta.json"):
                continue
            try:
                meta = RunMeta.from_dict(json.loads(child.read_text()))
            except Exception:
                continue
            if room_id and meta.room_id != room_id:
                continue
            if status and meta.status != status:
                continue
            summaries.append(RunSummary(
                id=meta.id, room_id=meta.room_id, status=meta.status,
                updated_at=meta.updated_at, event_count=meta.event_count,
                last_sequence=meta.last_sequence,
            ))
        summaries.sort(key=lambda s: s.updated_at, reverse=True)
        return summaries[offset:offset + limit]
