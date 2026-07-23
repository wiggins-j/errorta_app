"""F087-03 — autonomy loop + configurable stop/checkpoint controls.

Drives the F087-02 orchestration brain (decide_next + CodingReconciler) in a
loop until a configurable stop condition. The loop is DEFAULT-AUTONOMOUS: it
never pauses for a routine question; it only stops on a configured condition
(budget, definition-of-done, hard blocker, checkpoint cadence, cancel, or PM
no-progress).

The actual member turn is injected as ``run_turn`` so the loop logic is fully
unit-testable without the live model gateway; the live wiring supplies a real
``run_turn`` that runs a Council member and returns a :class:`TurnOutcome`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import paths as _paths
from .ledger import Task
from .topology import (
    DEV,
    PM,
    TESTER,
    Assign,
    CodingReconciler,
    Complete,
    GovernanceMaterialize,
    Merge,
    Plan,
    PMAssist,
    decide_next,
    plan_next_batch,
)

# --- stop reasons -----------------------------------------------------------
BUDGET_EXHAUSTED = "budget_exhausted"
DEFINITION_OF_DONE = "definition_of_done"
HARD_BLOCKER = "hard_blocker"
CHECKPOINT = "checkpoint"
CANCELLED = "cancelled"
NO_PROGRESS = "no_progress"
NO_ACTIONABLE_WORK = "no_actionable_work"
MEMBER_UNHEALTHY = "member_unhealthy"          # F120: a member can't run
WORKER_UNPRODUCTIVE = "worker_unproductive"    # F127: no member can do a task
COMPLETION_BLOCKED = "completion_blocked"      # F128: PM claimed done, open work remains
NOT_CONVERGING = "not_converging"              # F139 WS-E: nothing moved for N iterations
DELIVERY_REVIEW_STALLED = "delivery_review_stalled"  # F155: delivery review kept rejecting
GATE_NOT_IMPROVING = "gate_not_improving"      # Spec 04: acceptance gate result stuck

# --- checkpoint cadences ----------------------------------------------------
CADENCE_OFF = "off"
CADENCE_EVERY_N = "every_n_tasks"
CADENCE_PER_MILESTONE = "per_milestone"
CADENCE_ON_MERGE_READY = "on_merge_ready"


@dataclass(frozen=True)
class CodingAutonomyPolicy:
    """Configurable stop knobs. Editable mid-run (the loop re-reads it)."""
    max_iterations: int = 200
    max_model_calls: Optional[int] = None      # None = unlimited
    checkpoint_cadence: str = CADENCE_PER_MILESTONE
    checkpoint_n: int = 5                       # for CADENCE_EVERY_N
    pm_idle_limit: int = 2                      # consecutive no-progress PM turns
    # F120: consecutive unrecoverable failures of ONE member before the loop
    # raises a blocking member-health Problem and stops. Terminal reasons
    # (auth_failed/binary_missing) cap at 1 regardless via classify_aware_cap.
    member_failure_limit: int = 3
    # F127: how many UNPRODUCTIVE turns (unusable output, not a gateway failure)
    # one member may produce on one task before the task is reassigned to a
    # different (preferably stronger) member. The escalate-up ladder.
    worker_unproductive_limit: int = 2
    model_escalation_limit: int = 2
    task_reassignment_limit: int = 2
    pm_assist_limit: int = 1
    # F128: how many times the PM may falsely claim done while open work remains
    # before the loop stops with a blocking completion_blocked Problem (instead of
    # looping or accepting a false done). The PM gets re-prompted with the open
    # items in between; a productive turn resets the streak.
    completion_refused_limit: int = 2
    # F087-3: how many worker turns run at once. None = AUTO — bounded by the
    # number of worker members in the room (2 devs + 1 reviewer -> 3 in flight),
    # which is what a multi-member team expects. A positive int is an explicit
    # throttle (e.g. to cap model spend); 1 = the original sequential loop.
    max_parallel_workers: Optional[int] = None
    # F139 WS-A: while a `new` project's foundation has not merged (a build
    # manifest + a source entrypoint on master), worker concurrency is clamped to
    # 1 so the team scaffolds ONE coherent base before fanning out. If the
    # foundation still has not merged after this many clamped iterations, a
    # `foundation_not_converging` attention signal is raised (the run continues,
    # clamped, so a human can intervene).
    foundation_stall_limit: int = 12
    # F139 WS-E: if NOTHING moves — no net-new merged files, no PR state
    # transition, no F127 ladder activity — for this many consecutive iterations,
    # the run stops with `not_converging` instead of burning the iteration cap.
    convergence_stall_limit: int = 20
    # F155: how many times the delivery review may REJECT the integrated head
    # (filing fix findings) before the run stops with `delivery_review_stalled`.
    # A filed finding resets pm_idle (it IS progress) and changes the head, so
    # neither no_progress nor not_converging trips — without this cap a run that
    # keeps failing delivery review loops to budget_exhausted instead of stopping
    # truthfully. A passing review resets the count.
    delivery_review_round_limit: int = 3
    # F159: hot-file serialization. A path that appears in >= this many PRs'
    # `conflicts` is "hot" — parallel edits to it are serialized (one owner until
    # its PR merges). If it keeps conflicting past the escalation threshold, the
    # engine centralizes it (reuses the F139 WS-D2 contract-owner task) and freezes
    # direct parallel edits until that task merges; the freeze force-lifts (with an
    # alert) after the stall limit so a never-merging owner can't starve the file.
    hot_file_threshold: int = 2
    hot_file_escalation_threshold: int = 4
    hot_file_freeze_stall_limit: int = 15
    # Spec 04: if the ACCEPTANCE GATE RESULT (test-run pass count / delivery
    # verdict) does not IMPROVE for this many iterations, stop `gate_not_improving`
    # instead of churning the same failing solver->gate->same-result loop to
    # budget_exhausted. Unlike `convergence_stall_limit`, this keys on the gate
    # result (not the progress fingerprint), so a churning PR head does NOT reset
    # it. 0 disables the detector.
    gate_stall_limit: int = 8


def policy_to_dict(p: CodingAutonomyPolicy) -> dict[str, Any]:
    return {
        "max_iterations": p.max_iterations, "max_model_calls": p.max_model_calls,
        "checkpoint_cadence": p.checkpoint_cadence, "checkpoint_n": p.checkpoint_n,
        "pm_idle_limit": p.pm_idle_limit,
        "member_failure_limit": p.member_failure_limit,
        "worker_unproductive_limit": p.worker_unproductive_limit,
        "model_escalation_limit": p.model_escalation_limit,
        "task_reassignment_limit": p.task_reassignment_limit,
        "pm_assist_limit": p.pm_assist_limit,
        "completion_refused_limit": p.completion_refused_limit,
        "max_parallel_workers": p.max_parallel_workers,
        "foundation_stall_limit": p.foundation_stall_limit,
        "convergence_stall_limit": p.convergence_stall_limit,
        "delivery_review_round_limit": p.delivery_review_round_limit,
        "hot_file_threshold": p.hot_file_threshold,
        "hot_file_escalation_threshold": p.hot_file_escalation_threshold,
        "hot_file_freeze_stall_limit": p.hot_file_freeze_stall_limit,
        "gate_stall_limit": p.gate_stall_limit,
    }


def policy_from_dict(d: dict[str, Any]) -> CodingAutonomyPolicy:
    base = CodingAutonomyPolicy()
    raw_workers = d.get("max_parallel_workers", base.max_parallel_workers)
    workers = None if raw_workers is None else max(1, int(raw_workers))
    return CodingAutonomyPolicy(
        max_iterations=int(d.get("max_iterations", base.max_iterations)),
        max_model_calls=d.get("max_model_calls", base.max_model_calls),
        checkpoint_cadence=str(d.get("checkpoint_cadence", base.checkpoint_cadence)),
        checkpoint_n=int(d.get("checkpoint_n", base.checkpoint_n)),
        pm_idle_limit=int(d.get("pm_idle_limit", base.pm_idle_limit)),
        member_failure_limit=max(
            1, int(d.get("member_failure_limit", base.member_failure_limit))),
        worker_unproductive_limit=max(
            1, int(d.get("worker_unproductive_limit", base.worker_unproductive_limit))),
        model_escalation_limit=max(
            0, int(d.get("model_escalation_limit", base.model_escalation_limit))),
        task_reassignment_limit=max(
            0, int(d.get("task_reassignment_limit", base.task_reassignment_limit))),
        pm_assist_limit=max(1, int(d.get("pm_assist_limit", base.pm_assist_limit))),
        completion_refused_limit=max(
            1, int(d.get("completion_refused_limit", base.completion_refused_limit))),
        max_parallel_workers=workers,
        foundation_stall_limit=max(
            1, int(d.get("foundation_stall_limit", base.foundation_stall_limit))),
        convergence_stall_limit=max(
            1, int(d.get("convergence_stall_limit", base.convergence_stall_limit))),
        delivery_review_round_limit=max(
            1, int(d.get("delivery_review_round_limit", base.delivery_review_round_limit))),
        hot_file_threshold=max(
            1, int(d.get("hot_file_threshold", base.hot_file_threshold))),
        hot_file_escalation_threshold=max(
            1, int(d.get("hot_file_escalation_threshold", base.hot_file_escalation_threshold))),
        hot_file_freeze_stall_limit=max(
            1, int(d.get("hot_file_freeze_stall_limit", base.hot_file_freeze_stall_limit))),
        # Spec 04: `max(0, …)` — NOT max(1) — so an operator can set 0 to disable
        # the gate-stall detector entirely. Absent key -> dataclass default (8).
        gate_stall_limit=max(
            0, int(d.get("gate_stall_limit", base.gate_stall_limit))),
    )


# --- F159: hot-file serialization ------------------------------------------ #

def hot_files(ledger: Any, *, threshold: int) -> dict[str, int]:
    """Map ``path -> conflict_count`` over the PR history, keeping only paths that
    have conflicted at least ``threshold`` times (the "hot" files). Built from the
    durable per-PR ``conflicts`` lists (git repo-relative paths). Cheap enough to
    compute ONCE per iteration — never per dispatch candidate (``list_prs`` re-reads
    prs.json)."""
    list_prs = getattr(ledger, "list_prs", None)
    if not callable(list_prs):
        return {}
    counts: dict[str, int] = {}
    for pr in list_prs():
        for raw in (pr.get("conflicts") or []):
            p = _paths.normalize_path(str(raw))
            if p:
                counts[p] = counts.get(p, 0) + 1
    return {p: n for p, n in counts.items() if n >= max(1, threshold)}


def hot_owned_paths(ledger: Any, hot: dict[str, int]) -> set[str]:
    """The subset of hot paths currently held by an active DEV task — one that is
    ``doing`` or has an open (un-merged) PR. A hot path with a live owner must not
    be handed to a second task until that owner's PR merges (the conflict surfaces
    at merge, so the hold is merge-scoped, not turn-scoped)."""
    if not hot:
        return set()
    hot_set = set(hot)
    owned: set[str] = set()
    live_pr_tasks: set[str] = set()
    # F159: the OBSERVED files each live PR actually changed, keyed by task. This is
    # the reliable ownership signal where prose/`target_files` are silent — a dev
    # task titled "Add real-time activity indicators" that appends to a hot file
    # names it nowhere, so without this its open PR would own nothing and the
    # merge-scoped hold would never engage (the mockData.ts thrash).
    observed_by_task: dict[str, set[str]] = {}
    list_prs = getattr(ledger, "list_prs", None)
    if callable(list_prs):
        for pr in list_prs():
            if pr.get("status") not in ("merged", "superseded", "abandoned", "closed"):
                tid = pr.get("task_id")
                if tid:
                    live_pr_tasks.add(str(tid))
                    changed = {_paths.normalize_path(str(p))
                               for p in (pr.get("changed_paths") or []) if p}
                    if changed:
                        observed_by_task.setdefault(str(tid), set()).update(changed)
    for task in ledger.list_tasks(role=DEV):
        if task.state != "doing" and task.task_id not in live_pr_tasks:
            continue
        tp = _paths.task_touched_paths(task) | observed_by_task.get(task.task_id, set())
        for hp in hot_set:
            if _paths.paths_intersect(tp, {hp}):
                owned.add(hp)
    return owned


def frozen_paths(ledger: Any) -> set[str]:
    """Paths under a F159 centralize-freeze (only the contract-owner task may touch
    them until it merges). Stored on ``run_state.frozen_paths``."""
    try:
        raw = ledger.get_run_state().get("frozen_paths") or []
    except Exception:  # noqa: BLE001
        return set()
    return {_paths.normalize_path(str(p)) for p in raw if p}


def effective_parallelism(policy: CodingAutonomyPolicy,
                          members: list[tuple[str, str]]) -> int:
    """How many worker turns may run at once for this team. AUTO (None) is the
    worker-member count (non-PM); an explicit int is honored as a hard cap."""
    if policy.max_parallel_workers is None:
        workers = sum(1 for _mid, role in members if role != PM)
        return max(1, workers)
    return max(1, int(policy.max_parallel_workers))


def foundation_pending(ledger: Any) -> bool:
    """F139 WS-A: True while a `new` project's foundation has not yet merged to
    master. The runner derives this from git after each merge and persists it on
    run_state (`foundation_status`); the loop reads it here to clamp concurrency.
    Absent/unknown → not pending (never clamp a run whose runner didn't opt in)."""
    try:
        return str(ledger.get_run_state().get("foundation_status", "")) == "pending"
    except Exception:  # noqa: BLE001 — a run_state hiccup must never clamp/crash
        return False


def _feature_merges(ledger: Any) -> int:
    """Count merged PRs (proxy for 'has a feature slice integrated cleanly yet').
    Used by the WS-D concurrency ramp."""
    try:
        return sum(1 for p in ledger.list_prs() if p.get("status") == "merged")
    except Exception:  # noqa: BLE001
        return 0


def runtime_cap(policy: CodingAutonomyPolicy, members: list[tuple[str, str]],
                ledger: Any) -> int:
    """F139 WS-A/WS-D: the effective worker concurrency for THIS iteration, layering
    the foundation gate + ramp over the static `effective_parallelism`.

    The gate is OPT-IN on ``run_state.foundation_status`` being set (the runner
    seeds it for real runs; a bare ``run_coding_loop`` in a unit test does not, and
    so keeps the full static parallelism — no behavioural change for those). When
    engaged:

    * ``pending``  -> 1 (scaffold one coherent base before fan-out), ALWAYS, even
      when an explicit ``max_parallel_workers`` is set;
    * ``merged`` but only the foundation has merged (<= 1 merged PR), AUTO
      concurrency -> min(2, base): ease in until the first FEATURE lands cleanly;
    * otherwise -> the static base.
    """
    base = effective_parallelism(policy, members)
    try:
        fstatus = str(ledger.get_run_state().get("foundation_status", ""))
    except Exception:  # noqa: BLE001
        fstatus = ""
    if not fstatus:
        return base  # foundation gate not engaged for this run
    if fstatus == "pending":
        return 1
    if policy.max_parallel_workers is None and _feature_merges(ledger) <= 1:
        return min(2, base)
    return base


def _progress_fingerprint(ledger: Any, c: "LoopCounters") -> tuple:
    """F139 WS-E: a cheap snapshot of 'is anything moving anywhere?'. It changes iff
    there was ANY of:

    * net-new merged work — a PR state/head transition or an increase in the merged
      count (AC-17 'net-new merged files');
    * a task-set change — a task added / dropped / changed state, so a PM that is
      productively (re)planning, or a dev completing a task, counts as motion (this
      is the fix for the 'productive PM-only planning stalls the run' false-fire);
    * F127 ladder activity — reassign / escalate / pm-assist / unproductive counts;
    * a foundation-status flip.

    When two quiescent checks produce the SAME fingerprint, NOTHING moved.

    NOTE (division of labour): reddit-style *busy* churn (open PR -> reject ->
    revise -> new PR ...) keeps changing this fingerprint, so WS-E does NOT stop it
    by design — that pathology is caught by Part A's WS-C no-op/unproductive guard
    (empty re-emits -> F127 ladder -> stop). WS-E is the backstop for genuine
    QUIESCENCE (nothing touching the ledger at all). Do not coarsen this fingerprint
    to try to catch busy churn — that reintroduces false positives."""
    try:
        prs = ledger.list_prs()
        pr_fp = tuple(sorted((str(p.get("pr_id")), str(p.get("status")),
                              str(p.get("head"))) for p in prs))
    except Exception:  # noqa: BLE001
        pr_fp = ()
    try:
        task_fp = tuple(sorted((t.task_id, t.state) for t in ledger.list_tasks()))
    except Exception:  # noqa: BLE001
        task_fp = ()
    ladder = (c.task_reassignments, c.model_escalations, c.pm_assists,
              c.tasks_done, sum(c.unproductive_counts.values()),
              _feature_merges(ledger))
    foundation = ""
    try:
        foundation = str(ledger.get_run_state().get("foundation_status", ""))
    except Exception:  # noqa: BLE001
        pass
    return (pr_fp, task_fp, ladder, foundation)


def _gate_fingerprint(ledger: Any) -> tuple[tuple, int]:
    """Spec 04: a snapshot of the ACCEPTANCE GATE RESULT — the test-run pass set
    and the delivery-review verdict — keyed on the RESULT, NOT the PR head.

    Returns ``(fp, score)`` where ``fp`` identifies the current gate state and
    ``score`` is a monotonic quality measure (higher = better): the count of
    passing commands, with a passing delivery review dominating. ``_account_gate_stall``
    treats a strict score increase as motion (reset) and an unchanged/lower score
    as churn (a step toward the stall stop) — so a run stuck at 6/12 while the PR
    head keeps changing finally trips.

    Sentinel ``((), -1)`` means "no gate signal yet" (no test run and no delivery
    verdict) — the detector never trips on it. All ledger access is guarded so a
    ledger lacking these methods degrades to the sentinel rather than crashing the
    loop."""
    fp_parts: list = []
    score = -1

    list_test_runs = getattr(ledger, "list_test_runs", None)
    if callable(list_test_runs):
        try:
            runs = list_test_runs()
        except Exception:  # noqa: BLE001
            runs = None
        if runs:
            latest = runs[-1]
            results = latest.get("results") or []
            if results:
                fp_parts.append(tuple(sorted(
                    (str(r.get("command_id")), r.get("exit_code")) for r in results)))
                score = sum(1 for r in results if r.get("exit_code") == 0)
            elif latest.get("passed") is not None:
                passed = bool(latest.get("passed"))
                fp_parts.append(("run_passed", passed))
                score = 1 if passed else 0

    get_run_state = getattr(ledger, "get_run_state", None)
    if callable(get_run_state):
        try:
            state = get_run_state() or {}
        except Exception:  # noqa: BLE001
            state = {}
        if state.get("delivery_review_passed") is True:
            score = max(score if score >= 0 else 0, 10_000)
            fp_parts.append(("delivery_review_passed", True,
                             str(state.get("delivery_reviewed_head", ""))))

    if score < 0:
        return ((), -1)
    return (tuple(fp_parts), score)


def load_policy(store: Any) -> CodingAutonomyPolicy:
    """Read the per-project autonomy policy from the ledger (defaults if unset)."""
    path = store.dir / "autonomy.json"
    if not path.exists():
        return CodingAutonomyPolicy()
    import json
    return policy_from_dict(json.loads(path.read_text("utf-8")))


def save_policy(store: Any, policy: CodingAutonomyPolicy) -> CodingAutonomyPolicy:
    from .ledger import _atomic_write_json
    _atomic_write_json(store.dir / "autonomy.json", policy_to_dict(policy))
    return policy


# The run caps operators set (and that Spec 01 makes observable). A cap ABSENT
# from autonomy.json is served from the dataclass default — indistinguishable at
# runtime from an explicitly-persisted equal value without this provenance read.
CAP_KEYS = (
    "max_iterations",
    "max_model_calls",
    "max_parallel_workers",
    "delivery_review_round_limit",
)


def policy_with_provenance(store: Any) -> tuple[dict[str, Any], list[str]]:
    """Return ``(policy_to_dict(load_policy(store)), defaulted_keys)`` where
    ``defaulted_keys`` lists the :data:`CAP_KEYS` that are ABSENT from the raw
    ``autonomy.json`` on disk (so their effective value came from the dataclass
    default). A missing or unreadable file → all cap keys are defaulted. This is
    the read side of Spec 01: it lets ``errorta status`` mark a silent
    fallback-to-default that ``load_policy`` alone cannot detect."""
    import json

    path = store.dir / "autonomy.json"
    raw_keys: set[str] = set()
    try:
        raw = json.loads(path.read_text("utf-8"))
        if isinstance(raw, dict):
            raw_keys = set(raw.keys())
    except (FileNotFoundError, ValueError, OSError):
        raw_keys = set()
    defaulted = [k for k in CAP_KEYS if k not in raw_keys]
    return policy_to_dict(load_policy(store)), defaulted


@dataclass
class TurnOutcome:
    """What a member turn did — drives the reconciler + counters."""
    kind: str  # task_done | review_done | task_blocked | planned | project_done | noop
    task: Optional[Task] = None
    approved: bool = False
    reviewed_task_id: Optional[str] = None
    reviewed_title: Optional[str] = None
    reason: str = ""
    hard_blocker: bool = False
    made_progress: bool = True                 # for planned turns
    model_calls: int = 1
    # F120: when a member CALL itself failed (logged-out CLI, missing binary,
    # 401/429, unparseable output), the runner surfaces the classified failure
    # here instead of swallowing it into a bare noop. The loop counts consecutive
    # per-member failures and raises a blocking member-health Problem at the cap.
    member_id: str = ""
    member_failure: Optional[Any] = None       # member_health.MemberFailure
    member_role: str = ""                       # F120: coding role of the member
    member_route: str = ""                       # F120: gateway_route_id / provider
    # F127: a worker turn that connected fine but produced an UNUSABLE turn
    # (tool-call markup / schema mismatch) after the corrective retries — distinct
    # from a member_failure (gateway). Drives the escalate-up reassignment ladder.
    unproductive: bool = False
    repairs: int = 0


@dataclass
class LoopCounters:
    iterations: int = 0
    model_calls: int = 0
    tasks_done: int = 0
    since_checkpoint: int = 0
    pm_idle: int = 0
    # F120: consecutive unrecoverable failures per member_id. Reset to 0 on the
    # first `ok` turn for that member; at classify_aware_cap the loop raises a
    # blocking member-health Problem and stops.
    member_fail_counts: dict[str, int] = field(default_factory=dict)
    # F127: consecutive UNPRODUCTIVE turns per (member_id, task_id). At
    # worker_unproductive_limit the task is reassigned away from that member.
    unproductive_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    turns_repaired: int = 0
    task_reassignments: int = 0
    model_escalations: int = 0
    pm_assists: int = 0
    # F128: consecutive PM done=true claims refused because open work remained.
    # Reset on any productive turn; at completion_refused_limit the loop stops
    # with a blocking completion_blocked Problem.
    false_done_streak: int = 0
    # F139 WS-A: consecutive clamped iterations spent with the foundation still
    # pending. At foundation_stall_limit a `foundation_not_converging` signal is
    # raised once (the run continues, clamped). Reset when the foundation merges.
    foundation_stall: int = 0
    foundation_alerted: bool = False
    # F159: consecutive iterations spent with a hot-file freeze active. At
    # hot_file_freeze_stall_limit the freeze is force-lifted (the centralize owner
    # isn't landing) so the file's work can resume. Reset when no freeze is active.
    hot_freeze_stall: int = 0
    # F139 WS-E: convergence tracking. `last_progress_fp` is the last-seen
    # `_progress_fingerprint`; `last_progress_iter` is the iteration count when it
    # last changed. When `iterations - last_progress_iter` reaches
    # convergence_stall_limit — i.e. nothing moved for that many iterations — the
    # run stops `not_converging`. Iteration-based so the sequential and concurrent
    # loops behave identically.
    last_progress_fp: tuple = ()
    last_progress_iter: int = 0
    # Spec 04: gate-stall tracking, keyed on the ACCEPTANCE RESULT (test-run pass
    # count / delivery verdict), NOT the progress fingerprint. `last_gate_fp` is
    # the last-seen `_gate_fingerprint` fp; `last_gate_best` is the best (highest)
    # score observed so far (-1 = never observed); `last_gate_iter` is the
    # iteration count when the score last strictly improved. When
    # `iterations - last_gate_iter >= gate_stall_limit` — the gate result has not
    # improved for that many iterations — the run stops `gate_not_improving`. A
    # changed fp with an equal/lower score is CHURN and does NOT reset (that's the
    # 6/12-with-a-changing-head loop we must catch).
    last_gate_fp: tuple = ()
    last_gate_best: int = -1
    last_gate_iter: int = 0
    # F155: consecutive delivery-review rejections (findings filed) in this run.
    # At delivery_review_round_limit the loop stops `delivery_review_stalled`.
    # Reset to 0 on a PASSING delivery review.
    delivery_review_rounds: int = 0


@dataclass
class LoopResult:
    stop_reason: str
    counters: LoopCounters
    detail: dict[str, Any] = field(default_factory=dict)


RunTurn = Callable[[Any, Any], TurnOutcome]  # (action, ledger) -> outcome


def reserve_model_calls(counters: LoopCounters, policy: CodingAutonomyPolicy,
                        candidate: int) -> int:
    """F087 Slice 0 — strict ``max_model_calls`` budget reservation.

    Given a batch of ``candidate`` model-call-consuming turns the runtime is
    about to dispatch, return how many may run without exceeding
    ``max_model_calls``. The concurrent loop (Slice 3) calls this BEFORE
    dispatch and shrinks the batch to the result, so parallel dispatch can never
    overshoot the cap (no per-batch overshoot). ``max_model_calls=None`` means
    unlimited. Mechanical turns (a PM ``Merge``) cost 0 model calls and must be
    excluded from ``candidate`` by the caller."""
    if candidate <= 0:
        return 0
    if policy.max_model_calls is None:
        return candidate
    remaining = policy.max_model_calls - counters.model_calls
    if remaining <= 0:
        return 0
    return min(candidate, remaining)


def _checkpoint_due(policy: CodingAutonomyPolicy, counters: LoopCounters,
                    milestone: bool) -> bool:
    cad = policy.checkpoint_cadence
    if cad == CADENCE_OFF:
        return False
    if cad == CADENCE_EVERY_N:
        return counters.since_checkpoint >= policy.checkpoint_n
    if cad == CADENCE_PER_MILESTONE:
        return milestone
    # on_merge_ready is handled by definition-of-done completion.
    return False


def run_coding_loop(
    ledger: Any,
    members: list[tuple[str, str]],
    policy: CodingAutonomyPolicy,
    *,
    run_turn: RunTurn,
    reconciler: Optional[CodingReconciler] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    counters: Optional[LoopCounters] = None,
    policy_provider: Optional[Callable[[], CodingAutonomyPolicy]] = None,
    member_tiers: Optional[dict[str, int]] = None,
    delivery_review: Optional[Callable[[Any], Any]] = None,
) -> LoopResult:
    """Run the autonomous coding loop until a stop condition. Resumable: pass the
    returned ``counters`` back in to continue after a checkpoint/pause. If
    ``policy_provider`` is given it is re-read each iteration, so a mid-run edit
    to the project's autonomy policy (e.g. changing the checkpoint cadence) takes
    effect at the next turn.

    When ``policy.max_parallel_workers > 1`` the loop dispatches a batch of ready
    worker turns concurrently (F087-3); ``<= 1`` keeps the original single-action
    semantics exactly (decide_next, one turn per iteration).

    F146 Slice B: ``delivery_review`` (optional; None keeps the pre-F146 behavior)
    verifies the integrated delivered head before a ``project_done`` is accepted."""
    rec = reconciler or CodingReconciler(ledger)
    c = counters or LoopCounters()

    effective = policy_provider() if policy_provider is not None else policy
    if runtime_cap(effective, members, ledger) > 1:
        return _run_concurrent_loop(
            ledger, members, effective, run_turn=run_turn, rec=rec,
            should_cancel=should_cancel, c=c, policy_provider=policy_provider,
            member_tiers=member_tiers, delivery_review=delivery_review,
        )
    return _run_sequential_loop(
        ledger, members, policy, run_turn=run_turn, rec=rec,
        should_cancel=should_cancel, c=c, policy_provider=policy_provider,
        member_tiers=member_tiers, delivery_review=delivery_review,
    )


def _maybe_raise_monitor(ledger: Any, detector: str, reason: str) -> None:
    """F117-03 Progress Monitor producer: surface a stuck *governed* run as an
    attention Problem so a human is told, instead of the run just ending opaquely.

    Best-effort — wrapped so a signal-store hiccup can never break the run loop.
    Only governed runs (mode != off) have a governance stage to key the Problem on;
    ungoverned runs are skipped.
    """
    try:
        from . import attention
        from .governance import GovernanceStore
        state = GovernanceStore.for_ledger(ledger).load_state()
        if state.mode == "off":
            return
        signal = attention.raise_monitor_problem(
            ledger.project_id, stage=state.phase, detector=detector,
            reason=reason or detector, store=ledger,
        )
        if not state.block_on_problems:
            signal = signal or attention.find_open_monitor_problem(
                ledger.project_id, stage=state.phase, detector=detector,
                store=ledger,
            )
            if signal is not None:
                attention.auto_resolve(ledger.project_id, signal.id, store=ledger)
    except Exception:  # noqa: BLE001 - producer must never break the run loop
        pass


def _account_foundation_stall(ledger: Any, c: LoopCounters,
                              policy: CodingAutonomyPolicy) -> None:
    """F139 WS-A: while the foundation is pending, count clamped iterations; at
    ``foundation_stall_limit`` surface a `foundation_not_converging` signal ONCE
    (the run keeps going, clamped to 1, so a human can guide the PM). Reset when
    the foundation merges. Best-effort — never breaks the loop."""
    try:
        if not foundation_pending(ledger):
            c.foundation_stall = 0
            c.foundation_alerted = False
            return
        c.foundation_stall += 1
        limit = max(1, policy.foundation_stall_limit)
        # Re-alert every `limit` clamped iterations (a heartbeat), not once: a run
        # can span checkpoints/resumes with fresh counters, and the monitor signal
        # may auto-resolve when block_on_problems is off — a single alert could be
        # missed. `_maybe_raise_monitor` dedups an already-open signal, so this
        # re-raises only after a prior one resolved.
        if c.foundation_stall % limit != 0:
            return
        c.foundation_alerted = True
        try:
            ledger.record_decision(
                title="foundation not converging",
                context="foundation_gate",
                choice="foundation_not_converging",
                rationale=(
                    "no build manifest + source entrypoint has merged to master "
                    f"after {c.foundation_stall} clamped iterations; worker "
                    "concurrency stays at 1 until the scaffold lands — a human may "
                    "need to guide the PM"),
            )
        except Exception:  # noqa: BLE001
            pass
        _maybe_raise_monitor(ledger, "foundation_not_converging",
                             "foundation has not merged to master")
    except Exception:  # noqa: BLE001
        pass


def _account_hot_file_freeze(ledger: Any, c: LoopCounters,
                             policy: CodingAutonomyPolicy) -> None:
    """F159 never-lift guard: a hot-file freeze normally lifts when the centralize
    owner's PR merges (runner). If that PR never lands, the file would stay frozen
    forever — so count iterations under an active freeze and force-lift at
    ``hot_file_freeze_stall_limit`` (with a decision + monitor), so the file's work
    resumes and a human is told. Best-effort — never breaks the loop."""
    try:
        if not frozen_paths(ledger):
            c.hot_freeze_stall = 0
            return
        c.hot_freeze_stall += 1
        if c.hot_freeze_stall < max(1, policy.hot_file_freeze_stall_limit):
            return
        c.hot_freeze_stall = 0
        try:
            ledger.set_run_state(frozen_paths=[])
        except Exception:  # noqa: BLE001
            pass
        try:
            ledger.record_decision(
                title="hot-file freeze force-lifted",
                context="hot_file", choice="hot_file_freeze_stalled",
                rationale=("the shared-contract owner did not merge within "
                           f"{policy.hot_file_freeze_stall_limit} iterations; lifting "
                           "the freeze so the file's work can resume — a human may "
                           "need to look"))
        except Exception:  # noqa: BLE001
            pass
        _maybe_raise_monitor(ledger, "hot_file_freeze_stalled",
                             "a hot-file centralize task did not merge in time")
    except Exception:  # noqa: BLE001
        pass


def _account_convergence(ledger: Any, c: LoopCounters,
                         policy: CodingAutonomyPolicy) -> Optional[LoopResult]:
    """F139 WS-E: detect a run where NOTHING is moving. Compares a cheap progress
    fingerprint (merged heads + PR states + ladder activity) against the last one;
    if it has not changed for ``convergence_stall_limit`` iterations, stop
    `not_converging`. Resets on any motion, so a normal review/rework/self-heal
    cycle (which keeps opening/transitioning PRs or running the ladder) never trips
    it. Returns a stop result or None."""
    try:
        fp = _progress_fingerprint(ledger, c)
    except Exception:  # noqa: BLE001
        return None
    if fp != c.last_progress_fp:
        c.last_progress_fp = fp
        c.last_progress_iter = c.iterations
        return None
    if c.iterations - c.last_progress_iter < max(1, policy.convergence_stall_limit):
        return None
    _maybe_raise_monitor(
        ledger, "not_converging",
        "no merged progress, PR transition, or ladder activity")
    return LoopResult(NOT_CONVERGING, c)


def _account_gate_stall(ledger: Any, c: LoopCounters,
                        policy: CodingAutonomyPolicy) -> Optional[LoopResult]:
    """Spec 04: detect a run whose ACCEPTANCE GATE RESULT hasn't IMPROVED for
    ``gate_stall_limit`` iterations, and stop `gate_not_improving`.

    Clones ``_account_convergence`` but keys on ``_gate_fingerprint`` (test-run
    pass count / delivery verdict) instead of the progress fingerprint — precisely
    because a churning PR head keeps the progress fingerprint moving (so
    `not_converging` never fires) while the gate result stays byte-identical.

    Improvement = a STRICT score increase (more commands passing, or delivery
    flips to passed); that resets the window. A changed fp with an equal or lower
    score is CHURN and does NOT reset — that is the 6/12-with-a-changing-head loop
    this detector exists to catch. ``gate_stall_limit == 0`` disables it; a
    no-signal sentinel (score < 0) never trips."""
    if policy.gate_stall_limit <= 0:
        return None
    fp, score = _gate_fingerprint(ledger)
    if score < 0:
        return None  # no gate signal yet — never trips
    # First-ever observation, or a strict score improvement: motion — reset the
    # window and remember this as the new best.
    if c.last_gate_best == -1 or score > c.last_gate_best:
        c.last_gate_fp = fp
        c.last_gate_best = score
        c.last_gate_iter = c.iterations
        return None
    if c.iterations - c.last_gate_iter < policy.gate_stall_limit:
        return None
    _maybe_raise_monitor(
        ledger, "gate_not_improving",
        f"acceptance gate has not improved for {policy.gate_stall_limit} "
        f"iterations (score={score})")
    return LoopResult(GATE_NOT_IMPROVING, c)


def _maybe_raise_member_health(
    ledger: Any, member_id: str, role: str, route: str,
    failure: Any, attempts: int,
) -> None:
    """F120 producer: surface a terminally-unhealthy member as a blocking
    attention Problem (source=member_health) so the user is told exactly which
    member/provider failed, why, and how to fix it — instead of the run looping
    silently. Best-effort: a signal-store hiccup must never break the run loop.

    Mirrors ``_maybe_raise_monitor``: deduped per (member_id, reason); when
    ``block_on_problems`` is off the signal is auto-resolved but stays visible.
    """
    try:
        from . import attention
        from .governance import GovernanceStore
        state = GovernanceStore.for_ledger(ledger).load_state()
        stage = state.phase if state.mode != "off" else "idle"
        signal = attention.raise_member_health_problem(
            ledger.project_id, member_id=member_id, role=role, route=route,
            reason=failure.status, detail=failure.detail,
            remediation=failure.remediation, attempts=attempts,
            stage=stage, store=ledger,
        )
        if state.mode != "off" and not state.block_on_problems:
            signal = signal or attention.find_open_member_health_problem(
                ledger.project_id, member_id=member_id, reason=failure.status,
                store=ledger,
            )
            if signal is not None:
                attention.auto_resolve(ledger.project_id, signal.id, store=ledger)
    except Exception:  # noqa: BLE001 - producer must never break the run loop
        pass


def _handle_unproductive(
    ledger: Any, action: Any, outcome: TurnOutcome, c: LoopCounters,
    policy: CodingAutonomyPolicy, members: list[tuple[str, str]],
) -> Optional[str]:
    """F127 escalate-up ladder. Count an UNPRODUCTIVE worker turn for
    ``(member_id, task_id)``; at ``worker_unproductive_limit`` exclude that member
    from the task and reassign (the scheduler then prefers a higher tier). When
    same-role recovery is exhausted, schedule the bounded PM-assist rung; return
    ``WORKER_UNPRODUCTIVE`` only when no PM exists or the ladder itself fails.
    Never raises into the loop."""
    try:
        member_id = str(getattr(action, "member_id", "") or outcome.member_id or "")
        task_id = str(getattr(action, "task_id", "") or "")
        role = str(getattr(action, "role", "") or outcome.member_role or "")
        if not (member_id and task_id):
            return None
        key = (member_id, task_id)
        c.unproductive_counts[key] = c.unproductive_counts.get(key, 0) + 1
        if c.unproductive_counts[key] < max(1, policy.worker_unproductive_limit):
            return None  # let the same member retry up to the limit

        task = next((t for t in ledger.list_tasks() if t.task_id == task_id), None)
        if task is None:
            return WORKER_UNPRODUCTIVE
        extras = getattr(task, "_extras", {}) or {}
        # F129 inserts a bounded, strictly-stronger route rung before F127
        # excludes the member. Corrective retries have already been exhausted.
        from .model_assignment import next_escalation_assignment

        current_assignment = getattr(task, "model_assignment", None) or {}
        current_escalations = int(current_assignment.get("escalation_count") or 0)
        next_assignment = (
            next_escalation_assignment(task)
            if current_escalations < policy.model_escalation_limit
            else None
        )
        if next_assignment is not None:
            attempts = c.unproductive_counts[key]
            c.unproductive_counts[key] = 0
            c.model_escalations += 1
            ledger.update_task(
                task_id,
                state="todo",
                assignee_member_id=None,
                preferred_member_id=member_id,
                model_assignment=next_assignment.to_dict(),
                model_escalation_attempts=attempts,
                model_escalation_reason=outcome.reason or "unparseable",
            )
            ledger.record_decision(
                title=f"task model escalated: {task.title or task_id}",
                context=f"task {task_id}", choice="task_model_escalated",
                rationale=(
                    f"{outcome.member_route} produced {attempts} unusable turn(s); "
                    f"retrying the same member with strictly stronger route "
                    f"{next_assignment.route_id}."
                ),
                related_task_ids=[task_id],
                extra={
                    "member_id": member_id,
                    "from_route_id": outcome.member_route,
                    "to_route_id": next_assignment.route_id,
                    "assignment_id": next_assignment.assignment_id,
                    "escalation_count": next_assignment.escalation_count,
                },
            )
            return None
        prior = extras.get("excluded_member_ids") or []
        excluded = set(prior) | {member_id}
        failed_routes = dict(extras.get("excluded_member_routes") or {})
        failed_routes[member_id] = outcome.member_route
        role_members = {mid for mid, r in members if r == role}
        eligible = role_members - excluded
        attempts = c.unproductive_counts[key]
        c.unproductive_counts[key] = 0
        reassignments = int(extras.get("task_reassignment_count") or 0)
        can_reassign = bool(eligible) and reassignments < policy.task_reassignment_limit
        common_patch = {
            "state": "todo",
            "assignee_member_id": None,
            "excluded_member_ids": sorted(excluded),
            "excluded_member_routes": failed_routes,
        }
        if can_reassign:
            c.task_reassignments += 1
            ledger.update_task(
                task_id,
                **common_patch,
                task_reassignment_count=reassignments + 1,
                reassignment_from_member_id=member_id,
                reassignment_attempts=attempts,
                reassignment_reason=outcome.reason or "unparseable",
            )
            ledger.record_decision(
                title=f"worker excluded: {task.title or task_id}",
                context=f"task {task_id}", choice="worker_excluded",
                rationale=(
                    f"{member_id} produced {attempts} unusable turn(s) "
                    f"({outcome.reason or 'unparseable'}); selecting a different "
                    "eligible member."
                ),
                related_task_ids=[task_id],
            )
            return None

        pm_ids = [mid for mid, member_role in members if member_role == PM]
        if pm_ids:
            ledger.update_task(
                task_id,
                **common_patch,
                pm_assist_pending=True,
                pm_assist_limit=policy.pm_assist_limit,
            )
            ledger.record_decision(
                title=f"PM assist requested: {task.title or task_id}",
                context=f"task {task_id}", choice="pm_assist_requested",
                rationale=(
                    f"Same-role recovery is exhausted after {len(excluded)} member(s); "
                    "the PM must split or re-scope the task before human attention."
                ),
                related_task_ids=[task_id],
            )
            return None

        ledger.update_task(task_id, **common_patch)
        _raise_worker_unproductive_problem(
            ledger, task, excluded, member_id, outcome.member_route,
            outcome.reason or "unparseable",
        )
        return WORKER_UNPRODUCTIVE
    except Exception:  # noqa: BLE001 - stop visibly; never fall back to a silent loop
        logging.getLogger("errorta.coding").exception(
            "worker-unproductive escalation failed: member=%s task=%s",
            getattr(action, "member_id", ""),
            getattr(action, "task_id", ""),
        )
        return WORKER_UNPRODUCTIVE


def _raise_worker_unproductive_problem(
    ledger: Any,
    task: Task,
    members_tried: set[str],
    last_member: str,
    last_route: str,
    last_error: str,
) -> None:
    from . import attention as _attention

    _attention.raise_worker_unproductive_problem(
        ledger.project_id,
        task_id=task.task_id,
        task_title=task.title,
        members_tried=sorted(members_tried),
        last_member=last_member,
        last_route=last_route,
        last_error=last_error,
        store=ledger,
    )


def _reset_unproductive_count(
    c: LoopCounters, action: Any, outcome: TurnOutcome
) -> None:
    """A usable turn breaks the consecutive malformed-turn streak."""
    member_id = str(getattr(action, "member_id", "") or outcome.member_id or "")
    task_id = str(getattr(action, "task_id", "") or "")
    if member_id and task_id:
        c.unproductive_counts.pop((member_id, task_id), None)


def _handle_completion_refused(
    ledger: Any, c: LoopCounters, policy: CodingAutonomyPolicy,
) -> Optional[str]:
    """F128 bounded ladder. The runner refused a PM done=true claim because open
    work remained. Count it; the PM is re-prompted with the open items next turn.
    At ``completion_refused_limit`` the open set is treated as unresolvable —
    raise ONE blocking ``completion_blocked`` Problem and stop the run truthfully
    (never a silent ``no_progress``, never a false ``done``). Never raises into
    the loop."""
    c.false_done_streak += 1
    if c.false_done_streak < max(1, policy.completion_refused_limit):
        return None
    try:
        from . import attention
        from .completion import pending_completion_work
        open_items = pending_completion_work(ledger)
        attention.raise_completion_blocked_problem(
            ledger.project_id, open_items=open_items, store=ledger)
    except Exception:  # noqa: BLE001 — producer must never break the run loop
        pass
    return COMPLETION_BLOCKED


def _completion_streak_reset_by(outcome: TurnOutcome) -> bool:
    """Whether this turn made enough progress to break a false-done streak.

    ``TurnOutcome.made_progress`` only has defined meaning for planning and
    governance turns; its historical default is ``True``, including for some
    noops. Keep the reset vocabulary explicit so parse failures, gateway errors,
    and other nonproductive turns cannot indefinitely postpone escalation.
    """
    if outcome.kind in {"planned", "governance_progress"}:
        return bool(outcome.made_progress)
    return outcome.kind in {
        "project_done",
        "pr_opened",
        "pr_reviewed",
        "pr_tested",
        "pr_conflict",
        "pr_skipped",
        "pr_merged",
        "task_blocked",
        "review_done",
        "task_done",
    }


def _account_member_outcome(
    c: LoopCounters, policy: CodingAutonomyPolicy, outcome: TurnOutcome,
) -> Optional[tuple[str, str, str, Any, int]]:
    """F120 per-member consecutive-failure accounting.

    On a turn carrying a ``member_failure``: increment that member's count; when
    it reaches ``classify_aware_cap`` return the raise payload
    ``(member_id, route, role, failure, attempts)`` so the caller raises the
    Problem + stops. On any OTHER (successful / non-call-failure) turn for a known
    member: reset that member's count to 0 (transient resilience, criterion #8).
    Returns ``None`` when no member-health stop is due."""
    from .member_health import classify_aware_cap

    member_id = getattr(outcome, "member_id", "") or ""
    failure = getattr(outcome, "member_failure", None)
    if failure is None:
        # A turn that produced output for this member clears its failure streak.
        if member_id and c.member_fail_counts.get(member_id):
            c.member_fail_counts[member_id] = 0
        return None
    if not member_id:
        member_id = "unknown"
    count = c.member_fail_counts.get(member_id, 0) + 1
    c.member_fail_counts[member_id] = count
    cap = classify_aware_cap(failure.status, policy)
    if count >= cap:
        route = getattr(outcome, "member_route", "") or ""
        role = getattr(outcome, "member_role", "") or ""
        return (member_id, route, role, failure, count)
    return None


def _run_sequential_loop(
    ledger: Any,
    members: list[tuple[str, str]],
    policy: CodingAutonomyPolicy,
    *,
    run_turn: RunTurn,
    rec: CodingReconciler,
    should_cancel: Optional[Callable[[], bool]],
    c: LoopCounters,
    policy_provider: Optional[Callable[[], CodingAutonomyPolicy]],
    member_tiers: Optional[dict[str, int]] = None,
    delivery_review: Optional[Callable[[Any], Any]] = None,
) -> LoopResult:
    """The original one-action-per-iteration loop (max_parallel_workers <= 1)."""
    while True:
        if policy_provider is not None:
            policy = policy_provider()
        # F139 WS-A/WS-D: this loop is entered when concurrency is clamped to 1
        # (e.g. the foundation is pending). The upgrade must be self-healing — when
        # the clamp lifts (foundation merges, ramp opens up) hand back UP to the
        # concurrent loop, mirroring its downgrade-to-sequential hand-off. Without
        # this, a `checkpoint_cadence=off` run stays single-worker forever after the
        # foundation lands. `runtime_cap` is monotonic here (foundation flips once,
        # merges only increase), so there is no sequential<->concurrent ping-pong.
        if runtime_cap(policy, members, ledger) > 1:
            return _run_concurrent_loop(
                ledger, members, policy, run_turn=run_turn, rec=rec,
                should_cancel=should_cancel, c=c, policy_provider=policy_provider,
                member_tiers=member_tiers, delivery_review=delivery_review)
        if should_cancel is not None and should_cancel():
            return LoopResult(CANCELLED, c)

        action = decide_next(ledger, members, member_tiers)
        if isinstance(action, Complete):
            return LoopResult(action.reason, c)  # definition_of_done / no_actionable_work

        # Budget caps (always a stop).
        if c.iterations >= policy.max_iterations:
            return LoopResult(BUDGET_EXHAUSTED, c)
        if policy.max_model_calls is not None and c.model_calls >= policy.max_model_calls:
            return LoopResult(BUDGET_EXHAUSTED, c)

        # Execute the turn.
        if isinstance(action, Assign):
            rec.assign(action)
        outcome = run_turn(action, ledger)
        c.iterations += 1
        # F087-17: a PM merge turn is mechanical (no model call) -> model_calls 0.
        c.model_calls += max(0, int(outcome.model_calls))
        c.turns_repaired += max(0, int(outcome.repairs))
        if isinstance(action, PMAssist):
            c.pm_assists += 1

        milestone = _apply_outcome(rec, ledger, action, outcome, c, delivery_review)

        # F155: the delivery review kept rejecting the integrated head. A filed
        # finding counts as progress (resets pm_idle) and changes the head, so
        # no_progress / not_converging never trip — cap the rejected rounds here so
        # a persistently-failing delivery ends truthfully, not at budget_exhausted.
        if c.delivery_review_rounds >= policy.delivery_review_round_limit:
            return LoopResult(DELIVERY_REVIEW_STALLED, c)

        # F128: the runner refused a PM done=true claim (open work remained). The
        # PM is re-prompted next turn; if it keeps falsely claiming done, escalate
        # to a blocking completion_blocked Problem instead of a silent no_progress.
        if outcome.kind == "completion_refused":
            cb_stop = _handle_completion_refused(ledger, c, policy)
            if cb_stop is not None:
                return LoopResult(cb_stop, c)
            continue
        if _completion_streak_reset_by(outcome):
            c.false_done_streak = 0

        # F120: a member that can't run trips a blocking member-health Problem
        # and stops the run within classify_aware_cap attempts (NOT hundreds).
        mh_stop = _account_member_outcome(c, policy, outcome)
        if mh_stop is not None:
            member_id, route, role, failure, attempts = mh_stop
            _maybe_raise_member_health(
                ledger, member_id, role, route, failure, attempts)
            return LoopResult(
                MEMBER_UNHEALTHY, c,
                detail={"member_id": member_id, "reason": failure.status,
                        "attempts": attempts})

        # F127: a worker that keeps producing unusable turns gets its task
        # reassigned to a different/stronger member; if every member has failed it,
        # a blocking Problem is raised and the run stops (never a silent no_progress).
        if outcome.unproductive:
            up_stop = _handle_unproductive(ledger, action, outcome, c, policy, members)
            if up_stop is not None:
                return LoopResult(up_stop, c, detail={"task_id": getattr(action, "task_id", "")})
        else:
            _reset_unproductive_count(c, action, outcome)

        if outcome.kind == "pm_assist_exhausted":
            return LoopResult(
                WORKER_UNPRODUCTIVE,
                c,
                detail={"task_id": getattr(action, "task_id", "")},
            )

        if outcome.hard_blocker:
            _maybe_raise_monitor(ledger, "hard_blocker", outcome.reason)
            return LoopResult(HARD_BLOCKER, c, detail={"reason": outcome.reason})

        # PM made no progress N times in a row -> nothing left to do.
        if c.pm_idle >= policy.pm_idle_limit:
            _maybe_raise_monitor(ledger, "no_progress", "PM made no progress")
            return LoopResult(NO_PROGRESS, c)

        # F139 WS-A/WS-E: surface a stuck foundation, and stop a run where nothing
        # is moving anywhere (distinct from NO_PROGRESS, which is a PM-idle stop).
        _account_foundation_stall(ledger, c, policy)
        _account_hot_file_freeze(ledger, c, policy)
        conv_stop = _account_convergence(ledger, c, policy)
        if conv_stop is not None:
            return conv_stop
        # Spec 04: stop a run whose acceptance gate result keeps repeating without
        # improving (the 6/12-with-a-churning-head loop). Keyed on the gate result,
        # so it catches churn that `not_converging` (progress fingerprint) misses.
        gate_stop = _account_gate_stall(ledger, c, policy)
        if gate_stop is not None:
            return gate_stop

        # Checkpoint AFTER making progress on a unit; resume continues cleanly.
        if _checkpoint_due(policy, c, milestone):
            c.since_checkpoint = 0
            return LoopResult(CHECKPOINT, c)


_TURN_ERROR_PREFIX = "turn_error: "


def _safe_run_turn(run_turn: RunTurn, action: Any, ledger: Any,
                   model_cost: int) -> TurnOutcome:
    """Run a member turn with failure isolation: a crashing turn becomes a noop
    so one bad worker can't tear down the whole concurrent batch. The model call
    is counted as consumed (``model_cost``) so a crash can never let the budget
    overshoot on the next reservation. The crash is tagged (``_TURN_ERROR_PREFIX``)
    so the apply step can requeue the task instead of stranding it ``doing``."""
    try:
        return run_turn(action, ledger)
    except Exception as exc:  # noqa: BLE001
        return TurnOutcome(kind="noop", made_progress=False,
                           reason=f"{_TURN_ERROR_PREFIX}{exc}", model_calls=model_cost)


def _crashed(outcome: TurnOutcome) -> bool:
    return outcome.kind == "noop" and outcome.reason.startswith(_TURN_ERROR_PREFIX)


def _requeue_crashed(ledger: Any, action: Any, outcome: TurnOutcome) -> None:
    """Put a crashed worker's task back on the queue (``todo``, unassigned) and
    record why, so a transient member failure self-heals on a later batch."""
    try:
        ledger.update_task(action.task_id, state="todo", assignee_member_id=None)
        ledger.record_decision(
            title="worker turn crashed", context=f"task {action.task_id}",
            choice="worker_turn_requeued", rationale=outcome.reason,
            related_task_ids=[action.task_id])
    except Exception:  # noqa: BLE001 — never let cleanup crash the loop
        pass


def _requeue_stranded(ledger: Any, action: Any, outcome: TurnOutcome) -> bool:
    """Spec 09 §3 — stale-``doing`` reaper.

    ``CodingReconciler.assign`` marks a task ``doing`` BEFORE the turn runs. When
    the turn comes back ``noop`` (or any kind the reconciler does not recognise)
    nothing moves the task again, so it is stranded ``doing`` forever: invisible
    to ``next_task`` (which only dispatches ``todo``) yet not ``done``, so every
    dependent blocks on it permanently. Put it back on the queue instead.

    Liveness, not state: this only ever runs for the action whose turn JUST
    finished — the sequential loop calls it after ``run_turn`` returned, the
    concurrent loop after ``fut.result()`` with the action already popped out of
    ``in_flight`` — so it can never touch a task with a live future. The
    assignee check is the second belt: if the ledger says somebody else now owns
    the row, leave it alone."""
    if not isinstance(action, Assign):
        return False
    task_id = str(getattr(action, "task_id", "") or "")
    if not task_id:
        return False
    try:
        task = next(
            (t for t in ledger.list_tasks() if t.task_id == task_id), None)
        if task is None or task.state != "doing":
            return False  # the turn already moved it (done/blocked/dropped/todo)
        member_id = str(getattr(action, "member_id", "") or "")
        assignee = str(getattr(task, "assignee_member_id", "") or "")
        if assignee and member_id and assignee != member_id:
            return False  # reassigned out from under us — not ours to reap
        ledger.update_task(task_id, state="todo", assignee_member_id=None)
        if not outcome.unproductive:
            # F127's ladder records its own decision for unproductive turns —
            # don't double-log every one of them.
            ledger.record_decision(
                title="stranded task requeued", context=f"task {task_id}",
                choice="stale_doing_requeued",
                rationale=(
                    f"turn returned '{outcome.kind}' "
                    f"({outcome.reason or 'no change'}); returning the task to "
                    "todo so it cannot block its dependents forever"),
                related_task_ids=[task_id])
        return True
    except Exception:  # noqa: BLE001 — never let cleanup crash the loop
        return False


def _idle_members(members: list[tuple[str, str]],
                  busy: set[str]) -> list[tuple[str, str]]:
    return [m for m in members if m[0] not in busy]


def _run_concurrent_loop(
    ledger: Any,
    members: list[tuple[str, str]],
    policy: CodingAutonomyPolicy,
    *,
    run_turn: RunTurn,
    rec: CodingReconciler,
    should_cancel: Optional[Callable[[], bool]],
    c: LoopCounters,
    policy_provider: Optional[Callable[[], CodingAutonomyPolicy]],
    member_tiers: Optional[dict[str, int]] = None,
    delivery_review: Optional[Callable[[Any], Any]] = None,
) -> LoopResult:
    """F087-3 continuous pipeline. Keeps every idle worker member saturated: each
    iteration we re-plan for the members NOT currently in flight and dispatch
    their ready turns, then wait only for the NEXT turn to finish (not the whole
    batch) before re-planning. So a dev that finishes immediately picks up the
    next task WHILE the reviewer reviews the one it just opened — 2 devs + 1
    reviewer run as 3 in flight, not dev→reviewer→dev. Outcomes are applied
    serially in this thread so the ledger + counters stay race-free; a Merge is
    mechanical (0 model calls). A drain-stop (blocker/cancel/budget/checkpoint/
    no-progress) stops dispatching new work and returns once in-flight drains."""
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    cap = runtime_cap(policy, members, ledger)
    # The pool is sized to the STATIC ceiling; runtime_cap may clamp `cap` lower
    # (foundation gate / ramp) but never above the static parallelism, so the pool
    # is always large enough. (+2 headroom for a deferred Merge + a PM turn.)
    pool = ThreadPoolExecutor(max_workers=effective_parallelism(policy, members) + 2)
    in_flight: dict[Any, Any] = {}   # future -> action
    busy: set[str] = set()           # member_ids currently running a turn
    model_in_flight = 0              # non-merge turns in flight (cap + budget)
    pending_stop: Optional[LoopResult] = None
    milestone = False

    def _over_budget() -> bool:
        if c.iterations + len(in_flight) >= policy.max_iterations:
            return True
        return (policy.max_model_calls is not None
                and c.model_calls + model_in_flight >= policy.max_model_calls)

    try:
        while True:
            if policy_provider is not None:
                policy = policy_provider()
            # F139 WS-A/WS-D: recompute every iteration (not only on a policy edit)
            # so the foundation gate / ramp takes effect as state changes — e.g. a
            # mid-run clamp to 1 when the foundation is still pending routes back to
            # the sequential loop below.
            cap = runtime_cap(policy, members, ledger)
            # Downgraded to a single worker with nothing running -> hand off.
            if cap <= 1 and not in_flight:
                return _run_sequential_loop(
                    ledger, members, policy, run_turn=run_turn, rec=rec,
                    should_cancel=should_cancel, c=c, policy_provider=policy_provider,
                    member_tiers=member_tiers, delivery_review=delivery_review)

            if pending_stop is None and should_cancel is not None and should_cancel():
                pending_stop = LoopResult(CANCELLED, c)
            if pending_stop is None and _over_budget():
                pending_stop = LoopResult(BUDGET_EXHAUSTED, c)

            # --- dispatch phase: fill idle worker slots ---------------------
            dispatched_now = 0
            if pending_stop is None:
                # F159: compute the hot-file picture ONCE per iteration (list_prs is
                # a file read). `_hot` empty (the no-contention common case) makes
                # every gate below a no-op → dispatch is identical to pre-F159.
                _hot = hot_files(ledger, threshold=policy.hot_file_threshold)
                _hot_paths = set(_hot)
                _hot_blocked = hot_owned_paths(ledger, _hot)
                _frozen = frozen_paths(ledger)
                _frozen_owner = None
                if _frozen:
                    try:
                        _frozen_owner = str(
                            ledger.get_run_state().get("contract_owner_task_id", "") or "") or None
                    except Exception:  # noqa: BLE001
                        _frozen_owner = None
                while model_in_flight < cap:
                    batch = plan_next_batch(
                        ledger, _idle_members(members, busy), member_tiers,
                        hot_paths=_hot_paths, hot_blocked=_hot_blocked,
                        frozen=_frozen, frozen_owner_task_id=_frozen_owner)
                    if not batch:
                        break
                    if len(batch) == 1 and isinstance(batch[0], Complete):
                        if not in_flight and dispatched_now == 0:
                            return LoopResult(batch[0].reason, c)
                        break  # drain in-flight, then re-evaluate
                    action = next(
                        (a for a in batch
                         if not isinstance(a, Complete)
                         and getattr(a, "member_id", None) not in busy),
                        None)
                    if action is None:
                        break
                    is_mechanical = isinstance(action, (Merge, GovernanceMaterialize))
                    is_merge = isinstance(action, Merge)
                    flight = in_flight.values()
                    if is_merge:
                        # Integration is serial: a Merge mutates master and
                        # revalidates other PRs' worktrees, so it must not run
                        # while worker turns write in parallel. Defer it until the
                        # in-flight workers drain (we stop adding new work below,
                        # so they will), then run the merge alone.
                        if any(isinstance(a, Assign) for a in flight):
                            break
                    else:
                        # Don't start new work while a Merge is integrating (it's
                        # changing the base those turns would build on), and don't
                        # (re-)plan while workers are still running — an instant
                        # no-progress Plan would busy-loop the idle PM (burning
                        # iterations + model calls) while workers are slow.
                        if any(isinstance(a, Merge) for a in flight):
                            break
                        if (isinstance(action, (Plan, PMAssist))
                                and any(isinstance(a, Assign) for a in flight)):
                            break
                    if not is_mechanical and _over_budget():
                        break  # leave model budget for in-flight; stop adding
                    if isinstance(action, Assign):
                        rec.assign(action)  # ledger-locked: never double-assign
                        # F159: the just-assigned task now holds its hot paths for
                        # the rest of this tick (it's `doing`); recompute so the
                        # next plan_next_batch call won't hand a colliding task out.
                        if _hot:
                            _hot_blocked = hot_owned_paths(ledger, _hot)
                    fut = pool.submit(
                        _safe_run_turn, run_turn, action, ledger,
                        0 if is_mechanical else 1)
                    in_flight[fut] = action
                    busy.add(getattr(action, "member_id", f"m-{id(action)}"))
                    if not is_mechanical:
                        model_in_flight += 1
                    dispatched_now += 1

            # --- nothing running and nothing dispatched -> terminal ---------
            if not in_flight:
                if pending_stop is not None:
                    return pending_stop
                action = decide_next(ledger, members, member_tiers)
                if isinstance(action, Complete):
                    return LoopResult(action.reason, c)
                if _over_budget():
                    return LoopResult(BUDGET_EXHAUSTED, c)
                return LoopResult(NO_ACTIONABLE_WORK, c)

            # --- wait for the next turn to finish, apply its outcome --------
            done, _pending = wait(set(in_flight), return_when=FIRST_COMPLETED)
            for fut in done:
                action = in_flight.pop(fut)
                busy.discard(getattr(action, "member_id", None))
                outcome = fut.result()  # _safe_run_turn never raises
                if not isinstance(action, (Merge, GovernanceMaterialize)):
                    model_in_flight -= 1
                c.iterations += 1
                c.model_calls += max(0, int(outcome.model_calls))
                c.turns_repaired += max(0, int(outcome.repairs))
                if isinstance(action, PMAssist):
                    c.pm_assists += 1
                if _crashed(outcome) and isinstance(action, Assign):
                    _requeue_crashed(ledger, action, outcome)
                    continue
                milestone = _apply_outcome(
                    rec, ledger, action, outcome, c, delivery_review) or milestone
                # F155: cap delivery-review reject rounds (mirrors the sequential
                # loop). A filed finding resets pm_idle + changes the head, so
                # no_progress / not_converging never trip — stop truthfully instead
                # of looping to budget_exhausted. Drain-stop like the other caps.
                if (c.delivery_review_rounds >= policy.delivery_review_round_limit
                        and pending_stop is None):
                    pending_stop = LoopResult(DELIVERY_REVIEW_STALLED, c)
                # F128: a refused PM done-claim escalates to a blocking
                # completion_blocked Problem if the PM keeps falsely claiming done;
                # otherwise any productive turn resets the streak.
                if outcome.kind == "completion_refused":
                    cb_stop = _handle_completion_refused(ledger, c, policy)
                    if cb_stop is not None and pending_stop is None:
                        pending_stop = LoopResult(cb_stop, c)
                elif _completion_streak_reset_by(outcome):
                    c.false_done_streak = 0
                # F120: count per-member call failures; at the classify-aware cap
                # raise a blocking member-health Problem and drain-stop the run.
                mh_stop = _account_member_outcome(c, policy, outcome)
                if mh_stop is not None and pending_stop is None:
                    member_id, route, role, failure, attempts = mh_stop
                    pending_stop = LoopResult(
                        MEMBER_UNHEALTHY, c,
                        detail={"member_id": member_id, "reason": failure.status,
                                "attempts": attempts, "role": role, "route": route,
                                "_failure": failure})
                # F127: reassign-up an unproductive worker turn; drain-stop with a
                # blocking Problem only if every member of the role has failed it.
                if outcome.unproductive:
                    up_stop = _handle_unproductive(
                        ledger, action, outcome, c, policy, members)
                    if up_stop is not None and pending_stop is None:
                        pending_stop = LoopResult(
                            up_stop, c,
                            detail={"task_id": getattr(action, "task_id", "")})
                else:
                    _reset_unproductive_count(c, action, outcome)
                if outcome.kind == "pm_assist_exhausted" and pending_stop is None:
                    pending_stop = LoopResult(
                        WORKER_UNPRODUCTIVE,
                        c,
                        detail={"task_id": getattr(action, "task_id", "")},
                    )
                if outcome.hard_blocker and pending_stop is None:
                    pending_stop = LoopResult(
                        HARD_BLOCKER, c, detail={"reason": outcome.reason})

            # Drain-stop checks fire at a quiescent point (in-flight empty) so a
            # resume continues cleanly; while work is still running, keep going.
            if not in_flight:
                if pending_stop is not None:
                    if pending_stop.stop_reason == MEMBER_UNHEALTHY:
                        d = pending_stop.detail or {}
                        failure = d.get("_failure")
                        if failure is not None:
                            _maybe_raise_member_health(
                                ledger, d.get("member_id", ""), d.get("role", ""),
                                d.get("route", ""), failure,
                                int(d.get("attempts", 1)))
                        d.pop("_failure", None)  # keep result JSON-serializable
                        return pending_stop
                    if pending_stop.stop_reason == HARD_BLOCKER:
                        _maybe_raise_monitor(
                            ledger, "hard_blocker",
                            (pending_stop.detail or {}).get("reason", ""))
                    return pending_stop
                if c.pm_idle >= policy.pm_idle_limit:
                    _maybe_raise_monitor(ledger, "no_progress", "PM made no progress")
                    return LoopResult(NO_PROGRESS, c)
                # F139 WS-A/WS-E: foundation-stall surfacing + convergence stop,
                # checked at this quiescent (in-flight empty) point so a resume
                # continues cleanly.
                _account_foundation_stall(ledger, c, policy)
                _account_hot_file_freeze(ledger, c, policy)
                conv_stop = _account_convergence(ledger, c, policy)
                if conv_stop is not None:
                    return conv_stop
                # Spec 04: gate-repeat stall stop, at this same quiescent point.
                gate_stop = _account_gate_stall(ledger, c, policy)
                if gate_stop is not None:
                    return gate_stop
                if _checkpoint_due(policy, c, milestone):
                    c.since_checkpoint = 0
                    return LoopResult(CHECKPOINT, c)
                milestone = False
    finally:
        pool.shutdown(wait=True)


def _apply_outcome(rec: CodingReconciler, ledger: Any, action: Any,
                   outcome: TurnOutcome, c: LoopCounters,
                   delivery_review: Optional[Callable[[Any], Any]] = None) -> bool:
    """Apply the reconciler mutation for a turn's outcome. Returns whether this
    turn was a milestone (a fully-validated unit / project completion).

    F146 Slice B: ``delivery_review`` (when provided) verifies the INTEGRATED
    delivered head as a unit before a ``project_done`` is allowed to stick."""
    milestone = False
    if outcome.kind in {"planned", "governance_progress"}:
        if outcome.made_progress:
            c.pm_idle = 0
        else:
            c.pm_idle += 1
        return milestone

    if outcome.kind == "project_done":
        # F146 Slice B: before a `done` sticks, verify the INTEGRATED delivered
        # head as a unit — a real reviewer over the whole delivered diff plus the
        # registered test suite, both bound to workspace.head(). Fail-closed: a
        # reject / test failure / verify error does NOT mark done. When findings
        # were filed as dev tasks, Slice E's `_has_open_work` re-opens the run to
        # work them (progress -> pm_idle reset); when the review simply could not
        # run and queued nothing, count it toward the no-progress stop so a run
        # that can never verify still ends truthfully (never a false `done`).
        if delivery_review is not None:
            result = delivery_review(ledger)
            if not getattr(result, "passed", True):
                if getattr(result, "filed_findings", False):
                    c.pm_idle = 0
                    # F155: count this rejected round. The caller stops the run
                    # `delivery_review_stalled` once the cap is reached, instead of
                    # looping fix->re-review to budget_exhausted.
                    c.delivery_review_rounds += 1
                else:
                    c.pm_idle += 1
                return False
            # F155: a passing delivery review clears the stall count.
            c.delivery_review_rounds = 0
        ledger.set_project_status("done")
        c.pm_idle = 0
        # Not a checkpoint milestone: the next decide_next returns Complete
        # (definition_of_done), which is the proper completion path.
        return False

    # F087-17 branch-per-task PR flow. The runner performs the task/PR ledger
    # mutations inline; here we only update loop counters. A merged PR is the
    # "validated, integrated unit" milestone.
    if outcome.kind in ("pr_opened", "pr_reviewed", "pr_tested", "pr_conflict",
                        "pr_skipped"):
        c.pm_idle = 0
        return False
    if outcome.kind == "pr_merged":
        c.tasks_done += 1
        c.since_checkpoint += 1
        c.pm_idle = 0
        return True

    if outcome.kind == "task_blocked" and outcome.task is not None:
        rec.block_task(outcome.task, reason=outcome.reason or "blocked")
        c.pm_idle = 0
        return milestone

    if outcome.kind == "review_done" and outcome.task is not None:
        rec.complete_review_task(
            outcome.task, approved=outcome.approved,
            reviewed_task_id=outcome.reviewed_task_id or "",
            reviewed_title=outcome.reviewed_title or "",
        )
        c.tasks_done += 1
        c.since_checkpoint += 1
        c.pm_idle = 0
        return milestone

    if outcome.kind == "task_done" and outcome.task is not None:
        role = getattr(action, "role", None)
        if role == DEV:
            rec.complete_dev_task(outcome.task)
        elif role == TESTER:
            ledger.update_task(outcome.task.task_id, state="done")
            milestone = True  # a validated unit of work
        else:
            ledger.update_task(outcome.task.task_id, state="done")
        c.tasks_done += 1
        c.since_checkpoint += 1
        c.pm_idle = 0
        return milestone

    # noop / unknown — no reconciler transition fired, so an assigned task is
    # still sitting in `doing` from `rec.assign`. Spec 09 §3: return it to the
    # queue rather than stranding it (a stranded `doing` task is invisible to
    # `next_task` AND blocks every dependent forever).
    _requeue_stranded(ledger, action, outcome)
    return milestone
