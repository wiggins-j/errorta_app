"""F087-12 - persisted Coding Mode run recovery.

Coding Mode already persists run lifecycle in ``run_state.json``. This module
turns that persistence into an explicit recovery contract: after a sidecar
restart, an orphaned ``running`` run is marked ``interrupted`` and any in-flight
``doing`` tasks are returned to ``todo`` so a deliberate resume can continue
from the ledger without losing work or leaving tasks wedged forever.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .ledger import LedgerStore, _now, list_projects


@dataclass(frozen=True)
class CodingRunRecoveryResult:
    project_id: str
    recovered: bool
    status_before: str
    status_after: str
    requeued_task_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CodingRunRecoverySummary:
    recovered_projects: list[CodingRunRecoveryResult] = field(default_factory=list)
    scanned: int = 0

    @property
    def interrupted_projects(self) -> list[str]:
        return [r.project_id for r in self.recovered_projects if r.recovered]


def recover_orphaned_run(
    store: LedgerStore,
    *,
    live: bool,
    reason: str = "sidecar_restart",
) -> CodingRunRecoveryResult:
    """Recover one project if its ledger says ``running`` but no live worker owns
    it. Idempotent: only the first call transitions ``running`` -> ``interrupted``
    and records the recovery decision."""
    # F087-13 WS-3: guard the running->interrupted transition under the
    # per-project lock and re-check status inside it, so concurrent status polls
    # (or a poll racing boot recovery) cannot double-requeue tasks or append
    # duplicate run_interrupted decisions. Only the first caller flips the status.
    with store.lock:
        state = store.get_run_state()
        before = str(state.get("status", "idle"))
        if live or before != "running":
            return CodingRunRecoveryResult(
                project_id=store.project_id,
                recovered=False,
                status_before=before,
                status_after=before,
            )

        doing_tasks = list(store.list_tasks(state="doing"))
        requeued: list[str] = []
        for task in doing_tasks:
            store.update_task(task.task_id, state="todo", assignee_member_id=None)
            requeued.append(task.task_id)

        workspace_fingerprint = None
        try:
            from .workspace import CodingWorkspace
            workspace = CodingWorkspace(store.project_id, store)
            if workspace.exists():
                for task in doing_tasks:
                    workspace.remove_worktree(task.task_id)
                workspace.prune_worktrees()
                workspace_fingerprint = workspace.workspace_fingerprint()
        except Exception:
            workspace_fingerprint = None

        ts = _now()
        patch = {
            "status": "interrupted",
            "ended_at": ts,
            "interrupted_at": ts,
            "recovery_reason": reason,
            "recoverable": True,
            "can_resume": True,
            "cancel_requested": False,
            "requeued_task_ids": requeued,
            "previous_status": before,
        }
        if workspace_fingerprint is not None:
            patch["workspace_fingerprint"] = workspace_fingerprint
        store.set_run_state(**patch)
        store.record_decision(
            title="run interrupted",
            context="coding run recovery",
            choice="run_interrupted",
            rationale=(
                f"Recovered orphaned running Coding Mode run after {reason}; "
                f"requeued {len(requeued)} in-flight task(s)."
            ),
            related_task_ids=requeued,
        )
        return CodingRunRecoveryResult(
            project_id=store.project_id,
            recovered=True,
            status_before=before,
            status_after="interrupted",
            requeued_task_ids=requeued,
        )


def reclaim_stranded_inflight(
    store: LedgerStore, *, reason: str = "run_start"
) -> list[str]:
    """Return any tasks wedged in ``doing`` to ``todo`` before a (re)start.

    ``recover_orphaned_run`` only reclaims tasks when the prior status was
    ``running`` (a crashed/orphaned sidecar). But a run that ended in a TERMINAL
    state — ``stopped`` (e.g. a ``member_unhealthy`` stop) or ``failed`` — also
    leaves its in-flight tasks marked ``doing``, and the scheduler only ever
    dispatches ``todo`` tasks (``next_tasks``), so those would sit frozen in the
    board's "Doing" column forever and the next run could never pick them up.

    This is called at the top of every run start. At that moment no task is
    genuinely in flight in THIS process, so every ``doing`` task is an orphan from
    a prior process and is safe to requeue (clear the assignee + drop the stale
    worktree so a fresh turn re-establishes it from the persisted branch).
    Status-agnostic and idempotent: a no-op when nothing is ``doing``. Returns the
    requeued task ids."""
    with store.lock:
        doing_tasks = list(store.list_tasks(state="doing"))
        if not doing_tasks:
            return []
        requeued: list[str] = []
        for task in doing_tasks:
            store.update_task(task.task_id, state="todo", assignee_member_id=None)
            requeued.append(task.task_id)
        try:
            from .workspace import CodingWorkspace

            workspace = CodingWorkspace(store.project_id, store)
            if workspace.exists():
                for task in doing_tasks:
                    workspace.remove_worktree(task.task_id)
                workspace.prune_worktrees()
        except Exception:
            pass
        store.record_decision(
            title="reclaimed stranded tasks",
            context="coding run start",
            choice="inflight_reclaimed",
            rationale=(
                f"Requeued {len(requeued)} task(s) left in 'doing' by a prior run "
                f"({reason}) so the scheduler can pick them up again."
            ),
            related_task_ids=requeued,
        )
        return requeued


def scan_and_recover(
    *,
    root: Path | None = None,
    live_project_ids: Iterable[str] = (),
    reason: str = "sidecar_startup",
) -> CodingRunRecoverySummary:
    live = set(live_project_ids)
    recovered: list[CodingRunRecoveryResult] = []
    projects = list_projects(root)
    for project in projects:
        project_id = str(project.get("id", ""))
        if not project_id:
            continue
        result = recover_orphaned_run(
            LedgerStore(project_id, root=root),
            live=project_id in live,
            reason=reason,
        )
        if result.recovered:
            recovered.append(result)
    return CodingRunRecoverySummary(
        recovered_projects=recovered,
        scanned=len(projects),
    )


__all__ = [
    "CodingRunRecoveryResult",
    "CodingRunRecoverySummary",
    "recover_orphaned_run",
    "reclaim_stranded_inflight",
    "scan_and_recover",
]
