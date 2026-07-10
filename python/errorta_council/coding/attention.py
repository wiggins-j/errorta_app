"""F117 — Coding Team attention signals (the primitive + store).

One record, two faces: a **Problem** (showstopper) or an **Alert** (advisory).
Persisted append-only in ``signals.jsonl`` next to the rest of a project's ledger,
projected latest-per-id on read. Every create/transition also records a ledger
**decision** (``LedgerStore.record_decision``) so it surfaces in the Team Log —
``team_log.py`` is a read-only projection with no emit API.

A user correction is realized by the PM **creating a new ledger task**
(``add_task(role="pm")`` + ``update_task(..., source_signal_id=…)`` so the link
rides in the task's ``_extras``); the signal stores ``resolution.created_task_id``.

This module is the sole owner of the signal lifecycle. It exposes importable
helpers (``list_open``, ``blocks_stage``) so in-process callers — the governance
blocking gate and the F118 Director — don't go through HTTP.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from errorta_council.coding.ledger import (
    LedgerStore,
    _append_jsonl,
    _now,
    _read_jsonl,
)

KINDS = ("problem", "alert")
TERMINAL_STATES = (
    "accepted", "corrected", "deferred", "dismissed", "auto_resolved", "superseded",
)
# Actions a user may take, by kind (spec §"Actions by kind").
VALID_ACTIONS: dict[str, frozenset[str]] = {
    "problem": frozenset({"accept", "correct"}),
    "alert": frozenset({"accept", "correct", "defer", "dismiss"}),
}
# These Problems describe operator-owned recovery, not implementation work.
# Accepting one acknowledges the blocker and must not create a PM backlog task;
# doing so would manufacture another completion blocker that no scheduler can run.
_CONFIGURATION_PROBLEM_SOURCES = frozenset({
    "member_health", "worker_unproductive", "completion_blocked",
})


class AttentionError(ValueError):
    """Raised on an invalid signal create or an illegal transition (fail-loud)."""


@dataclass(frozen=True)
class AttentionSignal:
    id: str
    project_id: str
    kind: str                       # "problem" | "alert"
    blocking: bool
    source: str                     # "monitor" | "pm" | "reviewer" | "member:<id>"
    stage: str                      # a GovernancePhase literal
    title: str
    summary: str
    pm_evaluation: str | None = None
    suggestions: list[dict[str, Any]] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    state: str = "open"
    resolution: dict[str, Any] | None = None
    created_at: str = ""
    updated_at: str = ""
    audit: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "project_id": self.project_id, "kind": self.kind,
            "blocking": self.blocking, "source": self.source, "stage": self.stage,
            "title": self.title, "summary": self.summary,
            "pm_evaluation": self.pm_evaluation, "suggestions": self.suggestions,
            "context": self.context, "state": self.state,
            "resolution": self.resolution, "created_at": self.created_at,
            "updated_at": self.updated_at, "audit": self.audit,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AttentionSignal:
        return cls(
            id=raw["id"], project_id=raw["project_id"], kind=raw["kind"],
            blocking=bool(raw.get("blocking", False)), source=raw.get("source", ""),
            stage=raw.get("stage", ""), title=raw.get("title", ""),
            summary=raw.get("summary", ""), pm_evaluation=raw.get("pm_evaluation"),
            suggestions=list(raw.get("suggestions") or []),
            context=dict(raw.get("context") or {}), state=raw.get("state", "open"),
            resolution=raw.get("resolution"),
            created_at=raw.get("created_at", ""), updated_at=raw.get("updated_at", ""),
            audit=list(raw.get("audit") or []),
        )


def _signals_path(store: LedgerStore):
    return store.dir / "signals.jsonl"


def _project(store: LedgerStore) -> dict[str, AttentionSignal]:
    """Replay ``signals.jsonl`` to the latest record per id (missing file → {})."""
    latest: dict[str, AttentionSignal] = {}
    for raw in _read_jsonl(_signals_path(store)):
        sig = AttentionSignal.from_dict(raw)
        latest[sig.id] = sig
    return latest


def _store(project_id: str) -> LedgerStore:
    return LedgerStore(project_id)


def _team_log_decision(store: LedgerStore, sig: AttentionSignal, what: str) -> None:
    """Surface a signal transition in the Team Log via a ledger decision."""
    store.record_decision(
        title=f"Attention {sig.kind}: {sig.title}",
        context=f"signal {sig.id} ({sig.source}, stage={sig.stage})",
        choice=what,
        rationale=sig.summary,
        related_task_ids=[sig.resolution["created_task_id"]]
        if sig.resolution and sig.resolution.get("created_task_id") else [],
        extra={"attention_signal_id": sig.id, "attention_state": sig.state},
    )


# --------------------------------------------------------------------------- #
# Create
# --------------------------------------------------------------------------- #
def raise_signal(
    project_id: str,
    *,
    kind: str,
    source: str,
    stage: str,
    title: str,
    summary: str,
    pm_evaluation: str | None = None,
    suggestions: list[dict[str, Any]] | None = None,
    context: dict[str, Any] | None = None,
    blocking: bool | None = None,
    store: LedgerStore | None = None,
) -> AttentionSignal:
    """Create an open signal. Problems are fail-closed: a Problem MUST carry a
    non-empty ``pm_evaluation`` and at least one suggestion (criterion #2)."""
    if kind not in KINDS:
        raise AttentionError(f"invalid kind: {kind!r}")
    suggestions = list(suggestions or [])
    if kind == "problem":
        if not (pm_evaluation and pm_evaluation.strip()):
            raise AttentionError("a problem requires a non-empty pm_evaluation")
        if not suggestions:
            raise AttentionError("a problem requires at least one suggestion")
    if blocking is None:
        blocking = kind == "problem"
    store = store or _store(project_id)
    now = _now()
    sig = AttentionSignal(
        id=f"sig-{uuid.uuid4().hex[:12]}", project_id=project_id, kind=kind,
        blocking=bool(blocking), source=source, stage=stage, title=title,
        summary=summary, pm_evaluation=pm_evaluation, suggestions=suggestions,
        context=dict(context or {}), state="open", resolution=None,
        created_at=now, updated_at=now,
        audit=[{"at": now, "to": "open", "by": source}],
    )
    with store.lock:
        _append_jsonl(_signals_path(store), sig.to_dict())
    _team_log_decision(store, sig, "raised")
    return sig


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #
def list_all(
    project_id: str, *, state: str | None = None, kind: str | None = None,
    store: LedgerStore | None = None,
) -> list[AttentionSignal]:
    store = store or _store(project_id)
    out = list(_project(store).values())
    if state is not None:
        out = [s for s in out if s.state == state]
    if kind is not None:
        out = [s for s in out if s.kind == kind]
    out.sort(key=lambda s: s.created_at)
    return out


def list_open(project_id: str, *, store: LedgerStore | None = None) -> list[AttentionSignal]:
    return list_all(project_id, state="open", store=store)


def blocks_stage(project_id: str, phase: str, *, store: LedgerStore | None = None) -> bool:
    """True iff an open, blocking signal exists for ``phase`` — the predicate the
    governance blocking gate consults."""
    return any(
        s.blocking and s.stage == phase for s in list_open(project_id, store=store)
    )


def get(
    project_id: str, signal_id: str, *, store: LedgerStore | None = None,
) -> AttentionSignal | None:
    store = store or _store(project_id)
    return _project(store).get(signal_id)


# --------------------------------------------------------------------------- #
# Transition
# --------------------------------------------------------------------------- #
def _suggestion_text(sig: AttentionSignal, suggestion_id: str | None) -> str:
    for s in sig.suggestions:
        if s.get("id") == suggestion_id:
            return s.get("detail") or s.get("label") or ""
    # default: first suggestion
    if sig.suggestions:
        s = sig.suggestions[0]
        return s.get("detail") or s.get("label") or ""
    return ""


def _write_resolution(
    store: LedgerStore, sig: AttentionSignal, *, state: str, action: str,
    by: str, suggestion_id: str | None = None, correction_text: str | None = None,
    created_task_id: str | None = None,
) -> AttentionSignal:
    now = _now()
    resolution = {
        "action": action, "by": by, "suggestion_id": suggestion_id,
        "correction_text": correction_text, "created_task_id": created_task_id,
        "decided_at": now,
    }
    audit = list(sig.audit) + [{"at": now, "from": sig.state, "to": state, "action": action}]
    updated = AttentionSignal(
        id=sig.id, project_id=sig.project_id, kind=sig.kind, blocking=sig.blocking,
        source=sig.source, stage=sig.stage, title=sig.title, summary=sig.summary,
        pm_evaluation=sig.pm_evaluation, suggestions=sig.suggestions,
        context=sig.context, state=state, resolution=resolution,
        created_at=sig.created_at, updated_at=now, audit=audit,
    )
    with store.lock:
        _append_jsonl(_signals_path(store), updated.to_dict())
    _team_log_decision(store, updated, f"{action} → {state}")
    return updated


def _make_task(store: LedgerStore, sig: AttentionSignal, detail: str) -> str:
    """The resolution → task loop: a PM-role ledger task linked back to the signal
    via ``_extras.source_signal_id`` (no first-class Task field for it)."""
    task = store.add_task(
        role="pm",
        title=f"Resolve attention {sig.kind}: {sig.title}",
        detail=detail,
    )
    store.update_task(task.task_id, source_signal_id=sig.id)
    return task.task_id


def resolve(
    project_id: str, signal_id: str, action: str, *,
    suggestion_id: str | None = None, correction_text: str | None = None,
    by: str = "user", store: LedgerStore | None = None,
) -> tuple[AttentionSignal, str | None]:
    """Apply a user action to an open signal. Returns (signal, created_task_id?).

    Accepting a suggestion or providing a correction creates a linked PM task.
    """
    store = store or _store(project_id)
    sig = _project(store).get(signal_id)
    if sig is None:
        raise AttentionError(f"unknown signal: {signal_id}")
    if sig.state != "open":
        raise AttentionError(f"signal {signal_id} is not open (state={sig.state})")
    if action not in VALID_ACTIONS[sig.kind]:
        raise AttentionError(f"action {action!r} invalid for a {sig.kind}")

    created_task_id: str | None = None
    if action == "accept":
        # A member-health Problem is an INFRA issue (logged-out CLI, removed
        # model, rate-limited account) — its fix is changing the member's
        # provider/route, not a dev task. Spawning a "Resolve attention problem:
        # Member unhealthy …" backlog task is meta-work that clutters the board
        # and blocks definition-of-done, so accepting one just clears it.
        if sig.kind == "problem" and sig.source not in _CONFIGURATION_PROBLEM_SOURCES:
            # accept the PM's suggestion → create the implementing task
            created_task_id = _make_task(store, sig, _suggestion_text(sig, suggestion_id))
        new_state = "accepted"
    elif action == "correct":
        if not (correction_text and correction_text.strip()):
            raise AttentionError("correct requires correction_text")
        created_task_id = _make_task(store, sig, correction_text.strip())
        new_state = "corrected"
    elif action == "defer":
        new_state = "deferred"
    elif action == "dismiss":
        new_state = "dismissed"
    else:  # pragma: no cover - guarded above
        raise AttentionError(f"unhandled action {action!r}")

    updated = _write_resolution(
        store, sig, state=new_state, action=action, by=by,
        suggestion_id=suggestion_id, correction_text=correction_text,
        created_task_id=created_task_id,
    )
    return updated, created_task_id


def auto_resolve(
    project_id: str, signal_id: str, *, store: LedgerStore | None = None,
) -> tuple[AttentionSignal, str | None]:
    """``block_on_problems=off`` path: the PM picks the first suggestion, creates the
    implementing task, and the signal lands ``auto_resolved`` — still recorded and
    shown, never silently handled."""
    store = store or _store(project_id)
    sig = _project(store).get(signal_id)
    if sig is None:
        raise AttentionError(f"unknown signal: {signal_id}")
    if sig.state != "open":
        raise AttentionError(f"signal {signal_id} is not open (state={sig.state})")
    suggestion_id = sig.suggestions[0].get("id") if sig.suggestions else None
    # Member-health Problems are infra issues, not dev work — auto-resolving one
    # must NOT spawn a "Resolve attention problem: Member unhealthy …" backlog
    # task (meta-work that clutters the board + blocks definition-of-done).
    created_task_id = (
        None if sig.source in _CONFIGURATION_PROBLEM_SOURCES
        else _make_task(store, sig, _suggestion_text(sig, suggestion_id))
    )
    updated = _write_resolution(
        store, sig, state="auto_resolved", action="accept", by="pm",
        suggestion_id=suggestion_id, created_task_id=created_task_id,
    )
    return updated, created_task_id


# --------------------------------------------------------------------------- #
# Progress Monitor producer (F117-03)
# --------------------------------------------------------------------------- #
_MONITOR_SUGGESTIONS: list[dict[str, Any]] = [
    {"id": "proceed", "label": "Keep going",
     "detail": "Resume the run as-is."},
    {"id": "stop", "label": "Stop and let me look",
     "detail": "Pause so I can inspect the project before it continues."},
    {"id": "adjust", "label": "Adjust and retry",
     "detail": "Change direction, then retry the stuck stage."},
]


def _monitor_title(detector: str) -> str:
    return f"Stuck: {detector}"


def find_open_monitor_problem(
    project_id: str, *, stage: str, detector: str,
    store: LedgerStore | None = None,
) -> AttentionSignal | None:
    """Return the open monitor Problem for a stage+detector, if one exists."""
    store = store or _store(project_id)
    title = _monitor_title(detector)
    for sig in list_open(project_id, store=store):
        if (sig.kind == "problem" and sig.source == "monitor"
                and sig.stage == stage and sig.title == title):
            return sig
    return None


def raise_monitor_problem(
    project_id: str, *, stage: str, detector: str, reason: str,
    store: LedgerStore | None = None,
) -> AttentionSignal | None:
    """Raise a blocking Problem when a governed run stops making progress.

    v1 uses a canned, detector-derived evaluation (no PM model turn — that's a
    future enhancement). Deduped by (source=monitor, stage, detector): if an open
    monitor Problem for the same detector+stage already exists, returns ``None``
    instead of stacking duplicates across restarts/retries.
    """
    store = store or _store(project_id)
    title = _monitor_title(detector)
    if find_open_monitor_problem(
        project_id, stage=stage, detector=detector, store=store,
    ) is not None:
        return None
    pm_eval = (
        f"The run stopped on '{detector}' ({reason}). It is not making forward "
        f"progress — choose how to proceed."
    )
    return raise_signal(
        project_id, kind="problem", source="monitor", stage=stage, title=title,
        summary=reason or detector, pm_evaluation=pm_eval,
        suggestions=list(_MONITOR_SUGGESTIONS), store=store,
    )


# --------------------------------------------------------------------------- #
# Member-health producer (F120)
# --------------------------------------------------------------------------- #
_MEMBER_HEALTH_SUGGESTIONS: list[dict[str, Any]] = [
    {"id": "open_provider_settings", "label": "Open provider settings",
     "detail": "Log in / fix this provider in Settings → Providers, then retry."},
    {"id": "disable_member", "label": "Disable this member & continue",
     "detail": "Drop this member from the room so the rest of the team can run."},
    {"id": "stop", "label": "Stop and let me look",
     "detail": "Pause so I can inspect the providers before the run continues."},
]
_WORKER_UNPRODUCTIVE_SUGGESTIONS: list[dict[str, Any]] = [
    {"id": "edit_room", "label": "Edit room",
     "detail": "Switch this role to a stronger model or add another eligible member."},
    {"id": "stop", "label": "Stop and let me look",
     "detail": "Keep the run paused while I inspect the task and room."},
]
_COMPLETION_BLOCKED_SUGGESTIONS: list[dict[str, Any]] = [
    {"id": "stop", "label": "Stop and let me look",
     "detail": "Pause so I can finish, cancel, or unblock the open work."},
]


def _member_health_title(member_id: str, reason: str) -> str:
    return f"Member unhealthy: {member_id} ({reason})"


def find_open_member_health_problem(
    project_id: str, *, member_id: str, reason: str,
    store: LedgerStore | None = None,
) -> AttentionSignal | None:
    """Return the open member-health Problem for (member_id, reason), if any.

    Dedupe key is (source=member_health, member_id, reason) — a flapping member
    raises one Problem, not one-per-attempt (criterion #3, F119 policy)."""
    store = store or _store(project_id)
    title = _member_health_title(member_id, reason)
    for sig in list_open(project_id, store=store):
        if (sig.kind == "problem" and sig.source == "member_health"
                and sig.title == title):
            return sig
    return None


def _worker_unproductive_title(task_id: str) -> str:
    return f"Task stuck: no member can produce a usable turn ({task_id})"


def raise_worker_unproductive_problem(
    project_id: str, *, task_id: str, task_title: str, members_tried: list[str],
    last_member: str, last_route: str, last_error: str, stage: str = "development",
    store: LedgerStore | None = None,
) -> AttentionSignal | None:
    """F127 — the escalate-up ladder is exhausted: every eligible member produced
    unusable turns for this task (tool-call markup / schema mismatch). Raise ONE
    blocking Problem (deduped per task) so the user gets an actionable fix instead
    of a silent ``no_progress`` stop."""
    store = store or _store(project_id)
    title = _worker_unproductive_title(task_id)
    for sig in list_open(project_id, store=store):
        if (sig.kind == "problem" and sig.source == "worker_unproductive"
                and sig.title == title):
            return None
    tried = ", ".join(m for m in members_tried if m) or last_member or "the team"
    remediation = (
        "Switch a member to a stronger model in the room editor (Coding teams "
        "want a strong PM + capable workers), or simplify the task — the assigned "
        "models keep returning invalid turns instead of code."
    )
    summary = (
        f"'{task_title or task_id}' could not be done: {tried} produced unusable "
        f"turns (last: {last_member} on {last_route or 'its model'} — {last_error}). "
        f"{remediation}"
    )
    return raise_signal(
        project_id, kind="problem", source="worker_unproductive", stage=stage,
        title=title, summary=summary, pm_evaluation=summary,
        suggestions=list(_WORKER_UNPRODUCTIVE_SUGGESTIONS),
        context={
            "task_id": task_id, "members_tried": list(members_tried),
            "member_id": last_member, "gateway_route_id": last_route,
            "reason": last_error, "remediation": remediation,
        },
        store=store,
    )


_COMPLETION_BLOCKED_TITLE = "Run can't complete: open work remains"


def find_open_completion_blocked_problem(
    project_id: str, store: LedgerStore | None = None,
) -> AttentionSignal | None:
    """The open completion_blocked Problem for this project, if any (deduped
    per project — one Problem, not one per refused done-claim)."""
    store = store or _store(project_id)
    for sig in list_open(project_id, store=store):
        if sig.kind == "problem" and sig.source == "completion_blocked":
            return sig
    return None


def raise_completion_blocked_problem(
    project_id: str, *, open_items: list[Any], stage: str = "development",
    store: LedgerStore | None = None,
) -> AttentionSignal | None:
    """F128 — the PM kept claiming the project was done while open work remained.
    Raise ONE blocking Problem (deduped per project) listing what's still open so
    the human finishes/cancels it, instead of the run ending in a false "done" or
    a silent ``no_progress``."""
    store = store or _store(project_id)
    if find_open_completion_blocked_problem(project_id, store=store) is not None:
        return None
    from .completion import count_human_required, summarize_open_items

    listed = summarize_open_items(open_items)
    human = count_human_required(open_items)
    human_note = (
        f" {human} of them need you (a blocked task or conflicted PR that can't "
        "be auto-resolved)." if human else ""
    )
    remediation = (
        "Finish or cancel these tasks, or resolve the blocked merge conflict, "
        "then start the run again."
    )
    summary = (
        f"The team reported done, but {len(open_items)} item(s) are still open: "
        f"{listed}.{human_note} {remediation}"
    )
    return raise_signal(
        project_id, kind="problem", source="completion_blocked", stage=stage,
        title=_COMPLETION_BLOCKED_TITLE, summary=summary, pm_evaluation=summary,
        suggestions=list(_COMPLETION_BLOCKED_SUGGESTIONS),
        context={
            "open_item_count": len(open_items),
            "human_required_count": human,
            "remediation": remediation,
            "open_items": [
                {"kind": getattr(i, "kind", ""), "id": getattr(i, "id", ""),
                 "title": getattr(i, "title", ""), "state": getattr(i, "state", ""),
                 "human_required": bool(getattr(i, "human_required", False))}
                for i in open_items[:20]
            ],
        },
        store=store,
    )


def raise_member_health_problem(
    project_id: str, *, member_id: str, role: str, route: str, reason: str,
    detail: str, remediation: str, attempts: int, stage: str,
    store: LedgerStore | None = None,
) -> AttentionSignal | None:
    """F120 — raise a blocking Problem when a member terminally cannot run.

    Names the member, its coding role, the gateway route/provider, the classified
    ``reason``, the redacted ``detail`` and a one-step ``remediation``. Deduped by
    (member_id, reason): if an open member-health Problem for the same member+reason
    already exists, returns ``None`` instead of stacking duplicates across
    attempts/restarts.
    """
    store = store or _store(project_id)
    if find_open_member_health_problem(
        project_id, member_id=member_id, reason=reason, store=store,
    ) is not None:
        return None
    label = route or "its provider"
    pm_eval = (
        f"Member {member_id} ({role or 'member'}, {label}) failed {attempts} "
        f"time(s) with '{reason}': {detail or remediation}. It cannot produce "
        f"output — {remediation}"
    )
    suggestions = list(_MEMBER_HEALTH_SUGGESTIONS)
    summary = (
        f"{member_id} ({label}) failed {attempts}×: {reason}. {remediation}"
    )
    return raise_signal(
        project_id, kind="problem", source="member_health", stage=stage,
        title=_member_health_title(member_id, reason), summary=summary,
        pm_evaluation=pm_eval, suggestions=suggestions,
        context={
            "member_id": member_id, "coding_role": role,
            "gateway_route_id": route, "reason": reason, "detail": detail,
            "remediation": remediation, "attempts": attempts,
        },
        store=store,
    )


def resolve_stale_member_health(
    project_id: str,
    members: list[dict[str, Any]],
    *,
    store: LedgerStore | None = None,
) -> list[str]:
    """Dismiss open member-health Problems the current roster has already fixed.

    A member-health Problem is keyed by (member_id, reason) and stays OPEN and
    *blocking* until resolved. When the operator fixes the cause by changing the
    member's model/provider (e.g. a removed Cursor model -> a valid one, or
    Cursor -> Claude after hitting a usage limit), the OLD Problem doesn't
    auto-clear — so a stale, blocking Problem keeps gating the next run for a
    member that no longer has that route at all.

    Called at run start: dismiss any open member-health Problem whose recorded
    ``gateway_route_id`` no longer matches the member's CURRENT route (or whose
    member is gone from the roster). A Problem whose member+route is unchanged is
    left alone — it's still relevant. Returns the dismissed Problem titles."""
    store = store or _store(project_id)
    current_route: dict[str, str] = {}
    for m in members:
        mid = str(m.get("id", "") or "")
        if mid:
            current_route[mid] = str(m.get("gateway_route_id", "") or "")
    dismissed: list[str] = []
    for sig in list_open(project_id, store=store):
        if sig.kind != "problem" or sig.source != "member_health":
            continue
        ctx = sig.context or {}
        mid = str(ctx.get("member_id", "") or "")
        recorded_route = str(ctx.get("gateway_route_id", "") or "")
        # Still relevant only when the same member still uses the same route.
        if mid in current_route and recorded_route and current_route[mid] == recorded_route:
            continue
        _write_resolution(store, sig, state="dismissed", action="dismiss", by="system")
        dismissed.append(sig.title)
    return dismissed


def resolve_stale_worker_unproductive(
    project_id: str,
    members: list[dict[str, Any]],
    *,
    store: LedgerStore | None = None,
) -> list[str]:
    """Clear exhausted-task exclusions after the room configuration changes.

    The Problem tells the user to switch a model or add/enable another worker.
    Persisted member exclusions must therefore be tied to the route that failed,
    not to the member forever. At run start, restore a task when a same-role
    member is new or now uses a different route, and dismiss its stale Problem.
    """
    from .topology import coding_role_of

    store = store or _store(project_id)
    current = {
        str(member.get("id", "")): {
            "role": coding_role_of(member),
            "route": str(member.get("gateway_route_id", "") or ""),
        }
        for member in members
        if member.get("enabled", True) and str(member.get("id", ""))
    }
    signals = [
        signal
        for signal in list_all(project_id, store=store)
        if signal.kind == "problem" and signal.source == "worker_unproductive"
    ]
    signals_by_task = {
        str((signal.context or {}).get("task_id", "")): signal
        for signal in signals
        if (signal.context or {}).get("task_id")
    }
    dismissed: list[str] = []
    for task in store.list_tasks():
        signal = signals_by_task.get(task.task_id)
        if signal is None:
            continue
        extras = getattr(task, "_extras", {}) or {}
        excluded = set(extras.get("excluded_member_ids") or [])
        failed_routes = dict(extras.get("excluded_member_routes") or {})
        context = signal.context or {}
        last_member = str(context.get("member_id", "") or "")
        if last_member and last_member not in failed_routes:
            failed_routes[last_member] = str(context.get("gateway_route_id", "") or "")

        recovered = []
        for member_id, member in current.items():
            if member["role"] != task.role:
                continue
            failed_route = str(failed_routes.get(member_id, "") or "")
            if member_id not in excluded or (failed_route and member["route"] != failed_route):
                recovered.append(member_id)
        if not recovered:
            continue

        store.update_task(
            task.task_id,
            state="todo" if task.state not in {"done", "dropped"} else task.state,
            assignee_member_id=None,
            excluded_member_ids=[],
            excluded_member_routes={},
            task_reassignment_count=0,
            pm_assist_pending=False,
            pm_assist_attempts=0,
            reassignment_from_member_id=None,
            reassignment_attempts=None,
            reassignment_reason=None,
        )
        if signal.state == "open":
            _write_resolution(
                store, signal, state="dismissed", action="dismiss", by="system"
            )
            dismissed.append(signal.title)
    return dismissed


# --------------------------------------------------------------------------- #
# Alerts producer (F117-04)
# --------------------------------------------------------------------------- #
def raise_review_alert(
    project_id: str, *, stage: str, title: str, summary: str,
    store: LedgerStore | None = None,
) -> AttentionSignal | None:
    """Raise a non-blocking advisory Alert from a reviewer finding (e.g. "a save
    button was built with no guidance on button vs autosave"). Deduped by
    (source=reviewer, stage, title) so a re-review doesn't stack duplicates.
    """
    store = store or _store(project_id)
    for s in list_open(project_id, store=store):
        if (s.kind == "alert" and s.source == "reviewer"
                and s.stage == stage and s.title == title):
            return None
    return raise_signal(
        project_id, kind="alert", source="reviewer", stage=stage,
        title=title or "Reviewer note", summary=summary or title, store=store,
    )


def raise_tests_skipped_alert(
    project_id: str, *, stage: str, summary: str,
    store: LedgerStore | None = None,
) -> AttentionSignal | None:
    """F142 WS-C: a non-blocking Alert raised when the tester declares a PR's slice
    not-applicable (merged without running any test command). Deduped by
    (source=tests_skipped, stage) to ONE open alert per run — the point is to tell
    the operator "one or more slices merged without tests, verify coverage", not to
    stack a signal per PR or to block (a foundation slice legitimately has nothing
    runnable to test yet). The guardrail that a command which ran and FAILED still
    blocks is enforced in the tester handler, not here.
    """
    store = store or _store(project_id)
    for s in list_open(project_id, store=store):
        if s.kind == "alert" and s.source == "tests_skipped" and s.stage == stage:
            return None
    return raise_signal(
        project_id, kind="alert", source="tests_skipped", stage=stage,
        title="PR merged without running tests",
        summary=summary or "The tester declared a slice not-applicable for testing.",
        store=store,
    )
