"""F087-02 — PM task-queue orchestration brain for Coding Mode.

Two pieces, both pure-logic + unit-testable:

* ``decide_next`` — given the ledger + the room's coding members, decide the
  next action: assign a worker an actionable task, give the PM a plan turn when
  workers are idle, or complete. READ-ONLY over the ledger (mirrors the
  invariant-2 "topology proposes, scheduler mutates" split).
* ``CodingReconciler`` — the stateful side: when a member is assigned it marks
  the task ``doing``; when a task completes it spawns the next role's task so
  the dev -> reviewer -> tester loop is expressed as backlog tasks, not a fixed
  sequence.

The live scheduler (F087-03 loop) drives these against real member turns; here
they are exercised with the real ledger store.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol

from . import paths as _paths
from .ledger import Task

# Coding roles (distinct from F031 CouncilMember.role answerer/critic/judge).
PM = "pm"
DEV = "dev"
REVIEWER = "reviewer"
TESTER = "tester"

# Drain the pipeline before starting new dev work: finish reviewing/validating
# what's in flight, then do new development.
_WORKER_PRIORITY = (TESTER, REVIEWER, DEV)


class QueryLedger(Protocol):
    """Read surface the topology depends on (F001-SEAM-style injection)."""

    def get_project(self) -> Any: ...
    def list_tasks(
        self,
        *,
        state: Optional[str] = ...,
        role: Optional[str] = ...,
    ) -> list[Task]: ...
    def next_task(self, role: str) -> Optional[Task]: ...


@dataclass(frozen=True)
class Assign:
    """Give ``member_id`` the actionable ``task_id`` for its role."""
    member_id: str
    task_id: str
    role: str


@dataclass(frozen=True)
class Plan:
    """Give the PM a planning turn (workers idle / re-plan needed)."""
    member_id: str


@dataclass(frozen=True)
class PMAssist:
    """Give the PM one bounded turn to split/re-scope a stuck worker task."""
    member_id: str
    task_id: str


@dataclass(frozen=True)
class Merge:
    """F087-17: give the PM a turn to merge an approved+green PR into master."""
    member_id: str
    pr_id: str


@dataclass(frozen=True)
class GovernancePlan:
    """Give the PM a governance artifact turn (brainstorm/spec/plan)."""
    member_id: str
    phase: str


@dataclass(frozen=True)
class GovernanceReview:
    """Give a reviewer (or PM, in strict mode) a planning-artifact review turn."""
    member_id: str
    artifact_id: str
    reviewer_role: str = REVIEWER


@dataclass(frozen=True)
class GovernanceMaterialize:
    """Materialize approved plan slices into DEV tasks."""
    member_id: str


@dataclass(frozen=True)
class Complete:
    """The run is done (project marked done, or nothing left to do)."""
    reason: str


CodingAction = (
    Assign
    | Plan
    | PMAssist
    | Merge
    | GovernancePlan
    | GovernanceReview
    | GovernanceMaterialize
    | Complete
)


def _has_pending_interjection(ledger: Any) -> bool:
    """True when the user has sent a message the PM hasn't consumed yet. Defensive
    (test doubles may not expose the method; a read error never blocks scheduling)."""
    fn = getattr(ledger, "list_unconsumed_interjections", None)
    if not callable(fn):
        return False
    try:
        return bool(fn())
    except Exception:  # noqa: BLE001 — scheduling must not break on a read error
        return False


def _has_open_work(ledger: Any, members: list[tuple[str, str]]) -> bool:
    """True when a project still has something actionable: a todo/doing task a
    configured worker role can take, a PR still to land, or a pending user message.

    Used to keep a ``done`` project from short-circuiting straight to ``Complete``
    when NEW work has re-opened it — a user-added task or a Current-Focus steering
    directive (the "fix this crash on a finished project" case). A genuinely
    finished project (no open work) still completes normally. Read-only + defensive:
    a read error never keeps a done project spuriously running."""
    list_prs = getattr(ledger, "list_prs", None)
    if callable(list_prs):
        try:
            if any(p.get("status") == "mergeable" for p in list_prs()):
                return True
        except Exception:  # noqa: BLE001
            pass
    # F146 Slice E: an active Current Focus with no tasks of its own is open work.
    # A done project with a steering focus must fall through to the PM plan turn so
    # the focus is re-brainstormed into tasks (the "Start does nothing" fix), not
    # short-circuited to Complete before the PM ever plans.
    active_focuses = getattr(ledger, "active_focuses", None)
    if callable(active_focuses):
        try:
            if active_focuses():
                return True
        except Exception:  # noqa: BLE001 — scheduling must not break on a read error
            pass
    if _has_pending_interjection(ledger):
        return True
    roles = {role for _, role in members}
    for role in _WORKER_PRIORITY:
        if role in roles:
            try:
                if ledger.next_task(role) is not None:
                    return True
            except Exception:  # noqa: BLE001
                pass
    return False


def _excluded_member_ids(task: Any) -> set[str]:
    """F127: members barred from this task (each produced unusable turns for it)."""
    ex = (getattr(task, "_extras", {}) or {}).get("excluded_member_ids")
    return set(ex) if isinstance(ex, (list, set, tuple)) else set()


def _pick_member(
    candidates: list[str], member_tiers: dict[str, int] | None, *, escalate: bool
) -> str:
    """Preserve room order normally; prefer the highest tier on reassignment."""
    if not escalate or not member_tiers:
        return candidates[0]
    return max(candidates, key=lambda m: (member_tiers.get(m, 1), -candidates.index(m)))


def _pending_pm_assist_task(ledger: QueryLedger) -> Optional[Task]:
    """First ready worker task that exhausted same-role reassignment."""
    try:
        done_ids = {t.task_id for t in ledger.list_tasks(state="done")}
        tasks = ledger.list_tasks(state="todo")
    except Exception:
        return None
    for task in tasks:
        extras = getattr(task, "_extras", {}) or {}
        if extras.get("pm_assist_pending") and all(
            dep in done_ids for dep in task.depends_on
        ):
            return task
    return None


def decide_next(
    ledger: QueryLedger,
    members: list[tuple[str, str]],  # (member_id, coding_role)
    member_tiers: dict[str, int] | None = None,  # F127: member_id -> tier rank
) -> CodingAction:
    """Decide the next coding action from ledger state. READ-ONLY."""
    try:
        project = ledger.get_project()
    except Exception:
        return Complete(reason="no_project")
    # A `done` project completes ONLY when nothing is actionable. New work — a
    # user-added task or a Current-Focus steering directive on a finished project —
    # re-opens it: fall through and work the pending tasks instead of ending idle.
    if getattr(project, "status", "active") == "done" \
            and not _has_open_work(ledger, members):
        return Complete(reason="definition_of_done")

    by_role: dict[str, list[str]] = {}
    for member_id, role in members:
        by_role.setdefault(role, []).append(member_id)

    governance = _governance_preflight(ledger, by_role)
    if governance is not None:
        return governance

    # 0) F087-17: integrate first. A PR that is reviewer-approved AND tests-green
    #    (status "mergeable") is handed to the PM to merge into master before more
    #    dev work, so accumulated work lands and later tasks branch off it.
    pm_ids = by_role.get(PM)
    if pm_ids:
        list_prs = getattr(ledger, "list_prs", None)
        if callable(list_prs):
            for pr in list_prs():
                if pr.get("status") == "mergeable":
                    return Merge(member_id=pm_ids[0], pr_id=pr["pr_id"])
        # F100 PR-B: in strict mode the PM also reviews code PRs (the second of
        # the dual review). A ready PM PR-review task is dispatched before more
        # dev work so the merge gate can clear.
        pm_review = _ready_pm_review_task(ledger)
        if pm_review is not None:
            return Assign(member_id=pm_ids[0], task_id=pm_review.task_id, role=PM)

    # 0b) A pending user message to the PM preempts worker dispatch: give the PM
    #     its next turn NOW so it reads + acts on the message immediately, instead
    #     of waiting for the worker pipeline to run dry (its normal plan cadence).
    if pm_ids and _has_pending_interjection(ledger):
        return Plan(member_id=pm_ids[0])

    # F127: a task that exhausted same-role workers gets one bounded PM re-scope
    # turn before it becomes a blocking attention Problem.
    pm_assist = _pending_pm_assist_task(ledger)
    if pm_assist is not None:
        if pm_ids:
            return PMAssist(member_id=pm_ids[0], task_id=pm_assist.task_id)
        return Complete(reason="worker_unproductive")

    # 1) Drain the pipeline: assign an actionable task to a worker.
    for role in _WORKER_PRIORITY:
        member_ids = by_role.get(role)
        if not member_ids:
            continue
        task = ledger.next_task(role)
        if task is None:
            continue
        # F127: skip members this task has barred (escalate-up reassignment);
        # among the rest prefer the highest tier. If none are eligible right now,
        # the task waits for a non-excluded member to free up (the loop raises an
        # attention Problem if every member has been excluded).
        eligible = [m for m in member_ids if m not in _excluded_member_ids(task)]
        if not eligible:
            continue
        preferred = str(getattr(task, "preferred_member_id", "") or "")
        selected = (
            preferred if preferred in eligible else _pick_member(
                eligible, member_tiers, escalate=bool(_excluded_member_ids(task))
            )
        )
        return Assign(
            member_id=selected,
            task_id=task.task_id, role=role)

    # 2) Workers idle -> let the PM plan / re-plan (if there is a PM).
    pm_ids = by_role.get(PM)
    if pm_ids:
        return Plan(member_id=pm_ids[0])

    # 3) No PM and no worker tasks -> nothing to do.
    return Complete(reason="no_actionable_work")


def plan_next_batch(
    ledger: QueryLedger,
    members: list[tuple[str, str]],  # the IDLE (member_id, coding_role) this tick
    member_tiers: dict[str, int] | None = None,  # F127: member_id -> tier rank
    *,
    hot_paths: set[str] | None = None,        # F159: paths that are "hot"
    hot_blocked: set[str] | None = None,      # F159: hot paths already held this tick
    frozen: set[str] | None = None,           # F159: centralize-frozen paths
    frozen_owner_task_id: str | None = None,  # F159: the only task allowed to touch frozen
) -> list[CodingAction]:
    """F087 Slice 1 — the concurrent planner: return ALL runnable actions for the
    idle members this tick (vs ``decide_next``'s single action). READ-ONLY.

    Used only when ``max_parallel_workers > 1``; ``decide_next`` remains the
    one-action path at ``=1`` so that fallback is unchanged. ``members`` are the
    members idle this tick (the loop excludes in-flight ones).

    Rules:
    * Workers fan out: for each role (tester > reviewer > dev) assign distinct
      ready tasks to distinct idle members — so 2 devs + 1 reviewer can all work
      at once (3 in flight), and a 2nd same-role member (``m-3``) is actually
      used. ``ledger.next_tasks`` + a running exclude set prevents handing one
      task to two members.
    * The single PM does ``Merge`` OR ``Plan``, never both: a ``mergeable`` PR
      preempts a plan turn (integration stays serial — one merge at a time).
    * The PM plans only when no worker could be assigned this tick (mirrors
      ``decide_next``: plan when the pipeline is dry), so it doesn't spam plan
      turns while devs are busy.
    """
    try:
        project = ledger.get_project()
    except Exception:
        return [Complete(reason="no_project")]
    # See decide_next: a `done` project re-opened by new work (a user-added task or
    # a steering directive) must NOT short-circuit — fall through and work it.
    if getattr(project, "status", "active") == "done" \
            and not _has_open_work(ledger, members):
        return [Complete(reason="definition_of_done")]

    by_role: dict[str, list[str]] = {}
    for member_id, role in members:
        by_role.setdefault(role, []).append(member_id)

    governance = _governance_preflight(ledger, by_role)
    if governance is not None:
        return [governance]

    batch: list[CodingAction] = []
    used_members: set[str] = set()
    chosen_tasks: set[str] = set()

    # PM: Merge is an exclusive batch action. Slice 3 dispatches a returned
    # batch concurrently, so mixing a merge with fresh worker assignments would
    # let new work start from a base while that base is changing.
    pm_ids = by_role.get(PM) or []
    if pm_ids:
        list_prs = getattr(ledger, "list_prs", None)
        if callable(list_prs):
            for pr in list_prs():
                if pr.get("status") == "mergeable":
                    return [Merge(member_id=pm_ids[0], pr_id=pr["pr_id"])]
        # F100 PR-B: a ready PM PR-review (strict-mode dual review) is an
        # exclusive PM action this tick, like Merge — the PM has one turn.
        pm_review = _ready_pm_review_task(ledger)
        if pm_review is not None:
            return [Assign(member_id=pm_ids[0], task_id=pm_review.task_id, role=PM)]
        # A pending user message is an exclusive PM turn (like Merge): the PM reads
        # + acts on it NOW instead of waiting for the pipeline to run dry.
        if _has_pending_interjection(ledger):
            return [Plan(member_id=pm_ids[0])]

    pm_assist = _pending_pm_assist_task(ledger)
    if pm_assist is not None:
        if pm_ids:
            return [PMAssist(member_id=pm_ids[0], task_id=pm_assist.task_id)]
        return [Complete(reason="worker_unproductive")]

    # Workers: fan out distinct ready tasks across all idle same-role members.
    # F159: `blocked` accumulates hot paths that are unavailable this tick — seeded
    # with the ones already held by a live (doing / open-PR) task, then extended as
    # we assign, so two tasks in one batch never claim the same hot file.
    hot = set(hot_paths or ())
    blocked = set(hot_blocked or ())
    frozen_set = set(frozen or ())
    worker_assigned = False
    for role in _WORKER_PRIORITY:
        ids = [m for m in by_role.get(role, []) if m not in used_members]
        if not ids:
            continue
        # Over-fetch so we can still place tasks even when some members are barred
        # from the first-ready ones (F127 reassignment). F159: when the hot/frozen
        # gate is active, fetch extra headroom so a gated task doesn't starve an
        # idle member of the non-colliding work behind it.
        want = len(ids) + (32 if (hot or frozen_set) else 0)
        tasks = ledger.next_tasks(role, want, exclude=chosen_tasks)
        for task in tasks:
            # F159: serialize hot-file / frozen-file contention. A task that would
            # touch a frozen path (unless it IS the centralize owner) or a hot path
            # already held this tick waits — its member gets a non-colliding task
            # instead. Prevents the parallel-writers-to-one-file conflict churn.
            tp = _paths.task_touched_paths(task)
            if frozen_set and task.task_id != frozen_owner_task_id:
                if _paths.paths_intersect(tp, frozen_set):
                    continue
                # F159 teeth: a freeze only bites tasks whose touched paths are
                # KNOWN to hit a frozen file — but real dev tasks that thrash a hot
                # file ("Add real-time activity…") declare no `target_files` and name
                # it nowhere, so `tp` is empty and the freeze was inert (the
                # mockData.ts non-convergence). While a freeze is active we cannot
                # prove such a writer won't touch the frozen file, so hold prose-
                # silent DEV writers until the centralize owner lands. This is the
                # escalated, bounded state — force-lifted by
                # `hot_file_freeze_stall_limit` if the owner never merges — so it
                # can't wedge the run. Non-writer roles (reviewer/tester, so the
                # owner's PR can still be reviewed + merged) and DEV tasks with a
                # KNOWN non-colliding path set are unaffected; with no freeze,
                # dispatch is byte-identical to before.
                if role == DEV and not tp:
                    continue
            if blocked and _paths.paths_intersect(tp, blocked):
                continue
            # F127: assign each task to an eligible (non-excluded) idle member,
            # preferring the highest tier. A task barred for every free member is
            # skipped this tick (it waits / the loop escalates).
            avail = [m for m in ids
                     if m not in used_members and m not in _excluded_member_ids(task)]
            if not avail:
                continue
            member_id = _pick_member(
                avail, member_tiers, escalate=bool(_excluded_member_ids(task))
            )
            batch.append(Assign(member_id=member_id, task_id=task.task_id, role=role))
            used_members.add(member_id)
            chosen_tasks.add(task.task_id)
            worker_assigned = True
            # F159: claim this task's hot paths so a later candidate in the SAME
            # batch touching them waits (cross-tick holds come in via hot_blocked).
            if hot:
                blocked |= {hp for hp in hot if _paths.paths_intersect(tp, {hp})}

    # PM plans only when the worker pipeline was dry this tick (and not merging).
    if pm_ids and not worker_assigned:
        batch.append(Plan(member_id=pm_ids[0]))

    if batch:
        return batch
    return [Complete(reason="no_actionable_work")]


def _ready_pm_review_task(ledger: Any) -> Optional[Task]:
    """The next ready (deps satisfied) PM PR-review task, or None (F100 PR-B).

    PM tasks aren't in ``_WORKER_PRIORITY`` (the PM is a planner/merger, not a
    fan-out worker), so a strict-mode PM PR-review task — a ``pm`` role task with
    a ``pr_id`` titled "review PR: …" — is scheduled here, ahead of the merge
    step. Uses ``next_task("pm")`` so dependency readiness matches every other
    role; falls back to a manual scan if the ledger lacks that surface."""
    nxt = getattr(ledger, "next_task", None)
    if callable(nxt):
        try:
            t = nxt(PM)
        except Exception:
            t = None
        if t is not None and getattr(t, "pr_id", None) and \
                str(getattr(t, "title", "") or "").lower().startswith("review pr:"):
            return t
    return None


def _governance_preflight(
    ledger: QueryLedger,
    by_role: dict[str, list[str]],
) -> CodingAction | None:
    try:
        from .governance_scheduler import next_governance_action
        return next_governance_action(ledger, by_role)
    except Exception:
        return None


class MutateLedger(Protocol):
    """Write surface the reconciler needs."""

    def list_tasks(self, *, state: Optional[str] = ...,
                   role: Optional[str] = ...) -> list[Task]: ...
    def update_task(self, task_id: str, **patch: Any) -> Task: ...
    def add_task(self, *, title: str, role: str, detail: str = ...,
                 depends_on: Optional[list[str]] = ...) -> Task: ...
    def record_decision(self, **kwargs: Any) -> dict[str, Any]: ...


class CodingReconciler:
    """Stateful ledger transitions for the coding loop."""

    def __init__(self, ledger: MutateLedger) -> None:
        self.ledger = ledger

    def assign(self, action: Assign) -> Task:
        """Mark the assigned task ``doing`` with its member."""
        current = next(
            (task for task in self.ledger.list_tasks()
             if task.task_id == action.task_id),
            None,
        )
        updated = self.ledger.update_task(
            action.task_id, state="doing", assignee_member_id=action.member_id,
        )
        extras = getattr(current, "_extras", {}) or {}
        prior_member = str(extras.get("reassignment_from_member_id") or "")
        if prior_member:
            attempts = int(extras.get("reassignment_attempts") or 0)
            reason = str(extras.get("reassignment_reason") or "unusable turn")
            self.ledger.record_decision(
                title=f"task reassigned: {current.title}",
                context=f"task {action.task_id}",
                choice="task_reassigned",
                rationale=(
                    f"Reassigned from {prior_member} to {action.member_id} after "
                    f"{attempts} unusable turn(s) ({reason})."
                ),
                related_task_ids=[action.task_id],
                extra={
                    "from_member_id": prior_member,
                    "to_member_id": action.member_id,
                    "attempts": attempts,
                    "reason": reason,
                },
            )
            updated = self.ledger.update_task(
                action.task_id,
                reassignment_from_member_id=None,
                reassignment_attempts=None,
                reassignment_reason=None,
            )
        return updated

    def complete_dev_task(self, task: Task) -> Task:
        """Dev finished a task -> mark done + spawn the reviewer's review task."""
        done = self.ledger.update_task(task.task_id, state="done")
        self.ledger.add_task(
            title=f"review: {task.title}", role=REVIEWER,
            detail=f"Review the work for task {task.task_id}.",
            depends_on=[task.task_id],
        )
        return done

    def complete_review_task(self, task: Task, *, approved: bool,
                             reviewed_task_id: str, reviewed_title: str) -> Task:
        """Reviewer finished. Approved -> spawn the tester's validate task.
        Not approved -> reopen the dev task for revision (a new dev task)."""
        done = self.ledger.update_task(task.task_id, state="done")
        if approved:
            self.ledger.add_task(
                title=f"validate: {reviewed_title}", role=TESTER,
                detail=f"Run + validate the work for task {reviewed_task_id}.",
                depends_on=[task.task_id],
            )
        else:
            # F141 WS-D: give the row a human "why" (no structured findings on
            # this build-review path — the reviewer's verdict is the signal).
            self.ledger.add_task(
                title=f"revise: {reviewed_title}", role=DEV,
                reason_summary="Reviewer requested changes",
                detail=f"Address review feedback on task {reviewed_task_id}.",
            )
        return done

    def block_task(self, task: Task, *, reason: str) -> Task:
        """A task cannot proceed -> mark blocked + record why (PM picks it up)."""
        blocked = self.ledger.update_task(task.task_id, state="blocked")
        self.ledger.record_decision(
            title=f"blocked: {task.title}", context=f"task {task.task_id}",
            choice="blocked", rationale=reason, related_task_ids=[task.task_id],
        )
        return blocked


