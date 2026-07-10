"""Durable pause/resume/cancel + ask-decision state machine.

Built on top of the Phase 0 RunStore + the Phase 1 RunWriterToken surface
(Task 2). RunControl appends control events via a transient writer token
when the scheduler does not hold one (route layer); when the scheduler is
running, callers pass `scheduler_writer=` so control events serialize on
the scheduler's writer task.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from errorta_council.run_store import (
    RunStore,
    RunWriterToken,
    WriterAlreadyHeld,
)
from errorta_council.schema import (
    TERMINAL_RUN_STATUSES,
    CouncilEvent,
    EventStatus,
    EventType,
    RunMeta,
)

_DECISION_CHOICES = {"stop", "skip_member", "continue_local_only"}
_DECISION_SCOPES = {"current_turn", "current_round", "remainder_of_run"}


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DecisionNotApplicable(RuntimeError):
    """submit_decision was called while the run is not awaiting one.

    F031-09 — decisions only resolve an ask-pause; the route layer maps
    this to a 409.
    """


class TerminalRunError(RuntimeError):
    """Raised on control attempts against a terminal run. Maps to HTTP 409."""

    http_status = 409


class RunControl:
    """Per-run control surface: pause, resume, cancel, decision."""

    def __init__(
        self,
        *,
        run_store: RunStore,
        run_id: str,
        scheduler_writer: RunWriterToken | None = None,
    ) -> None:
        self._store = run_store
        self.run_id = run_id
        # When the scheduler hands us its writer token, control events flow
        # through it; otherwise we acquire transiently per call.
        self._scheduler_writer = scheduler_writer
        self._unpaused = asyncio.Event()
        meta, _ = self._store.read_run(run_id)
        if meta.status != "paused":
            self._unpaused.set()

    def _read_meta(self) -> RunMeta:
        return self._store.read_run(self.run_id)[0]

    def _assert_not_terminal(self, meta: RunMeta) -> None:
        if meta.status in TERMINAL_RUN_STATUSES:
            raise TerminalRunError(f"run {self.run_id} is terminal ({meta.status})")

    def _append_control_event(
        self,
        *,
        type: EventType,
        status: EventStatus,
        payload: dict,
        allow_meta_only_fallback: bool = False,
    ) -> CouncilEvent | None:
        if self._scheduler_writer is not None:
            return self._store.append_event(
                self.run_id, type=type, status=status, payload=payload,
                writer=self._scheduler_writer,
            )
        # No scheduler writer outstanding: acquire transiently.
        try:
            token = self._store.acquire_writer(self.run_id)
        except WriterAlreadyHeld as exc:
            if allow_meta_only_fallback:
                # Scheduler holds the writer; queue the event so the
                # scheduler drains it at its next checkpoint and the user's
                # control action shows up in the transcript (P1 — durable
                # control-event audit trail).
                self._store.push_pending_control_event(
                    self.run_id,
                    event_spec={
                        "type": type.value if hasattr(type, "value") else str(type),
                        "status": status.value if hasattr(status, "value") else str(status),
                        "payload": dict(payload),
                    },
                )
                return None
            raise RuntimeError(
                f"control event blocked: scheduler holds writer for {self.run_id}"
            ) from exc
        try:
            return self._store.append_event(
                self.run_id, type=type, status=status, payload=payload, writer=token,
            )
        finally:
            self._store.release_writer(token)

    async def request_pause(self, *, requested_by: str) -> RunMeta:
        meta = self._read_meta()
        self._assert_not_terminal(meta)
        if meta.status == "paused":
            return meta
        self._append_control_event(
            type=EventType.RUN_STATUS_CHANGED,
            status=EventStatus.PAUSED,
            payload={"status_change": "paused", "requested_by": requested_by},
            allow_meta_only_fallback=True,
        )
        new_meta = self._store.merge_meta_fields(
            self.run_id, status="paused", paused_at=_utcnow(),
        )
        self._unpaused.clear()
        return new_meta

    async def request_resume(self, *, requested_by: str) -> RunMeta:
        """F031-09: /resume resumes BOTH ``paused`` and ``awaiting_user_decision``.

        For ``awaiting_user_decision`` the resume is interpreted as
        ``continue_local_only`` — the user signals the scheduler to keep
        going with the current plan. The state-change event is emitted
        for both transitions so the audit trail is consistent.
        """
        meta = self._read_meta()
        self._assert_not_terminal(meta)
        if meta.status not in ("paused", "awaiting_user_decision"):
            return meta
        was_awaiting = meta.status == "awaiting_user_decision"
        self._append_control_event(
            type=EventType.RUN_STATUS_CHANGED,
            status=EventStatus.RESUMED,
            payload={
                "status_change": "running",
                "requested_by": requested_by,
                "from_status": meta.status,
            },
            allow_meta_only_fallback=True,
        )
        new_meta = self._store.merge_meta_fields(
            self.run_id, status="running", paused_at=None,
        )
        self._unpaused.set()
        if was_awaiting:
            # Surface a durable ``continue_local_only`` decision so the
            # scheduler's await_decision_or_cancelled() observes it on
            # its next poll. Recovery and audit see the same projection.
            decision = {
                "choice": "continue_local_only",
                "scope": "current_turn",
                "requested_by": requested_by,
                "at": _utcnow(),
            }
            new_meta = self._store.merge_meta_fields(
                self.run_id, last_decision=decision,
            )
        return new_meta

    async def request_cancel(
        self, *, requested_by: str, reason: str
    ) -> tuple[RunMeta, CouncilEvent | None]:
        meta = self._read_meta()
        self._assert_not_terminal(meta)
        if meta.cancel_requested_at is not None:
            _, events = self._store.read_run(self.run_id)
            for e in reversed(events):
                if e.type == EventType.RUN_CANCEL_REQUESTED:
                    return meta, e
        ev = self._append_control_event(
            type=EventType.RUN_CANCEL_REQUESTED,
            status=EventStatus.CANCEL_REQUESTED,
            payload={"requested_by": requested_by, "reason": reason},
            allow_meta_only_fallback=True,
        )
        new_meta = self._store.merge_meta_fields(
            self.run_id, cancel_requested_at=_utcnow(),
        )
        self._unpaused.set()
        return new_meta, ev

    async def submit_interjection(
        self, *, text: str, requested_by: str
    ) -> tuple[RunMeta, CouncilEvent | None]:
        """F049: append a live user message to a running (or paused) run.

        The message is a USER_INTERJECTION transcript event. If the scheduler
        holds the writer (the normal live case) it is queued in
        pending_control_events and the scheduler drains it at the top of its
        next turn loop — so it lands in the transcript BEFORE the next member
        builds its context. Read-once: members who already spoke are not re-run.
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("empty_interjection_text")
        meta = self._read_meta()
        self._assert_not_terminal(meta)
        ev = self._append_control_event(
            type=EventType.USER_INTERJECTION,
            status=EventStatus.COMPLETED,
            payload={"content": text, "author": "user", "requested_by": requested_by},
            allow_meta_only_fallback=True,
        )
        # Nudge the scheduler if it is parked on a pause/decision gate so the
        # interjection is picked up promptly rather than only on the next tick.
        self._unpaused.set()
        # Re-read so the returned meta reflects the just-appended/queued event,
        # not the pre-append snapshot.
        return self._read_meta(), ev

    async def submit_decision(
        self, *, choice: str, scope: str, requested_by: str
    ) -> tuple[RunMeta, CouncilEvent | None]:
        """F031-09 decision surface — durably projects into RunMeta.

        - Appends a RUN_STATUS_CHANGED event carrying {choice, scope, requested_by}.
        - Persists the decision into ``meta.last_decision`` via write_meta so a
          scheduler restart, the audit drawer, and recovery can all observe it.
        - choice="stop" additionally triggers cancellation (request_cancel
          semantics): meta.cancel_requested_at is set and the scheduler
          observes ``is_cancelled() == True`` at its next checkpoint.
        - choice="skip_member"/"continue_local_only" stay non-terminal; the
          scheduler reads last_decision and applies the scope on its next loop.
        """
        if choice not in _DECISION_CHOICES:
            raise ValueError(f"unknown_decision_choice: {choice}")
        if scope not in _DECISION_SCOPES:
            raise ValueError(f"unknown_decision_scope: {scope}")
        meta = self._read_meta()
        self._assert_not_terminal(meta)
        # F031-09: decisions only resolve an ask-pause. Reject any
        # decision submitted outside ``awaiting_user_decision``; otherwise
        # callers could inject ``stop`` mid-run and bypass the cancel
        # confirmation surface.
        if meta.status != "awaiting_user_decision":
            raise DecisionNotApplicable(
                f"submit_decision rejected: run is {meta.status!r}, "
                f"not awaiting_user_decision"
            )
        now = _utcnow()
        decision_payload = {
            "choice": choice, "scope": scope,
            "requested_by": requested_by, "at": now,
        }
        ev = self._append_control_event(
            type=EventType.RUN_STATUS_CHANGED,
            status=EventStatus.RUNNING,
            payload={
                "decision": {"choice": choice, "scope": scope},
                "requested_by": requested_by,
            },
            allow_meta_only_fallback=True,
        )
        # merge_meta_fields holds the per-run lock for read-then-write so the
        # scheduler's writer-token thread can't clobber last_decision /
        # cancel_requested_at between our read and our write.
        merge_updates: dict = {"last_decision": decision_payload}
        if choice == "stop":
            # Cancel semantics — scheduler observes cancel_requested_at and
            # emits a terminal RUN_CANCELLED at its next checkpoint.
            current = self._read_meta()
            merge_updates["cancel_requested_at"] = (
                current.cancel_requested_at or now
            )
        new_meta = self._store.merge_meta_fields(self.run_id, **merge_updates)
        if choice == "stop":
            self._unpaused.set()
        return new_meta, ev

    def is_cancelled(self) -> bool:
        return self._read_meta().cancel_requested_at is not None

    def is_paused(self) -> bool:
        return self._read_meta().status == "paused"

    async def await_unpaused_or_cancelled(
        self, *, poll_interval_seconds: float = 0.05,
    ) -> None:
        """Block until the run is either unpaused or cancelled.

        Polls the on-disk meta (P1 fix): a `RunControl` instance constructed
        in a FastAPI route handler is a different object than the
        scheduler's `RunControl`, so the route's `request_pause` cannot
        clear the scheduler's in-memory `asyncio.Event`. The scheduler must
        therefore observe the durable `meta.status` instead of a local
        event, otherwise route-issued pause silently does nothing.
        """
        if self.is_cancelled():
            return
        while self.is_paused():
            if self.is_cancelled():
                return
            await asyncio.sleep(poll_interval_seconds)

    async def await_decision_or_cancelled(
        self, *, poll_interval_seconds: float = 0.05,
    ) -> dict | None:
        """Block until a fresh decision is persisted or the run is cancelled.

        Returns the decision dict on success, or None when the run is being
        cancelled. The scheduler calls this from an "ask" branch where it has
        durably set ``status="awaiting_user_decision"``; once a decision is
        persisted via ``submit_decision``, the scheduler reads it, applies the
        scope, then calls ``clear_last_decision`` so the next ask can wait
        again. Polling cadence is small (50 ms) so behavior is responsive in
        tests without a notification surface across RunStore instances.
        """
        if self.is_cancelled():
            return None
        while True:
            meta = self._read_meta()
            if self.is_cancelled():
                return None
            if meta.last_decision is not None:
                return dict(meta.last_decision)
            await asyncio.sleep(poll_interval_seconds)

    def clear_last_decision(self) -> RunMeta:
        """Consume the persisted decision so the next ask can wait fresh."""
        return self._store.merge_meta_fields(self.run_id, last_decision=None)

    def enter_awaiting_user_decision(
        self,
        *,
        question_code: str,
        member_id: str | None = None,
        round: int | None = None,
    ) -> RunMeta:
        """Durably enter ``status="awaiting_user_decision"``.

        Called from the scheduler when ``policy.stop_behavior == "ask"`` and
        a checkpoint needs an explicit user decision. The corresponding
        RUN_STATUS_CHANGED event is emitted by the scheduler under its
        writer token; this helper only flips the meta projection so route
        consumers and recovery observe the durable state.
        """
        return self._store.merge_meta_fields(
            self.run_id,
            status="awaiting_user_decision",
        )

    def exit_awaiting_user_decision(self) -> RunMeta:
        """Restore ``status="running"`` after a decision is consumed."""
        return self._store.merge_meta_fields(self.run_id, status="running")
