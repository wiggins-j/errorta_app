"""F031-08 Phase 2 read-only audit subset.

Composes ``RunStore.read_run()`` event lists + ``RunMeta`` into UI-safe
view models. Phase 0/1 event-store data only — no manifests, no F030
audit records, no redaction artifacts. The Phase 3 ``inspection.py``
supersedes the manifest-needing fields.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .run_store import RunStore
from .schema import CouncilEvent, EventType, RunMeta


@dataclass(frozen=True)
class TurnAuditRow:
    turn_id: str
    sequence: int
    member_id: str
    member_name: str
    role: str
    destination_scope: str   # "local" | "fake"
    status: str
    reason_code: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class RunAuditTotals:
    turns: int = 0
    completed: int = 0
    blocked: int = 0
    skipped: int = 0
    cancelled: int = 0
    failed: int = 0
    local_calls: int = 0
    fake_calls: int = 0
    remote_calls: int = 0


@dataclass(frozen=True)
class RunAuditSummary:
    run_id: str
    status: str
    residency_owner: str   # "local" | "ssh_remote" | "cloud"
    totals: RunAuditTotals
    terminal_reason: str | None = None
    paused_at: str | None = None
    cancel_requested_at: str | None = None
    turns: list[TurnAuditRow] = field(default_factory=list)


@dataclass(frozen=True)
class TurnOverview:
    run_id: str
    turn_id: str
    member: str
    round: int | None
    sequence: int
    topology_label: str
    status: str
    destination_scope: str   # "local" | "fake"
    reason_code: str | None = None


@dataclass(frozen=True)
class TurnAfter:
    finish_reason: str | None = None
    output_appended: bool = False
    usage: dict[str, Any] | None = None
    terminal_reason: str | None = None


def _destination_scope(snapshot: dict[str, Any] | None) -> str:
    if not snapshot:
        return "local"
    locality = snapshot.get("locality") or snapshot.get("provider", "")
    if locality == "fake":
        return "fake"
    return "local"


def _resolve_residency() -> str:
    try:
        from errorta_residency import config as residency_config
        state = residency_config.load()
        return getattr(state, "mode", "local") or "local"
    except Exception:
        return "local"


def build_run_audit_summary(store: RunStore, run_id: str) -> RunAuditSummary:
    meta, events = store.read_run(run_id)
    totals = _aggregate_totals(events)
    turns = _turns_from_events(events)
    return RunAuditSummary(
        run_id=meta.id,
        status=meta.status,
        residency_owner=_resolve_residency(),
        totals=totals,
        terminal_reason=meta.terminal_reason,
        paused_at=meta.paused_at,
        cancel_requested_at=meta.cancel_requested_at,
        turns=turns,
    )


def build_turn_audit(
    store: RunStore, run_id: str, turn_id: str
) -> tuple[TurnOverview, TurnAfter]:
    meta, events = store.read_run(run_id)
    target = next((e for e in events if e.id == turn_id), None)
    if target is None:
        raise KeyError(f"turn_not_found: {turn_id}")
    snapshot = _snapshot_dict(target.member_snapshot)
    overview = TurnOverview(
        run_id=run_id,
        turn_id=turn_id,
        member=(snapshot or {}).get("name", target.member_id or ""),
        round=target.round,
        sequence=target.sequence,
        topology_label=str((meta.room_snapshot or {}).get("topology_kind", "")),
        status=target.status.value if hasattr(target.status, "value") else str(target.status),
        destination_scope=_destination_scope(snapshot),
        reason_code=(target.payload or {}).get("reason"),
    )
    after = TurnAfter(
        finish_reason=(target.payload or {}).get("finish_reason"),
        output_appended=(target.type == EventType.MEMBER_MESSAGE),
        usage=target.usage,
        terminal_reason=meta.terminal_reason,
    )
    return overview, after


def _aggregate_totals(events: list[CouncilEvent]) -> RunAuditTotals:
    completed = blocked = skipped = cancelled = failed = 0
    local_calls = fake_calls = remote_calls = 0
    turns = 0
    for ev in events:
        ev_status = ev.status.value if hasattr(ev.status, "value") else str(ev.status)
        if ev.type == EventType.MEMBER_MESSAGE:
            completed += 1
            turns += 1
            scope = _destination_scope(_snapshot_dict(ev.member_snapshot))
            if scope == "fake":
                fake_calls += 1
            else:
                local_calls += 1
        elif ev.type == EventType.MEMBER_SKIPPED:
            turns += 1
            # P1: a skip with status=BLOCKED is a blocked turn — branch on
            # event status, not on a payload key.
            if ev_status == "blocked" or (ev.payload or {}).get("blocked"):
                blocked += 1
            else:
                skipped += 1
        elif ev.type == EventType.MEMBER_FAILED:
            failed += 1
            turns += 1
        elif ev.type == EventType.MEMBER_CANCELLED:
            cancelled += 1
            turns += 1
    return RunAuditTotals(
        turns=turns,
        completed=completed,
        blocked=blocked,
        skipped=skipped,
        cancelled=cancelled,
        failed=failed,
        local_calls=local_calls,
        fake_calls=fake_calls,
        remote_calls=remote_calls,
    )


def _turns_from_events(events: list[CouncilEvent]) -> list[TurnAuditRow]:
    out: list[TurnAuditRow] = []
    for ev in events:
        if ev.type not in (
            EventType.MEMBER_MESSAGE,
            EventType.MEMBER_SKIPPED,
            EventType.MEMBER_FAILED,
            EventType.MEMBER_CANCELLED,
        ):
            continue
        snapshot = _snapshot_dict(ev.member_snapshot) or {}
        usage = ev.usage or {}
        out.append(TurnAuditRow(
            turn_id=ev.id,
            sequence=ev.sequence,
            member_id=ev.member_id or "",
            member_name=snapshot.get("name", ev.member_id or ""),
            role=snapshot.get("role", ""),
            destination_scope=_destination_scope(snapshot),
            status=ev.status.value if hasattr(ev.status, "value") else str(ev.status),
            reason_code=(ev.payload or {}).get("reason"),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
        ))
    return out


def _snapshot_dict(snapshot) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    if isinstance(snapshot, dict):
        return snapshot
    try:
        return {
            "name": snapshot.name,
            "role": snapshot.role,
            "locality": snapshot.locality,
            "context_access": snapshot.context_access,
            "transcript_access": snapshot.transcript_access,
            "provider_display": snapshot.provider_display,
            "model_display": snapshot.model_display,
        }
    except AttributeError:
        return None