def coding_role_of(member: dict[str, Any]) -> str:
    """Resolve a member's coding role from metadata.coding_role (or coding_role),
    defaulting to ``dev`` so an unmarked member is a worker, not a planner."""
    meta = member.get("metadata") or {}
    role = member.get("coding_role") or meta.get("coding_role")
    return str(role) if role in (PM, DEV, REVIEWER, TESTER) else DEV


@dataclass(frozen=True)
class _TurnProposal:
    member_id: str
    round: int
    turn_index: int
    transcript_cursor: int | None = None


@dataclass(frozen=True)
class _RunCompletion:
    reason: str
    detail: dict[str, Any] | None = None


class CodingTopology:
    """Topology-protocol adapter: reads the ledger from ``run['coding_ledger']``
    and the members' coding roles, then maps :func:`decide_next` onto the
    scheduler's ``TurnProposal | RunCompletion`` contract. The F087-03 loop runs
    the :class:`CodingReconciler` mutations between turns. Returns the project's
    real TurnProposal/RunCompletion types when available; falls back to local
        structurally-identical dataclasses for standalone testing."""

    def propose_next(self, run: dict[str, Any], transcript: list[Any]):
        try:
            from errorta_council.topologies.round_robin import (
                RunCompletion,
                TurnProposal,
            )
            TP_Proposal, TP_Complete = TurnProposal, RunCompletion
        except Exception:  # pragma: no cover - standalone
            TP_Proposal, TP_Complete = _TurnProposal, _RunCompletion

        ledger = run["coding_ledger"]
        members_raw: list[dict[str, Any]] = run["members"]
        members = [(m["id"], coding_role_of(m))
                   for m in members_raw if m.get("enabled", True)]
        counters = run.get("counters")
        turn_index = getattr(counters, "total_messages_completed", 0) if counters else 0

        action = decide_next(ledger, members)
        if isinstance(action, Complete):
            return TP_Complete(reason=action.reason)
        return TP_Proposal(member_id=action.member_id, round=0,
                           turn_index=turn_index)
