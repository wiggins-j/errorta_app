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
from typing import Any, Callable, Optional, Union

from . import detector_state as _detector_state
from . import paths as _paths
from . import task_dedupe
from .ledger import Task
from .topology import (
    _WORKER_PRIORITY,
    DEV,
    PM,
    TESTER,
    Assign,
    CodingReconciler,
    Complete,
    GateRun,
    GovernanceMaterialize,
    LastWord,
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
PLANNING_CHURN = "planning_churn"              # Spec 07: PM-only plan turns, no worker
DISPATCH_WEDGED = "dispatch_wedged"            # Spec 10: large todo backlog, nothing dispatchable
REVISE_LIVELOCK = "revise_livelock"            # Spec 16: broken revise lineage, no recovery

# --- SPEC-23 (Item 1): the stop-reason taxonomy, HARD vs HEURISTIC ----------- #
#
# An autonomous run should end for exactly two reasons: the work is DONE, or a
# human/budget said STOP. Every other condition is a detector's OPINION that the
# run is stuck — a signal to change strategy, not to terminate. The split below is
# the spec's table, in code, and `test_spec23_continue_by_default.py` locks it as an
# exact partition of the constants above, so a new stop reason added without a
# class fails CI instead of silently defaulting to "terminate".
#
# NO string values change here and no reason is added or removed, so the CLI's
# fail-closed allowlist (`runstream.FAILURE_STOP_REASONS` / `SUCCESS_STOP_REASONS`)
# and its own partition lock need zero edits (batch regression lock 1).

# Terminate immediately, exactly as today. An intervention here would either cost
# a model call the run does not have (budget), argue with a human (cancel,
# checkpoint), or ask the PM to propose a strategy for something that is not a
# strategy problem (a member declared a hard blocker; a provider cannot
# authenticate).
HARD_STOP_REASONS = frozenset({
    BUDGET_EXHAUSTED,   # the budget said stop — an intervention costs what it lacks
    CANCELLED,          # a human said stop
    CHECKPOINT,         # the operator's own cadence knob, resumable via `continue`
    HARD_BLOCKER,       # a MEMBER declared it — the team is asking for a human
    MEMBER_UNHEALTHY,   # after the F120 classify-aware cap — not a strategy problem
})

# A detector's opinion, computed between turns from the ledger alone. These are
# what SPEC-23 hands to the PM before they land.
HEURISTIC_STOP_REASONS = frozenset({
    NO_PROGRESS,               # pm_idle_limit consecutive non-progressing PM turns
    NOT_CONVERGING,            # progress fingerprint unchanged for N iterations
    GATE_NOT_IMPROVING,        # gate score not strictly improving for N iterations
    PLANNING_CHURN,            # N PM plan turns with no worker turn
    DISPATCH_WEDGED,           # a big todo backlog with nothing dispatchable
    REVISE_LIVELOCK,           # broken lineage + no merge for N iterations
    DELIVERY_REVIEW_STALLED,   # N delivery rejections
    WORKER_UNPRODUCTIVE,       # but only the LADDER-EXHAUSTED return (see below)
    COMPLETION_BLOCKED,        # HEURISTIC, but ALREADY intervened — see below
})

# The DONE outcome, plus the one heuristic reason this spec deliberately defers.
# `no_actionable_work` comes from `decide_next` returning `Complete`, not from a
# detector window, so there is no window to reset — and it is CLI SUCCESS-class,
# so intervening there risks flipping an exit code. SPEC-27 owns it.
TERMINAL_STOP_REASONS = frozenset({DEFINITION_OF_DONE, NO_ACTIONABLE_WORK})

# Where `_intervene` actually fires: HEURISTIC minus `completion_blocked`.
# F128's `_handle_completion_refused` ALREADY is a last-word loop — the PM is
# re-prompted with the open item set between claims, `completion_refused_limit`
# times, and its prompt shows it exactly what is open. A second intervention would
# ask the same party the same question, so the class is recorded (above) and the
# hook is not wired (SPEC-23 Item 4). Written as a derived set so a future reader
# sees the decision rather than assuming an oversight.
_INTERVENABLE_STOP_REASONS = HEURISTIC_STOP_REASONS - {COMPLETION_BLOCKED}

# `_handle_unproductive` returns WORKER_UNPRODUCTIVE from two places with two
# different meanings: the ladder was exhausted (a strategy problem — exactly what
# the PM should re-route), and its own `except` arm (the escalation code THREW —
# an engine fault). Asking a PM to propose a strategy for an engine bug is noise,
# so the except arm returns this sentinel instead; the call sites map it to the
# SAME `worker_unproductive` stop reason (no new reason) carrying
# `detail={"engine_fault": True}`, which `_intervene` treats as HARD.
ENGINE_FAULT_UNPRODUCTIVE = "worker_unproductive:engine_fault"

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
    # Spec 07: consecutive PM PLAN turns with ZERO interleaved worker turns before
    # the run stops `planning_churn`. This is the PM-only pathology both other
    # convergence detectors are structurally blind to: `not_converging` treats a
    # newly-created task as motion BY DESIGN, and `gate_not_improving` needs a gate
    # signal that only worker turns produce. Any worker turn (task_done /
    # review_done / task_blocked / a PR transition) resets the streak, so a
    # legitimate up-front decomposition burst is unaffected. 0 disables it.
    plan_streak_limit: int = 6
    # Spec 10: wedged-graph probe. When at least `wedge_min_tasks` todo tasks exist
    # but NO worker role has a dispatchable (deps-satisfied) head — sustained for
    # `wedge_stall_limit` iterations — the run stops `dispatch_wedged` after naming
    # the non-satisfiable dependency ids wedging the backlog. This is the pathology
    # every other detector is blind to: a large backlog that presents as a
    # legitimate PM plan turn because nothing is dispatchable. `wedge_min_tasks`
    # guards a legitimately small/empty backlog from tripping; `wedge_stall_limit`
    # requires the wedge to persist so a normal in-flight `doing` task doesn't
    # false-fire. `wedge_stall_limit == 0` disables the detector.
    wedge_min_tasks: int = 10
    wedge_stall_limit: int = 5
    # Spec 11 (P1a): let a DEV turn READ its task worktree in-turn (read-only:
    # Read/Grep/Glob only — no write, no exec, no network), so it can grep the
    # rest of the repo and see BOTH sides of a cross-file contract instead of
    # reasoning from a pre-baked half-context. The model's actual edits still
    # flow through the coding_turn.v1 envelope + execute_dev_turn, never a Write
    # tool. Only the claude_cli vendor honors this today (the all-Opus team's
    # critical vendor); codex/cursor are a documented follow-up.
    #
    # Spec 12-18 prep (P0.3) — DEFAULT OFF, stated in exactly one place: here.
    # Three other sites (this field's own comment, policy_from_dict's comment,
    # and build_run_turn's docstring) each asserted the opposite default; the
    # field has been False since Spec 11 P1a introduced it and was never flipped,
    # so the prose was wrong, not the value. A project opts in by persisting
    # `dev_repo_read: true` in autonomy.json. Turning it on by default is a live
    # behaviour change for every dev turn and belongs in its own PR.
    # `test_spec12_18_prep.py` locks the field and the prose together.
    dev_repo_read: bool = False
    # Spec 14: the same in-turn read-only retrieval for REVIEWER (and strict-mode
    # PM PR-review) turns, so a verdict can be grounded in the tree it judges
    # instead of a diff excerpt. Defaults to `dev_repo_read` deliberately — the
    # two are one capability decision, and letting them drift lands it half-on.
    reviewer_repo_read: bool = False
    # Spec 14: wall-time floor (ms) under which an EMPTY reviewer approval is
    # treated as unparsed and retried once. The primary ungrounded-verdict signal
    # is the provider's retrieval turn count; this is the fallback for vendors
    # that do not report one. 0 (the default) disables it: a blanket floor would
    # retry most approvals — fake providers, cached CLI responses, small diffs —
    # doubling review cost and adding a retry loop to the very path this batch
    # exists to de-loop.
    review_min_latency_ms: int = 0
    # Spec 14 (P2): attach a headless screenshot of the running merged head to
    # review prompts for a visual Definition of Done. Off by default — it is the
    # one part of the batch that introduces a browser dependency.
    review_screenshot: bool = False
    # Spec 12: auto-acquire a gate for a greenfield project (detect + persist
    # runtime profiles; register an acceptance-scoped test command that is proven
    # to execute on master). Without this the test-command registry is only ever
    # written by the app UI, so an autonomous headless run never has a gate to
    # run and every gate is vacuously satisfied.
    gate_bootstrap: bool = True
    # Spec 12: minimum gate-relevant merges between in-loop gate runs. The gate
    # runs on the MERGED tree at a quiescent point, never inside the merge turn
    # (integration is already serial, so running a suite there would stall the
    # whole team and cancel Spec 13's fan-out). >1 coalesces a burst of merges.
    gate_min_merge_interval: int = 3
    # Spec 16: consecutive same-class rejections on ONE revise lineage before the
    # breaker stops spawning `revise:` tasks and escalates to a PM re-plan. A
    # DIFFERENT finding class resets the streak, so a lineage working through
    # successive distinct defects is never broken. 0 disables the breaker.
    revise_chain_limit: int = 3
    # Spec 16: iterations a broken lineage may sit without producing a merge
    # (i.e. the PM's re-plan did not unstick it either) before the run stops
    # `revise_livelock`. 0 disables the detector.
    revise_livelock_limit: int = 5
    # GL01 (Item 1): the unconditional default web probe — the black-canvas
    # oracle. ON by default (unlike Spec 14's default-OFF screenshot) because a
    # liveness assertion is cheap and is the whole point: it runs REGARDLESS of
    # the command registry, so a buildless web project that authored no test still
    # gets a did-it-render signal. Fail-open — a headless-browser inability records
    # no evidence, never a red gate. The on/off switch for the whole probe arm.
    web_probe: bool = True
    # GL01 (Item 1): rendered frames the probe waits before asserting non-black —
    # a rendered-frame count (requestAnimationFrame), NOT a wall clock, so a
    # slow-but-live canvas clears once it paints. 0 disables the frame wait (assert
    # on the first paint).
    web_probe_frames: int = 30
    # GL04 (GAP-4): the diff-level breaker — stasis + oscillation on a revise
    # lineage, a THIRD trip condition on Spec 16's ONE breaker, checked BEFORE the
    # depth+class cap. On/off switch (default ON); the two thresholds below tune it.
    diff_deadlock: bool = True
    # GL04 (GAP-4): stasis threshold — a new revise diff whose signed-hunk multiset
    # is within this Jaccard distance of the IMMEDIATELY PRECEDING lineage member's
    # is non-progressive (a resubmitted-essentially-the-same change). 0.0 => only a
    # byte-identical diff trips; larger => looser near-identity.
    diff_stasis_epsilon: float = 0.12
    # GL04 (GAP-4): revert overlap — a new diff whose signed-hunk multiset reproduces
    # >= this fraction of the SIGN-FLIP of ANY ancestor's multiset is an oscillation
    # (A->B->A), even with a distinct finding class per hop. High by construction so
    # a mostly-new diff grazing an old hunk does NOT trip (the real-progress lock).
    revert_overlap: float = 0.7
    # GL04 (GAP-5): run-level convergence clamp. Window of most-recent RESOLVED PRs
    # (merged/superseded/blocked/abandoned) the churn metric reads. 0 disables the
    # detector (the `max(0, …)` convention), restoring today's fan-out.
    convergence_window: int = 20
    # GL04 (GAP-5): TRIP band — clamp fan-out to serial when the windowed
    # superseded-ratio >= this OR the merge-rate <= `convergence_clamp_merge_rate`.
    # The run's 53/96-superseded, 30%-merge is the calibration point.
    convergence_clamp_ratio: float = 0.5
    convergence_clamp_merge_rate: float = 0.35
    # GL04 (GAP-5): RELEASE band — un-clamp only once the window recovers past a
    # SEPARATE, tighter band (superseded-ratio <= this AND merge-rate >=
    # `convergence_release_merge_rate`). Distinct trip/release bands are the
    # hysteresis that keeps the clamp from flapping on the boundary.
    convergence_release_ratio: float = 0.35
    convergence_release_merge_rate: float = 0.5
    # GL05 (Item 2): the strict, a-priori file-ownership partition. F159's hot-file
    # gate is REACTIVE — it serializes a path only after it has conflicted
    # `hot_file_threshold` times. This holds a file from the FIRST tick a task owns
    # it, so a fan-out can never hand two in-flight tasks the same DECLARED file
    # (RQ6's "strictly partitioned file ownership" [13]). Fail-open on silence: a
    # task that declares no paths is treated as UNKNOWN ownership, not universal, so
    # it never collapses fan-out to serial (mirrors SPEC-13/F159). On by default — it
    # is inert for the common prose-silent task and only bites declared colliders.
    # At `cap == 1` (sequential loop) it is a no-op. On/off switch for the guardrail.
    strict_file_partition: bool = True
    # --- Spec 22-28 batch (prep PR P0.1) — landed with NO CONSUMERS ----------- #
    # Every knob below is inert in this commit: nothing reads it. They ship early
    # so the five feature branches never race `CodingAutonomyPolicy` /
    # `policy_to_dict` / `policy_from_dict` (the Spec 12-18 batch proved the seam).
    # Each one's DISABLE VALUE (0 / 0.0 / {}) must reproduce today's trace exactly
    # — that is the batch's escape hatch, and each spec must keep it true.
    #
    # SPEC-23: how many times ONE run may hand a heuristic stop back to the PM for
    # a "last word" turn before it stops for real. Each intervention costs ~1 model
    # call + 1 iteration, so the worst case is bounded by this number. 0 disables
    # the whole mechanism — every heuristic stop terminates exactly as it does now.
    last_word_limit: int = 2
    # SPEC-24: how close to a detector's trip threshold a reading must be before it
    # is rendered into the PM prompt as observed state (a fraction of the threshold,
    # so 0.6 = "show it once we're 60% of the way there"). 0.0 disables the whole
    # `governance_state` segment, restoring today's prompt bytes.
    governance_proximity: float = 0.6
    # SPEC-27: run-wide budget for NARROWING rungs (clamp/serialize/defer responses
    # to a tripped detector, one rung softer than a stop). Bounds the worst case the
    # ladder can add to a run. 0 disables the ladder entirely — every detector keeps
    # returning its present stop.
    narrow_limit: int = 3
    # SPEC-27 (Item 4, bounds 2 + 2'): the drain cap. TWO uses, deliberately the
    # same number so the worst case is one product and not a sum:
    #   * how many iterations a NARROWING FLAG (`integration_only`,
    #     `planning_clamped`) may stay engaged before it FORCE-LIFTS with a
    #     decision + monitor signal — the `_account_hot_file_freeze` pattern, so a
    #     narrowing whose release condition never arrives can never become the
    #     wedge it was diagnosing;
    #   * the per-narrow multiplier on the run-wide deferral cap
    #     (`narrow_limit * narrow_drain_iters` — 15 by default), which is the HARD
    #     ceiling on extra iterations this whole spec can add to a run.
    # 0 disables the drain cap the same way `narrow_limit == 0` disables the
    # ladder: no rung may defer a stop at all.
    narrow_drain_iters: int = 5
    # SPEC-25: how many BLOCKED turns one worker may emit on one task before the
    # loop stops treating "blocked" as a legal, progress-bearing answer and routes
    # the task to the recovery ladder. 0 disables the accounting.
    blocked_turn_limit: int = 3
    # SPEC-25 (Item 3a): how many consecutive PM turns rejected for SHAPE are
    # absorbed before they resume counting as idleness. A rejected turn is not an
    # idle turn — the PM tried to say something and the schema refused it — so
    # charging `pm_idle` for it made "trying to comply" accelerate termination
    # (four rejections, `pm_idle_limit=2`, a healthy run stopped `no_progress`
    # with two PRs open). Past this many the run must still be able to end, so the
    # rejections start counting again and the existing `no_progress` stop lands:
    # a schema the PM cannot satisfy is a bug in the SCHEMA, and the recorded
    # `pm turn rejected` decisions carry the validator dump to file it with.
    # 0 disables the split — every shape rejection counts as idle, exactly as it
    # does today.
    schema_reject_limit: int = 3
    # SPEC-26: operator overrides for the per-role capability manifest, as
    # ``{role: {capability: bool}}``. EMPTY (the default) means "derive every
    # capability exactly as today" — the disable value for this one is `{}`, not 0.
    # NOTE the dataclass mutable-default rule: a bare `{}` default is a TypeError on
    # a dataclass field, so this uses `field(default_factory=dict)`. The dataclass is
    # frozen but the dict is not — treat it as read-only; `policy_from_dict` always
    # builds a fresh copy so a persisted policy can never alias a caller's dict.
    capability_overrides: dict[str, Any] = field(default_factory=dict)
    # F156 (G5): how many PRs in ONE run may merge on a tester `not_applicable`
    # declaration before the run surfaces an operator-visible escalation instead of a
    # deduped non-blocking alert. Deliberately NOT a hard cap: a partial slice
    # legitimately has no test that exercises it, and refusing the declaration would
    # wedge the run. What this bounds is INVISIBILITY — a run leaning on the escape
    # for slice after slice is merging on review alone, and the operator should be
    # told rather than have it pass silently forever. The FINAL head is still gated
    # deterministically by delivery_review's full-registry run and F154's default
    # build, so this is about visibility, not the last line of defence. 0 disables the
    # escalation entirely, restoring today's always-non-blocking alert.
    not_applicable_soft_limit: int = 3


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
        "plan_streak_limit": p.plan_streak_limit,
        "wedge_min_tasks": p.wedge_min_tasks,
        "wedge_stall_limit": p.wedge_stall_limit,
        "dev_repo_read": p.dev_repo_read,
        # Spec 12-18 batch (landed by the prep PR with no consumers, so neither
        # feature branch has to edit this function).
        "reviewer_repo_read": p.reviewer_repo_read,
        "review_min_latency_ms": p.review_min_latency_ms,
        "review_screenshot": p.review_screenshot,
        "gate_bootstrap": p.gate_bootstrap,
        "gate_min_merge_interval": p.gate_min_merge_interval,
        "revise_chain_limit": p.revise_chain_limit,
        "revise_livelock_limit": p.revise_livelock_limit,
        "web_probe": p.web_probe,
        "web_probe_frames": p.web_probe_frames,
        "diff_deadlock": p.diff_deadlock,
        "diff_stasis_epsilon": p.diff_stasis_epsilon,
        "revert_overlap": p.revert_overlap,
        "convergence_window": p.convergence_window,
        "convergence_clamp_ratio": p.convergence_clamp_ratio,
        "convergence_clamp_merge_rate": p.convergence_clamp_merge_rate,
        "convergence_release_ratio": p.convergence_release_ratio,
        "convergence_release_merge_rate": p.convergence_release_merge_rate,
        "strict_file_partition": p.strict_file_partition,
        # Spec 22-28 batch (prep PR P0.1) — landed with no consumers, so no
        # feature branch has to edit this function.
        "last_word_limit": p.last_word_limit,
        "governance_proximity": p.governance_proximity,
        "narrow_limit": p.narrow_limit,
        "narrow_drain_iters": p.narrow_drain_iters,
        "blocked_turn_limit": p.blocked_turn_limit,
        "schema_reject_limit": p.schema_reject_limit,
        # Copy: the returned dict is persisted/serialized by callers, and handing
        # out the policy's own dict would let a caller mutate a frozen policy.
        "capability_overrides": dict(p.capability_overrides),
        # F156 (G5) — bound the tester's not_applicable escape.
        "not_applicable_soft_limit": p.not_applicable_soft_limit,
    }


def _coerce_overrides(raw: Any, default: dict[str, Any]) -> dict[str, Any]:
    """Spec 22-28 P0.1 — read ``capability_overrides`` off unvalidated JSON.

    Absent -> the dataclass default (a fresh copy, never the shared instance);
    a non-mapping -> ``{}`` (the disable value), because a malformed knob must
    degrade to today's behaviour rather than fail the whole policy load."""
    if raw is None:
        return dict(default)
    if not isinstance(raw, dict):
        return {}
    return dict(raw)


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
        # Spec 07: `max(0, …)` — NOT max(1) — so 0 disables the planning-churn
        # detector entirely. Absent key -> dataclass default (6).
        plan_streak_limit=max(
            0, int(d.get("plan_streak_limit", base.plan_streak_limit))),
        # Spec 10: `max(0, …)` clamps — a small/negative min is meaningless, and
        # `wedge_stall_limit == 0` disables the wedged-graph probe entirely.
        wedge_min_tasks=max(
            0, int(d.get("wedge_min_tasks", base.wedge_min_tasks))),
        wedge_stall_limit=max(
            0, int(d.get("wedge_stall_limit", base.wedge_stall_limit))),
        # Spec 11: a plain bool gate for in-turn read-only worktree retrieval on
        # dev turns. Absent key -> the dataclass default (see the field: OFF).
        dev_repo_read=bool(d.get("dev_repo_read", base.dev_repo_read)),
        # --- Spec 12-18 batch (prep PR P0.2) --------------------------------- #
        # Spec 14: reviewer-side retrieval + ungrounded-verdict handling.
        reviewer_repo_read=bool(
            d.get("reviewer_repo_read", base.reviewer_repo_read)),
        # `max(0, …)` — 0 disables the latency fallback entirely (the default).
        review_min_latency_ms=max(
            0, int(d.get("review_min_latency_ms", base.review_min_latency_ms))),
        review_screenshot=bool(d.get("review_screenshot", base.review_screenshot)),
        # Spec 12: gate bootstrap + in-loop cadence. `max(1, …)` on the interval —
        # 0 merges between runs is meaningless, so it clamps up rather than
        # disabling; `gate_bootstrap` is the on/off switch.
        gate_bootstrap=bool(d.get("gate_bootstrap", base.gate_bootstrap)),
        gate_min_merge_interval=max(
            1, int(d.get("gate_min_merge_interval", base.gate_min_merge_interval))),
        # Spec 16: `max(0, …)` — 0 disables the breaker / the livelock detector,
        # matching the Spec 04 / Spec 07 / Spec 10 convention above.
        revise_chain_limit=max(
            0, int(d.get("revise_chain_limit", base.revise_chain_limit))),
        revise_livelock_limit=max(
            0, int(d.get("revise_livelock_limit", base.revise_livelock_limit))),
        # GL01 (Item 1): the web probe is a plain on/off bool (default ON). The
        # frame count uses `max(0, …)` — 0 asserts on the first paint — matching
        # the Spec 04 / Spec 07 / Spec 10 clamp convention.
        web_probe=bool(d.get("web_probe", base.web_probe)),
        web_probe_frames=max(
            0, int(d.get("web_probe_frames", base.web_probe_frames))),
        # GL04 (GAP-4): the diff-deadlock breaker is a plain on/off bool; the two
        # thresholds clamp to [0, 1] (a distance / a fraction — a value outside the
        # unit interval is meaningless).
        diff_deadlock=bool(d.get("diff_deadlock", base.diff_deadlock)),
        diff_stasis_epsilon=min(1.0, max(
            0.0, float(d.get("diff_stasis_epsilon", base.diff_stasis_epsilon)))),
        revert_overlap=min(1.0, max(
            0.0, float(d.get("revert_overlap", base.revert_overlap)))),
        # GL04 (GAP-5): `max(0, …)` on the window — 0 disables the clamp detector
        # (matching the Spec 04/07/10/16 convention); the ratios clamp to [0, 1].
        convergence_window=max(
            0, int(d.get("convergence_window", base.convergence_window))),
        convergence_clamp_ratio=min(1.0, max(
            0.0, float(d.get("convergence_clamp_ratio", base.convergence_clamp_ratio)))),
        convergence_clamp_merge_rate=min(1.0, max(
            0.0, float(d.get("convergence_clamp_merge_rate",
                             base.convergence_clamp_merge_rate)))),
        convergence_release_ratio=min(1.0, max(
            0.0, float(d.get("convergence_release_ratio",
                             base.convergence_release_ratio)))),
        convergence_release_merge_rate=min(1.0, max(
            0.0, float(d.get("convergence_release_merge_rate",
                             base.convergence_release_merge_rate)))),
        # GL05 (Item 2): plain on/off bool (default ON) — the a-priori file
        # partition. Absent key -> the dataclass default.
        strict_file_partition=bool(
            d.get("strict_file_partition", base.strict_file_partition)),
        # --- Spec 22-28 batch (prep PR P0.1) ---------------------------------- #
        # `max(0, …)` — NOT max(1) — on every int, matching the Spec 04/07/10/16
        # convention: 0 is the DISABLE value and must restore today's behaviour.
        # Absent key -> the dataclass default.
        last_word_limit=max(
            0, int(d.get("last_word_limit", base.last_word_limit))),
        # A proximity FRACTION, so it clamps to [0, 1] like the GL04 ratios; 0.0
        # disables the segment.
        governance_proximity=min(1.0, max(
            0.0, float(d.get("governance_proximity", base.governance_proximity)))),
        narrow_limit=max(0, int(d.get("narrow_limit", base.narrow_limit))),
        narrow_drain_iters=max(
            0, int(d.get("narrow_drain_iters", base.narrow_drain_iters))),
        blocked_turn_limit=max(
            0, int(d.get("blocked_turn_limit", base.blocked_turn_limit))),
        schema_reject_limit=max(
            0, int(d.get("schema_reject_limit", base.schema_reject_limit))),
        # A mapping, so the disable value is `{}`. Coerced defensively: this dict
        # comes straight off unvalidated JSON on disk, and a non-mapping there must
        # degrade to "no overrides" rather than crash policy load for the whole run.
        capability_overrides=_coerce_overrides(
            d.get("capability_overrides"), base.capability_overrides),
        # F156 (G5): `max(0, …)` — NOT max(1) — so 0 disables the escalation
        # entirely, matching the gate_stall_limit / plan_streak_limit convention.
        not_applicable_soft_limit=max(
            0, int(d.get("not_applicable_soft_limit",
                         base.not_applicable_soft_limit))),
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


def inflight_owned_paths_by_task(ledger: Any) -> dict[str, set[str]]:
    """Return active DEV path ownership keyed by the task that may advance it.

    GL05 originally flattened ownership into one set. That cannot represent a
    revise lineage: a rejected PR remains non-terminal while its ``revise:`` task
    back-links that PR, so the predecessor's paths made the successor collide with
    itself forever. Keep the owner identity and transfer every live ancestor's
    claim to the newest live successor. Unrelated tasks are still held off those
    paths; only the successor itself may advance the lineage.

    Ownership combines declared task paths with observed ``changed_paths`` from
    non-terminal PRs. Prose-silent work with neither signal contributes nothing,
    preserving the existing fail-open-on-unknown behavior.
    """
    observed_by_task: dict[str, set[str]] = {}
    live_pr_tasks: set[str] = set()
    pr_owner: dict[str, str] = {}
    list_prs = getattr(ledger, "list_prs", None)
    if callable(list_prs):
        for pr in list_prs():
            if pr.get("status") not in ("merged", "superseded", "abandoned", "closed"):
                tid = pr.get("task_id")
                if tid:
                    task_id = str(tid)
                    live_pr_tasks.add(task_id)
                    pr_id = str(pr.get("pr_id") or "")
                    if pr_id:
                        pr_owner[pr_id] = task_id
                    changed = {_paths.normalize_path(str(p))
                               for p in (pr.get("changed_paths") or []) if p}
                    if changed:
                        observed_by_task.setdefault(task_id, set()).update(changed)

    tasks = list(ledger.list_tasks(role=DEV))
    # predecessor task -> its first live revise successor. Multiple live successors
    # are already an invalid lineage; choosing the first keeps one owner and lets the
    # within-batch partition serialize any duplicate behind it.
    successor: dict[str, str] = {}
    for task in tasks:
        predecessor_pr = str(getattr(task, "pr_id", "") or "")
        # A completed revise task can still be the bridge to its own live PR and
        # the next revise generation. Retain that bridge while its PR is live;
        # otherwise terminal tasks are not active successors.
        terminal_without_live_pr = (
            task.state in ("done", "dropped")
            and task.task_id not in live_pr_tasks
        )
        if not predecessor_pr or terminal_without_live_pr:
            continue
        predecessor_task = pr_owner.get(predecessor_pr)
        if predecessor_task and predecessor_task != task.task_id:
            successor.setdefault(predecessor_task, task.task_id)

    def _latest_successor(task_id: str) -> str:
        current = task_id
        seen = {current}
        while successor.get(current) and successor[current] not in seen:
            current = successor[current]
            seen.add(current)
        return current

    owned: dict[str, set[str]] = {}
    for task in tasks:
        if task.state != "doing" and task.task_id not in live_pr_tasks:
            continue
        paths = (_paths.task_touched_paths(task)
                 | observed_by_task.get(task.task_id, set()))
        if paths:
            owned.setdefault(_latest_successor(task.task_id), set()).update(paths)
    return owned


def inflight_owned_paths(ledger: Any) -> set[str]:
    """The flat union of :func:`inflight_owned_paths_by_task`.

    Kept for diagnostics and compatibility. The concurrent scheduler uses the
    keyed form so a revise is not blocked by the ownership it inherited.
    """
    owned: set[str] = set()
    for paths in inflight_owned_paths_by_task(ledger).values():
        owned |= paths
    return owned


def hot_owned_paths_by_task(ledger: Any, hot: dict[str, int]) -> dict[str, set[str]]:
    """The hot-path subset of keyed in-flight ownership.

    F159's hot-file gate needs the same successor exception as GL05's strict
    partition. Otherwise a path becomes redispatchable under the strict gate but
    remains permanently blocked once conflict history makes it hot.
    """
    if not hot:
        return {}
    hot_set = set(hot)
    out: dict[str, set[str]] = {}
    for task_id, paths in inflight_owned_paths_by_task(ledger).items():
        held = {path for path in hot_set if _paths.paths_intersect(paths, {path})}
        if held:
            out[task_id] = held
    return out


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
        _state = ledger.get_run_state()
    except Exception:  # noqa: BLE001
        _state = {}
    # GL04 (GAP-5): the run-level convergence clamp forces serial integration —
    # honored ABOVE the foundation gate/ramp (a churning wide fan-out is exactly the
    # run this brake exists for) and independent of whether the foundation gate is
    # engaged. `_account_convergence_clamp` sets/clears the flag with hysteresis; the
    # release path restores this to the base cap, so the clamp can never wedge — it
    # only ever narrows concurrency to 1, never makes a dispatchable task un-runnable.
    # GL05 (Item 4): this superseded-ratio clamp IS the RQ6 "is parallelism paying
    # off?" health brake — over threshold it freezes fan-out to serial; GL05 asserts
    # the wiring rather than adding a second signal.
    if _state.get("convergence_clamped"):
        return 1
    fstatus = str(_state.get("foundation_status", ""))
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


def _gate_scoring_runs(runs: Any) -> list:
    """The test-runs the ACCEPTANCE-GATE detectors score, excluding the web:probe.

    SPEC-30: the web:probe is a LIVENESS oracle (does the assembled page render and
    respond to input), not part of the acceptance-gate SCORE. It has its own
    enforcement — the completion gate blocks `done` on a red probe (S2), and the
    per-PR probe grounds the reviewer (S4). Scoring it inside `gate_not_improving`
    conflates two different signals on one axis: a 0/1 probe score interleaves with
    the acceptance-command count, so a mid-build integration that renders but has no
    input wired yet (legitimately) pins the 'latest gate' score to 0 and trips the
    acceptance-CHURN detector (run 6: a per-PR probe first, then the master probe,
    each drove a false gate_not_improving pressure). Exclude BOTH the per-PR and the
    master/delivery probe here; the acceptance-gate detectors score on registered
    test commands + runtime launch + the delivery verdict. Guarded — a malformed
    list degrades to itself rather than failing the detector."""
    try:
        from .web_probe import _PROBE_TASK_ID, PR_PROBE_TASK_ID
        probe_task_ids = {PR_PROBE_TASK_ID, _PROBE_TASK_ID}
    except Exception:  # noqa: BLE001
        probe_task_ids = {"web-probe-pr", "web-probe"}
    try:
        return [r for r in (runs or [])
                if str((r or {}).get("task_id")) not in probe_task_ids]
    except Exception:  # noqa: BLE001
        return list(runs or [])


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
            runs = _gate_scoring_runs(list_test_runs())
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
    # Spec 25 (Item 3a): this turn was refused by the SCHEMA — it never parsed, so
    # it is not a plan that made no progress, it is a sentence the validator would
    # not accept. Kept distinct from `unproductive` (a worker that connected and
    # produced nothing usable) because they have different cures: one is fixed by
    # routing around a member, the other by fixing a model. Consumed only by
    # `_apply_outcome`'s PM accounting; `schema_reject_limit` bounds it.
    schema_rejected: bool = False
    # SPEC-23 (Item 2): how the runner CLASSIFIED a last-word turn, as
    # ``{"outcome": accepted|done|confirmed|unparsed, "rationale": str,
    #    "task_ids": [...]}``. Set only for a ``LastWord`` action; ``None`` on every
    # other turn, so nothing else in the loop changes shape.
    #
    # The runner classifies because only it can see what survived materialization —
    # the Spec 08 dedupe gate and the Spec 15 capability lint decide whether the
    # PM's proposal produced ROWS, and "a task materialized" (not "a response
    # arrived") is the reset condition, or the intervention would be a licence to
    # loop. `_intervene` reads this to apply the reset map and record the outcome.
    last_word: Optional[dict[str, Any]] = None


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
    # Spec 16 (Phase 3): revise-livelock tracking. `last_broken_count` is the last
    # count of `revise_chain_broken` decisions; `last_broken_merges` the last count
    # of merged PRs (progress anywhere resets the window); `last_broken_iter` the
    # iteration when either last changed. When broken > 0 and neither has changed
    # for `revise_livelock_limit` iterations, the PM re-plan did not unstick
    # anything → stop `revise_livelock`.
    last_broken_count: int = -1
    last_broken_merges: int = -1
    last_broken_iter: int = 0
    # F155: consecutive delivery-review rejections (findings filed) in this run.
    # At delivery_review_round_limit the loop stops `delivery_review_stalled`.
    # Reset to 0 on a PASSING delivery review.
    delivery_review_rounds: int = 0
    # Spec 07: consecutive PM `planned` turns with ZERO interleaved worker turns.
    # Incremented in `_apply_outcome` ONLY on a `planned` outcome (the PM planning
    # churn pathology); reset to 0 by every branch a WORKER turn reaches (task_done
    # / review_done / task_blocked / the PR branches — i.e. exactly where pm_idle is
    # reset) AND by `governance_progress` (FIX 1: governance advancing is bounded
    # progress toward implementation, guarded by max_review_rounds, and its design
    # phase has no worker turn to reset the streak — so counting it would false-fire
    # planning_churn before implementation tasks ever exist). At plan_streak_limit
    # the run stops `planning_churn`.
    plan_streak: int = 0
    # Spec 25 (Item 3a): consecutive PM turns rejected for SHAPE (the turn never
    # parsed). Absorbed instead of charged to `pm_idle` while under
    # `schema_reject_limit`; RESET to 0 by any turn that parsed, so a transient
    # malformed response costs nothing. Past the limit the rejections resume
    # counting as idle and the ordinary `no_progress` stop lands — bounded, with
    # no new stop reason (batch regression lock 1).
    schema_rejects: int = 0
    # Spec 25 (Item 1): blocked turns per (member_id, task_id). "I am blocked" is
    # a legal, progress-bearing answer — and therefore has to be bounded, or it
    # becomes a way to idle forever. At `blocked_turn_limit` the turn is ALSO
    # marked unproductive so the existing F127 recovery ladder (escalate the
    # model, reassign, PM-assist) takes the task, instead of the same member
    # blocking the same task on every re-open.
    blocked_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    # Spec 10: consecutive iterations observed WEDGED — a `wedge_min_tasks`+ todo
    # backlog with no dispatchable worker head. Incremented in
    # `_account_dispatch_wedge`; reset to 0 the moment any dispatchable work
    # appears (or the backlog shrinks below the floor). At `wedge_stall_limit` the
    # run stops `dispatch_wedged`.
    wedge_streak: int = 0
    # SPEC-23 (Item 3, bound 1): how many LAST-WORD interventions this run has
    # spent. The run-wide budget is `last_word_limit` (default 2), so the whole
    # feature can cost at most that many extra PM turns — 2 iterations and 2 model
    # calls against a default `max_iterations` of 200. PERSISTED (see
    # `_WINDOW_STREAK_FIELDS`) because the budget must not silently re-arm on
    # `errorta continue`: without that, N continues buy N*limit interventions and
    # the bound is fiction.
    last_words: int = 0
    # SPEC-23 (Item 3, bound 2): `detector -> (iteration, merged_pr_count)` at that
    # detector's last intervention. A SECOND intervention for the same detector is
    # refused BEFORE the turn is dispatched unless the merged-PR count has risen
    # since — the same "any merge anywhere is progress" signal
    # `_account_revise_livelock` already trusts, so no new notion of progress. This
    # is what makes the pathological case (a detector that keeps re-tripping because
    # the PM's proposal did nothing) cost exactly ONE turn, not one per trip.
    # Deliberately NOT persisted: it is a within-run routing decision, and a
    # continue is an operator intervention that may well have changed the situation.
    last_word_by_detector: dict[str, tuple[int, int]] = field(default_factory=dict)
    # --- SPEC-27 (Items 2-4) — the intervention ladder, as state -------------- #
    # `detector -> rung index into _DETECTOR_LADDERS[detector]`. Advances on every
    # trip; reset to 0 for EVERY detector on PROGRESS (`_ladder_progress`), which
    # requires a merged PR or a recovered GL04 window — i.e. the run actually
    # integrated something. PERSISTED via `run_state["narrow_ladder"]` so a
    # checkpoint/resume cycle cannot hand a mid-ladder run a fresh ladder.
    narrow_rungs: dict[str, int] = field(default_factory=dict)
    # Bound 3: CHARGED narrowing engagements, run-wide, across all detectors and
    # all ladder resets. Capped by `policy.narrow_limit`. Monotone — a ladder reset
    # never refunds it, which is what makes the budget a budget. A narrow that was
    # already satisfied (the flag is up) or whose mechanism is disabled by policy
    # is a recorded NO-OP: the rung advances and this is NOT charged.
    narrows_used: int = 0
    # Bound 2': every narrowing rung defers a stop by EXACTLY ONE iteration (the
    # detector re-trips next iteration and takes the next rung), so this counts the
    # extra iterations the ladder has bought — charged rungs and no-op rungs alike.
    # Capped at `narrow_limit * narrow_drain_iters` (15 by default). Monotone and
    # never reset, so it is the hard ceiling on the run-length cost of this spec —
    # the number the boundedness test asserts. No-ops are counted here precisely
    # because they are NOT counted by `narrows_used`, and an uncharged rung still
    # costs an iteration.
    narrow_deferrals: int = 0
    # `M₀` — the merged-PR count the current ladder generation was anchored at
    # (-1 = never anchored). PROGRESS is `merged_pr_count > M₀`, the exact signal
    # `_account_revise_livelock` and SPEC-23's bound 2 already trust.
    narrow_anchor_merges: int = -1
    # `narrow action -> the iteration it was engaged at`, for the force-lift cap.
    narrow_engaged_at: dict[str, int] = field(default_factory=dict)


@dataclass
class LoopResult:
    stop_reason: str
    counters: LoopCounters
    detail: dict[str, Any] = field(default_factory=dict)


# --- SPEC-27 (Item 1) — the detector outcome contract ----------------------- #
#
# THE DEFECT: seven `_account_*` detectors shared the signature
# ``(ledger, c, policy) -> Optional[LoopResult]``, whose entire vocabulary is
# "continue" or "DIE". A detector that wanted to say *"clamp concurrency and keep
# going"* had to reach around its own contract and mutate run state as a side
# effect while returning ``None`` — which is exactly what
# `_account_convergence_clamp` does. It works, but it is INVISIBLE to the caller:
# the loop cannot tell "nothing happened" from "I just narrowed the run", so it
# cannot count interventions and therefore cannot BOUND them.
#
# THE CONTRACT — a strict GENERALISATION of today's control flow, not a
# replacement:
#
#   | outcome    | loop action                                  | model calls | short-circuits? |
#   |------------|----------------------------------------------|-------------|-----------------|
#   | None       | nothing                                      | 0           | no  (today)     |
#   | Narrow     | engage the action, record it, advance the rung| 0           | NO              |
#   | Escalate   | hand to SPEC-23's `_intervene`               | <=1 (23's)  | yes             |
#   | Stop       | `LoopResult(reason, c, detail)`              | 0           | yes (today)     |
#
# Mapping ``None -> fall through`` and ``Stop -> return`` reproduces the existing
# early-return chain EXACTLY, so a chain in which every detector returns `None` or
# `Stop` executes the instruction sequence it executes today. `Narrow` falling
# through is also already the shape of the code: `_account_convergence_clamp` is a
# pure-`Narrow` detector wired BEFORE Spec 16's stop in both chains and
# deliberately does not short-circuit.

# The narrowing actions. Each is an EXISTING mechanism made legible, not a new one.
NARROW_CLAMP_FANOUT = "clamp_fanout"            # GL04's existing convergence clamp
NARROW_FORCE_INTEGRATION = "force_integration"  # drain: merge-first + serial dispatch
NARROW_CLAMP_PLANNING = "clamp_planning"        # dig deeper for a worker turn first
NARROW_FORCE_LIFT = "force_lift"                # F159's existing freeze force-lift
NARROW_ALERT_ONLY = "alert_only"                # F139 WS-A's existing stall heartbeat

# The two non-narrowing rungs, named so `_DETECTOR_LADDERS` is one flat table.
RUNG_ESCALATE = "escalate"   # SPEC-23's last-word PM turn, unchanged
RUNG_STOP = "stop"           # `LoopResult(reason, c, detail)`, byte-identical


@dataclass(frozen=True)
class Narrow:
    """A detector's diagnosis answered by NARROWING the run instead of ending it.

    Falls through: the loop engages ``action``, records it, advances that
    detector's rung, and CONTINUES the detector chain — a `Narrow` is not a
    verdict on the iteration, it is a change of strategy within it."""
    action: str                    # one of the NARROW_* constants
    detector: str                  # the stop reason this ladder is deferring
    evidence: str = ""             # the string `_maybe_raise_monitor` already gets
    detail: dict[str, Any] = field(default_factory=dict)
    # True when the DETECTOR already performed the side effect itself (F139 WS-A's
    # alert, F159's force-lift). The loop records it for legibility and charges
    # nothing — these are not ladder rungs, they are the detector's own permanent
    # behaviour, reported so the contract is visibly TOTAL.
    self_applied: bool = False


@dataclass(frozen=True)
class Escalate:
    """The stop that WOULD have fired, routed to SPEC-23's last-word PM turn first.

    This spec does not re-plumb the intervention: `_apply_detector_outcome` lowers
    an `Escalate` to the same `LoopResult` the detector used to return and hands it
    to `_intervene`, so `last_word_limit`, the same-detector-once rule and the
    non-recursion snapshot all apply unchanged. There is exactly ONE intervention
    path in this module and it is SPEC-23's."""
    reason: str
    detector: str
    evidence: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Stop:
    """The terminal rung — byte-identical to today's stop: same reason, same detail
    keys, same exit code. Exhausting a ladder is not a new outcome."""
    reason: str
    detector: str
    evidence: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


DetectorOutcome = Optional[Union[Narrow, Escalate, Stop]]

# --- SPEC-27 (Item 3) — the per-detector rung table -------------------------- #
#
# THE TABLE *IS* THE SPEC. The machinery is uniform; the rung LISTS are not,
# because narrowing a wedge makes it worse while narrowing a churning fan-out
# fixes it. Ordering principle (F127's, and GL04's, already): cheap mechanical
# zero-model-call narrowing first, the judgement-heavy run-scoped escalation next,
# the stop last.
#
# `test_spec27_convergence_control.py` asserts this covers EXACTLY the reasons
# SPEC-23 classes HEURISTIC (plus the one it deferred here), that no HARD reason
# appears, and that the last rung of every tuple is `RUNG_STOP`.
_DETECTOR_LADDERS: dict[str, tuple[str, ...]] = {
    # Quiescence with approved-but-unmerged work is the cheapest recoverable
    # shape, so integration is tried before a turn is spent. The clamp follows so a
    # re-armed run cannot immediately re-fan-out into the same stasis.
    NOT_CONVERGING: (NARROW_FORCE_INTEGRATION, NARROW_CLAMP_FANOUT,
                     RUNG_ESCALATE, RUNG_STOP),
    # Fan-out is not the problem — a RED GATE is. Nothing mechanical helps; the
    # escalation carries the failing gate output the detector already computed.
    GATE_NOT_IMPROVING: (RUNG_ESCALATE, RUNG_STOP),
    # The detector's own diagnosis is "PM plan turns with zero interleaved worker
    # turns", so forcing the next turn to be a worker turn IS the remedy.
    PLANNING_CHURN: (NARROW_CLAMP_PLANNING, RUNG_ESCALATE, RUNG_STOP),
    # NO narrowing rung BY CONSTRUCTION — narrowing a graph with nothing
    # dispatchable makes it strictly worse. Escalate with the named culprit deps.
    DISPATCH_WEDGED: (RUNG_ESCALATE, RUNG_STOP),
    # GL04's clamp is ALREADY the rung below this stop in both chains (it is wired
    # immediately before `_account_revise_livelock`), so this spec adds only the
    # escalate rung and does not re-implement the clamp.
    REVISE_LIVELOCK: (RUNG_ESCALATE, RUNG_STOP),
    # A delivery review judges the INTEGRATED head, so draining pending merges
    # changes the thing under review before anyone is asked about it.
    DELIVERY_REVIEW_STALLED: (NARROW_FORCE_INTEGRATION, RUNG_ESCALATE, RUNG_STOP),
    # SPEC-23's rung, unchanged. No mechanical narrowing applies to an idle PM.
    NO_PROGRESS: (RUNG_ESCALATE, RUNG_STOP),
    # F127's four task-scoped rungs run BEFORE this reason is ever produced, and
    # SPEC-23 already appended the escalate rung. Recorded here so a reader sees
    # F127 as an INSTANCE of the general pattern, not an exception to it.
    WORKER_UNPRODUCTIVE: (RUNG_ESCALATE, RUNG_STOP),
    # F128 already re-prompts the PM with the open item set `completion_refused_limit`
    # times; a second intervention asks the same party the same question.
    COMPLETION_BLOCKED: (RUNG_STOP,),
    # SPEC-23 Item 1 deferred this reason to this spec explicitly. It is CLI
    # SUCCESS-class, so it escalates ONLY when the ledger still holds open work —
    # i.e. when it is a wedge wearing a success label. A refused escalation returns
    # `no_actionable_work` unchanged, still EXIT_OK (Item 5).
    NO_ACTIONABLE_WORK: (RUNG_ESCALATE, RUNG_STOP),
}

# The two pure-`Narrow` detectors. They have no ladder because they have no stop:
# they narrow FOREVER (the foundation heartbeat) or force-lift once (F159). Listed
# so the contract is visibly total and so `_apply_detector_outcome` can charge
# nothing for them.
_SELF_APPLIED_NARROWS = frozenset({NARROW_ALERT_ONLY, NARROW_FORCE_LIFT})


# --- Spec 22-28 batch (prep PR P0.2) — reserved run_state keys --------------- #
#
# ``run_state.json`` is a free-form document (``LedgerStore.get_run_state``
# merges whatever is on disk over a small default), so a new key needs no
# migration: it is ADDITIVE and ABSENT->FALSY, exactly like the keys that already
# live there (``gate_due``, ``convergence_clamped``, ``foundation_status``,
# ``frozen_paths``). Reserving the names here — one place, before five branches
# start writing — is what keeps two specs from picking the same key for different
# shapes. NOTHING in this commit reads or writes them.
#
#   detector_state  (SPEC-24) — the published snapshot of every detector's current
#                   reading vs its threshold, for the governance_state prompt
#                   segment. Must be CLEARED at run start (a stale snapshot from
#                   the previous run would be rendered as live).
#   last_words      (SPEC-23) — how many last-word interventions this run has
#                   spent, and against which detectors, so the run-wide budget
#                   survives an `errorta continue`.
#   narrow_ladder   (SPEC-27) — per-detector narrowing rung state, likewise
#                   budget-bearing and likewise must survive a resume.
#   role_closure    (SPEC-26) — the capable/deferred/unclosable verdict per role,
#                   so the topology advisory can be resolved rather than re-raised.
RESERVED_RUN_STATE_KEYS = (
    "detector_state",
    "last_words",
    "narrow_ladder",
    "role_closure",
)


# --- Spec 22-28 batch (prep PR P0.2) — the resume asymmetry ------------------ #
#
# THE BUG: ``run_coding_loop`` does ``c = counters or LoopCounters()`` and the one
# production caller (``CodingRunner.run``) passes none, so every detector window
# re-arms from zero on `errorta continue` — while the FLAGS those windows bound
# (``convergence_clamped``, ``frozen_paths``, ``foundation_status``) do survive in
# run_state. A resumed run therefore gets a clamped, wedged, or stalled state with
# a brand-new budget: the exact combination that lets a run spin.
#
# THE FIX: persist the window-bounding counter fields at both terminal writers and
# rehydrate them at the resume/continue seam.
#
# The line drawn — and it is a deliberate one — is **does the state this window
# bounds outlive the run?** A window whose subject is durable (a run-state flag or
# a ledger fact) must not re-arm, because its subject did not reset either:
#
#   hot_freeze_stall     <- run_state.frozen_paths  (a freeze that never force-lifts)
#   foundation_stall     <- run_state.foundation_status
#   last_gate_*          <- the ledger's test runs / delivery verdict
#   last_broken_*        <- the open `revise chain broken:` tasks + merged PRs
#   wedge_streak         <- the todo backlog + dependency graph
#   delivery_review_rounds <- the recorded delivery reviews on the same head
#
# Deliberately NOT persisted:
#
# * ``pm_idle`` / ``false_done_streak`` / ``plan_streak`` — these bound the PM's
#   BEHAVIOUR WITHIN one run, and their subject (a conversation) genuinely does
#   reset. A continue is an operator intervention, usually carrying an
#   interjection; rehydrating `pm_idle == pm_idle_limit` would re-stop the run
#   `no_progress` on its first non-productive turn, which is the failure mode this
#   whole batch exists to remove. Regression lock 6 still holds: `pm_idle_limit`
#   bounds genuinely empty turns inside the resumed run.
# * ``iterations`` / ``model_calls`` / ``since_checkpoint`` — BUDGETS. A continue
#   is meant to grant a fresh one; carrying them makes every continue an instant
#   ``budget_exhausted``.
# * ``last_progress_fp`` / ``last_progress_iter`` — ``_progress_fingerprint``
#   embeds this run's ladder counters (reassignments, escalations, unproductive
#   totals), so the fingerprint cannot be restored without also restoring the F127
#   ladder state — which WOULD change dispatch. Restoring the anchor without the
#   fingerprint is worthless (the first iteration re-seeds the fingerprint and
#   resets the window), so `not_converging` stays as it is today. Making it
#   resume-safe means changing what the fingerprint reads, which is a detector
#   contract change: SPEC-27's seam, not Phase 0's.
# * ``unproductive_counts`` / ``member_fail_counts`` — per-member ladder state; a
#   member one turn from exclusion is a live routing decision, not a window.
#
# Iteration-anchored fields are stored as an ELAPSED count, never as an absolute
# iteration number: the resumed counter restarts ``iterations`` at 0, so a raw
# ``last_gate_iter=17`` would make ``iterations - last_gate_iter`` NEGATIVE and
# disarm the detector for 17 iterations — worse than the bug. Storing the elapsed
# and rehydrating it as a negative anchor preserves the remaining window exactly.

# ``counter field -> persisted key`` for plain, non-iteration-anchored windows.
_WINDOW_STREAK_FIELDS = {
    "delivery_review_rounds": "delivery_review_rounds",
    "wedge_streak": "wedge_streak",
    "foundation_stall": "foundation_stall",
    "hot_freeze_stall": "hot_freeze_stall",
    "last_gate_best": "last_gate_best",
    "last_broken_count": "last_broken_count",
    "last_broken_merges": "last_broken_merges",
    # SPEC-23: the intervention budget. Not a detector window, but it rides the
    # same seam for the same reason — its subject (this run's spent last words)
    # outlives the process, so re-arming it on `errorta continue` would make
    # Item 3's bound fiction: three continues would buy six interventions.
    "last_words": "last_words",
    # SPEC-27 (Item 4, bound 3 + the resume edge case): the narrowing budget and
    # the deferral ceiling. Same reason as `last_words`, and it is LOAD-BEARING:
    # the narrowing FLAGS live in run state (`convergence_clamped`,
    # `integration_only`, `planning_clamped`) and therefore already survive a
    # resume, so without this a checkpoint/resume cycle would hand a narrowed run a
    # brand-new `narrow_limit` — and bound 3 would be fiction.
    "narrows_used": "narrows_used",
    "narrow_deferrals": "narrow_deferrals",
}

# SPEC-27: the per-detector rung map rides the P0.2-reserved `narrow_ladder`
# run-state key rather than the counters block, because it is a MAP (the counters
# block is ints by contract) and because it doubles as the operator-visible ladder
# state. Written live by `_publish_narrow_ladder`; read back here.
NARROW_LADDER_KEY = "narrow_ladder"

# ``(iteration-anchor field, persisted elapsed key)`` — stored as
# ``iterations - anchor`` and rehydrated as ``-elapsed``.
_WINDOW_ELAPSED_FIELDS = (
    ("last_gate_iter", "last_gate_stall"),
    ("last_broken_iter", "last_broken_stall"),
)


def window_counters_to_dict(c: LoopCounters) -> dict[str, Any]:
    """P0.2 — the detector-window slice of ``LoopCounters``, JSON-safe, for the
    terminal ``set_run_state(counters=…)`` writers. Ints only; no fingerprints, no
    budgets, no per-member ladder state (see the block comment above)."""
    out: dict[str, Any] = {
        key: int(getattr(c, field_name, 0))
        for field_name, key in _WINDOW_STREAK_FIELDS.items()
    }
    for field_name, key in _WINDOW_ELAPSED_FIELDS:
        out[key] = max(0, int(c.iterations) - int(getattr(c, field_name, 0)))
    return out


def counters_from_run_state(state: Any) -> Optional[LoopCounters]:
    """P0.2 — rebuild the detector windows a previous run left behind, for a
    resume/continue that would otherwise hand a clamped run a fresh budget.

    Returns ``None`` when there is nothing to restore (no ``counters`` block, or a
    block written before this key existed), so the caller falls through to today's
    ``LoopCounters()`` and behaves EXACTLY as it does now. Fully guarded: a
    malformed value can never fail a run start."""
    try:
        raw = (state or {}).get("counters") or {}
        if not isinstance(raw, dict):
            raw = {}
        ladder = (state or {}).get(NARROW_LADDER_KEY) or {}
        if not isinstance(ladder, dict):
            ladder = {}
        known = set(_WINDOW_STREAK_FIELDS.values()) | {
            key for _f, key in _WINDOW_ELAPSED_FIELDS}
        if not (known & set(raw)) and not ladder:
            return None  # pre-P0.2 counters block — nothing to restore
        c = LoopCounters()
        for field_name, key in _WINDOW_STREAK_FIELDS.items():
            if key in raw:
                setattr(c, field_name, int(raw[key]))
        for field_name, key in _WINDOW_ELAPSED_FIELDS:
            if key in raw:
                # The fresh counter starts at iterations == 0, so anchor the window
                # in the negative past: `iterations - anchor` == the elapsed we saved.
                setattr(c, field_name, -max(0, int(raw[key])))
        # SPEC-27: the rung map, off the reserved `narrow_ladder` key. Only the
        # rung indices carry — the anchor is re-derived on the first trip and the
        # engaged-at marks are per-process (a resumed run re-evaluates every
        # narrowing flag's release band on its first quiescent point, exactly as it
        # re-evaluates GL04's).
        rungs = ladder.get("rungs")
        if isinstance(rungs, dict):
            c.narrow_rungs = {
                str(k): max(0, int(v)) for k, v in rungs.items()
                if isinstance(v, (int, float))
            }
        return c
    except Exception:  # noqa: BLE001 — a run start must never fail on this
        return None


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
    pool_members: Optional[list[tuple[str, str]]] = None,
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
    verifies the integrated delivered head before a ``project_done`` is accepted.

    SPEC-26 (Item 3): ``pool_members`` is the PRE-CLOSURE roster, used for THREAD-POOL
    SIZING only. The concurrent loop sizes its pool once, before the loop, but a role
    unseated by capability closure can be re-seated mid-run — which would raise
    ``runtime_cap`` above a pool sized for the filtered roster and silently serialize
    dispatch behind a full pool. The pool is an upper bound only, so widening it is
    strictly safe; ``None`` (every caller but ``CodingRunner.run``) sizes from
    ``members`` exactly as before."""
    rec = reconciler or CodingReconciler(ledger)
    c = counters or LoopCounters()

    effective = policy_provider() if policy_provider is not None else policy
    if runtime_cap(effective, members, ledger) > 1:
        return _run_concurrent_loop(
            ledger, members, effective, run_turn=run_turn, rec=rec,
            should_cancel=should_cancel, c=c, policy_provider=policy_provider,
            member_tiers=member_tiers, delivery_review=delivery_review,
            pool_members=pool_members,
        )
    return _run_sequential_loop(
        ledger, members, policy, run_turn=run_turn, rec=rec,
        should_cancel=should_cancel, c=c, policy_provider=policy_provider,
        member_tiers=member_tiers, delivery_review=delivery_review,
        pool_members=pool_members,
    )


@dataclass(frozen=True)
class DetectorEvidence:
    """Spec 22-28 P0.3 — a detector's reading, as a VALUE.

    Today every detector derives its evidence, formats it into a string, hands the
    string to ``_maybe_raise_monitor``, and drops it. SPEC-23 needs that evidence
    for the intervention prompt and SPEC-24 for the ``governance_state`` prompt
    segment, so without a carrier the same numbers get re-derived in three places
    and drift (the SPEC-19 "four declarations, two values" shape). The detector
    builds this ONCE; the monitor call and every future consumer read the same
    object.

    Fields:

    * ``detector`` — the detector id, matching the stop reason / monitor key.
    * ``text``     — the human-readable evidence sentence (what used to be the
      bare ``reason`` argument). This is what the attention Problem records.
    * ``value``    — the current reading that is compared against ``threshold``
      (e.g. iterations since the gate last improved). ``None`` when the detector
      has no scalar reading.
    * ``threshold``— the configured limit at which the detector trips, so a reader
      can compute proximity as ``value / threshold`` without knowing the policy.
    * ``window``   — the size of the window the reading was taken over, when the
      detector uses one (else ``None``).

    Frozen and JSON-friendly on purpose: a snapshot may be published to run_state
    and must never be mutated by a reader.
    """
    detector: str
    text: str = ""
    value: Optional[float] = None
    threshold: Optional[float] = None
    window: Optional[int] = None

    @property
    def reason(self) -> str:
        """The string the attention Problem is keyed/recorded with — the evidence
        text, falling back to the detector id (the pre-P0.3 ``reason or detector``
        behaviour, preserved exactly)."""
        return self.text or self.detector

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe projection, for a future ``run_state.detector_state``."""
        return {"detector": self.detector, "text": self.text,
                "value": self.value, "threshold": self.threshold,
                "window": self.window}


def _maybe_raise_monitor(ledger: Any, detector: str,
                         reason: "str | DetectorEvidence") -> DetectorEvidence:
    """F117-03 Progress Monitor producer: surface a stuck *governed* run as an
    attention Problem so a human is told, instead of the run just ending opaquely.

    Best-effort — wrapped so a signal-store hiccup can never break the run loop.
    Only governed runs (mode != off) have a governance stage to key the Problem on;
    ungoverned runs are skipped.

    Spec 22-28 P0.3: ``reason`` may be a plain string (unchanged behaviour — it is
    wrapped into a text-only :class:`DetectorEvidence`) or a pre-built evidence
    value. Either way the evidence is RETURNED rather than discarded, so SPEC-23's
    intervention and SPEC-24's snapshot can read exactly what was raised instead of
    re-deriving it. Nothing consumes the return value in this commit.
    """
    evidence = (reason if isinstance(reason, DetectorEvidence)
                else DetectorEvidence(detector=detector, text=str(reason or "")))
    try:
        from . import attention
        from .governance import GovernanceStore
        state = GovernanceStore.for_ledger(ledger).load_state()
        if state.mode == "off":
            return evidence
        signal = attention.raise_monitor_problem(
            ledger.project_id, stage=state.phase, detector=detector,
            reason=evidence.reason, store=ledger,
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
    return evidence


def _stop_with_evidence(reason: str, c: LoopCounters,
                        evidence: DetectorEvidence, **detail: Any) -> LoopResult:
    """SPEC-23 — a detector's stop, CARRYING the evidence it just computed.

    P0.3 turned detector evidence into a value for exactly this: without a carrier
    the intervention prompt would re-derive the same numbers a third time and drift
    (the SPEC-19 "four declarations, two values" shape). `detail["evidence"]` is
    that value, JSON-safe; `_last_word_evidence` is its first consumer. Purely
    additive — every existing `detail` key a caller passes is preserved, and the
    stop reason is untouched (batch regression lock 1)."""
    if isinstance(evidence, DetectorEvidence):
        detail["evidence"] = evidence.to_dict()
    return LoopResult(reason, c, detail=detail)


def _account_foundation_stall(ledger: Any, c: LoopCounters,
                              policy: CodingAutonomyPolicy) -> DetectorOutcome:
    """F139 WS-A: while the foundation is pending, count clamped iterations; at
    ``foundation_stall_limit`` surface a `foundation_not_converging` signal ONCE
    (the run keeps going, clamped to 1, so a human can guide the PM). Reset when
    the foundation merges. Best-effort — never breaks the loop.

    SPEC-27 Item 1: this detector was ALREADY a pure narrow — it alerts and the run
    continues, clamped, by design. It now SAYS so, returning
    ``Narrow(NARROW_ALERT_ONLY, self_applied=True)`` on the iteration it raises.
    Its side effects are unchanged and it charges no budget; only the return value
    becomes legible to the caller, which is what makes the contract total."""
    try:
        if not foundation_pending(ledger):
            c.foundation_stall = 0
            c.foundation_alerted = False
            return None
        c.foundation_stall += 1
        limit = max(1, policy.foundation_stall_limit)
        # Re-alert every `limit` clamped iterations (a heartbeat), not once: a run
        # can span checkpoints/resumes with fresh counters, and the monitor signal
        # may auto-resolve when block_on_problems is off — a single alert could be
        # missed. `_maybe_raise_monitor` dedups an already-open signal, so this
        # re-raises only after a prior one resolved.
        if c.foundation_stall % limit != 0:
            return None
        c.foundation_alerted = True
        # Spec 13 (Item 2): for a web-only tree the runner persists which specific
        # Item-1 condition is failing (`foundation_stall_reason`); lead the
        # rationale with it so the PM gets a cause it can act on, not a generic
        # three-shape list. Absent (non-web tree / undiagnosable) -> generic.
        try:
            reason = str(ledger.get_run_state().get(
                "foundation_stall_reason", "") or "")
        except Exception:  # noqa: BLE001
            reason = ""
        if reason:
            rationale = (
                "no runnable foundation has merged to master after "
                f"{c.foundation_stall} clamped iterations — worker concurrency "
                "stays at 1 until it lands. This is a no-build web target and it "
                f"is not yet self-resolving: {reason}. A human may need to guide "
                "the PM.")
        else:
            rationale = (
                # Spec 13 (S2): don't assert a manifest is required — a
                # buildless web project (index.html + relative <script src>
                # modules the browser resolves) is foundation-ready with NO
                # manifest, so the old "needs a build manifest" wording sent
                # the PM to add one that never should exist. State both valid
                # foundations.
                "no runnable foundation has merged to master after "
                f"{c.foundation_stall} clamped iterations — worker concurrency "
                "stays at 1 until it lands. A foundation is: a build manifest "
                "+ source entrypoint (node/compiled); a script entrypoint "
                "(python/etc.); or, for a no-build web target, an index.html "
                "whose <script src>/<link> graph resolves entirely against "
                "files on master (no bare-specifier imports / JSX). A human "
                "may need to guide the PM.")
        try:
            ledger.record_decision(
                title="foundation not converging",
                context="foundation_gate",
                choice="foundation_not_converging",
                rationale=rationale,
            )
        except Exception:  # noqa: BLE001
            pass
        evidence = _maybe_raise_monitor(
            ledger, "foundation_not_converging", DetectorEvidence(
                detector="foundation_not_converging",
                text="foundation has not merged to master",
                value=c.foundation_stall, threshold=limit))
        # SPEC-27: the alert-only rung, forever. `self_applied` — the side effect
        # above IS the narrowing, so the loop records nothing further and charges
        # no budget.
        return Narrow(action=NARROW_ALERT_ONLY,
                      detector="foundation_not_converging",
                      evidence=evidence.reason, self_applied=True)
    except Exception:  # noqa: BLE001
        pass
    return None


def _account_hot_file_freeze(ledger: Any, c: LoopCounters,
                             policy: CodingAutonomyPolicy) -> DetectorOutcome:
    """F159 never-lift guard: a hot-file freeze normally lifts when the centralize
    owner's PR merges (runner). If that PR never lands, the file would stay frozen
    forever — so count iterations under an active freeze and force-lift at
    ``hot_file_freeze_stall_limit`` (with a decision + monitor), so the file's work
    resumes and a human is told. Best-effort — never breaks the loop.

    SPEC-27 Item 1: already a pure narrow, and the PRECEDENT for every force-lift
    in this spec — a narrowing that never releases is a wedge, so it is capped and
    lifted with a decision + monitor. It now returns
    ``Narrow(NARROW_FORCE_LIFT, self_applied=True)`` on the iteration it lifts."""
    try:
        if not frozen_paths(ledger):
            c.hot_freeze_stall = 0
            return None
        c.hot_freeze_stall += 1
        limit = max(1, policy.hot_file_freeze_stall_limit)
        if c.hot_freeze_stall < limit:
            return None
        stalled = c.hot_freeze_stall  # P0.3: read the evidence BEFORE the reset
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
        evidence = _maybe_raise_monitor(
            ledger, "hot_file_freeze_stalled", DetectorEvidence(
                detector="hot_file_freeze_stalled",
                text="a hot-file centralize task did not merge in time",
                value=stalled, threshold=limit))
        return Narrow(action=NARROW_FORCE_LIFT, detector="hot_file_freeze_stalled",
                      evidence=evidence.reason, self_applied=True)
    except Exception:  # noqa: BLE001
        pass
    return None


def _account_convergence(ledger: Any, c: LoopCounters,
                         policy: CodingAutonomyPolicy) -> DetectorOutcome:
    """F139 WS-E: detect a run where NOTHING is moving. Compares a cheap progress
    fingerprint (merged heads + PR states + ladder activity) against the last one;
    if it has not changed for ``convergence_stall_limit`` iterations, stop
    `not_converging`. Resets on any motion, so a normal review/rework/self-heal
    cycle (which keeps opening/transitioning PRs or running the ladder) never trips
    it.

    SPEC-27 Item 3 — ladder ``FORCE_INTEGRATION -> CLAMP_FANOUT -> ESCALATE ->
    STOP``: quiescence with approved-but-unmerged work is the cheapest recoverable
    shape, so integration is tried before a turn is spent; the clamp follows so a
    re-armed run cannot immediately re-fan-out into the same stasis."""
    try:
        fp = _progress_fingerprint(ledger, c)
    except Exception:  # noqa: BLE001
        return None
    if fp != c.last_progress_fp:
        c.last_progress_fp = fp
        c.last_progress_iter = c.iterations
        return None
    limit = max(1, policy.convergence_stall_limit)
    stalled = c.iterations - c.last_progress_iter
    if stalled < limit:
        return None
    # P0.3: build the evidence ONCE — the monitor Problem and SPEC-23's
    # intervention prompt must read the same object, not two re-derivations.
    evidence = DetectorEvidence(
        detector="not_converging",
        text="no merged progress, PR transition, or ladder activity",
        value=stalled, threshold=limit)
    _maybe_raise_monitor(ledger, "not_converging", evidence)
    return _trip(ledger, c, policy, NOT_CONVERGING, evidence)


def _account_gate_stall(ledger: Any, c: LoopCounters,
                        policy: CodingAutonomyPolicy) -> DetectorOutcome:
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
    stalled = c.iterations - c.last_gate_iter
    if stalled < policy.gate_stall_limit:
        return None
    # Spec 21: a GREEN gate is not a stalled one. The score is "how many commands
    # pass", so a gate with nothing failing sits at its maximum forever and can
    # never strictly increase — which this detector read as churn and used to STOP
    # a healthy run. That is what killed the gravity-golf run at iteration 22 with
    # 6/6 PRs merged and zero revises: the only gate signal was a runtime probe
    # that PASSED, twice.
    #
    # The pathology this detector actually exists for is a RED gate that stays red
    # (stuck at 6/12 while the PR head churns). So require something to be failing
    # before calling it a stall. When the gate is green there is nothing to
    # improve — the run should be ended by the definition of done, not by this.
    if not _gate_has_failure(ledger):
        return None
    evidence = DetectorEvidence(
        detector="gate_not_improving",
        text=(f"acceptance gate has not improved for {policy.gate_stall_limit} "
              f"iterations (score={score})"),
        value=stalled, threshold=policy.gate_stall_limit)
    _maybe_raise_monitor(ledger, "gate_not_improving", evidence)
    # SPEC-27 Item 3 — ladder `ESCALATE -> STOP`. Fan-out is not the problem here;
    # a RED gate is, so no narrowing rung applies and the escalation carries the
    # evidence (per-command exit codes / delivery verdict) the detector just built.
    return _trip(ledger, c, policy, GATE_NOT_IMPROVING, evidence)


def _gate_has_failure(ledger: Any) -> bool:
    """Is anything in the latest gate result actually FAILING? Only then can the
    gate meaningfully "not improve". Mirrors `_gate_fingerprint`'s reading of the
    same record (per-command exit codes, else the run-level `passed`, else the
    delivery verdict) and is guarded identically — an unreadable ledger reports no
    failure, so the detector stays silent rather than stopping a run on a hiccup."""
    try:
        runs = _gate_scoring_runs(ledger.list_test_runs() or [])
    except Exception:  # noqa: BLE001
        return False
    if runs:
        latest = runs[-1]
        results = latest.get("results") or []
        if results:
            return any(r.get("exit_code") not in (0, None) for r in results)
        if latest.get("passed") is not None:
            return not bool(latest.get("passed"))
    try:
        reviews = ledger.list_delivery_reviews() or []
        if reviews:
            return not bool(reviews[-1].get("passed"))
    except Exception:  # noqa: BLE001
        pass
    return False


_CONVERGENCE_RESOLVED = ("merged", "superseded", "blocked", "abandoned")


def _convergence_window_stats(
    ledger: Any, window: int,
) -> Optional[tuple[float, float, int]]:
    """Spec 22-28 P0.3 — the windowed churn reading, as ONE computation.

    Returns ``(superseded_ratio, merge_rate, n)`` over the most recent ``window``
    RESOLVED PRs (merged/superseded/blocked/abandoned), or ``None`` when there is
    no judgeable reading: the detector is disabled (``window <= 0``), the ledger
    could not be read, or fewer than ``window`` PRs have resolved (a metric off 2
    resolved PRs is noise, and an early lone supersession clamping the whole run is
    exactly the flap the hysteresis exists to avoid).

    Extracted verbatim out of ``_account_convergence_clamp``'s inlined arithmetic
    so SPEC-24 (which renders this reading) and SPEC-27 (which ladders on it) read
    the SAME numbers the clamp acts on, instead of each re-deriving them — the
    "four declarations, two values" failure shape, avoided in advance. Pure and
    read-only: it never touches run state and never returns a stop."""
    if window <= 0:
        return None
    try:
        prs = ledger.list_prs()
    except Exception:  # noqa: BLE001 — a churn metric must never break the loop
        return None
    resolved = [p for p in prs if p.get("status") in _CONVERGENCE_RESOLVED]
    if len(resolved) < window:
        return None
    recent = resolved[-window:]
    n = len(recent)
    superseded = sum(1 for p in recent if p.get("status") == "superseded")
    merged = sum(1 for p in recent if p.get("status") == "merged")
    return (superseded / n, merged / n, n)


def _account_convergence_clamp(ledger: Any, c: LoopCounters,
                               policy: CodingAutonomyPolicy) -> DetectorOutcome:
    """GL04 (GAP-5): the run-level convergence health brake — one rung SOFTER than
    Spec 16's lineage-scoped ``revise_livelock`` hard stop, wired BEFORE it in both
    loops. Spec 16's detector is blind to aggregate churn: a run can be below the
    per-lineage break threshold on EVERY chain and still be superseding half its PRs
    across all of them (the observed 53/96-superseded, 30%-merge run).

    Over the last ``convergence_window`` RESOLVED PRs (merged/superseded/blocked/
    abandoned) it reads the superseded-ratio and merge-rate. On the TRIP band it sets
    a ``convergence_clamped`` run-state flag that ``runtime_cap`` honors (-> serial,
    no new fan-out), records a decision, and raises ONE deduped alert. It RELEASES —
    clears the flag — only past a SEPARATE, tighter release band (hysteresis, so the
    clamp never flaps on the boundary). It NEVER returns a stop: the clamp narrows the
    run and buys it a chance to drain before Spec 16's stop lands underneath. It never
    makes a dispatchable task non-dispatchable (a clamped run with one ready task
    still dispatches, serially), so it can never itself wedge the run.

    ``convergence_window == 0`` disables it (``max(0, …)`` convention), restoring
    today's fan-out. Fully guarded — a ledger/run-state hiccup never clamps or stops
    the run.

    SPEC-27 Item 1: this is the detector that PROVED the softer shape works in this
    codebase, and it is FOLDED INTO the new contract rather than rewritten — it now
    returns ``Narrow(NARROW_CLAMP_FANOUT, self_applied=True)`` on the engage
    transition, so the caller can finally tell "nothing happened" from "I just
    narrowed the run". Bands, hysteresis, decision and alert are untouched, and it
    still never produces a `Stop`."""
    # P0.3: the window arithmetic (disabled-check, ledger read, full-window
    # requirement, ratio + merge-rate) now lives in `_convergence_window_stats` so
    # this clamp and its future readers share ONE computation. `None` means "no
    # judgeable reading" — disabled, unreadable, or not yet a full window.
    stats = _convergence_window_stats(ledger, policy.convergence_window)
    if stats is None:
        return None
    ratio, merge_rate, n = stats
    try:
        clamped = bool(ledger.get_run_state().get("convergence_clamped"))
    except Exception:  # noqa: BLE001
        clamped = False
    if not clamped:
        if ratio >= policy.convergence_clamp_ratio \
                or merge_rate <= policy.convergence_clamp_merge_rate:
            _engage_convergence_clamp(ledger, ratio=ratio, merge_rate=merge_rate, n=n)
            return Narrow(
                action=NARROW_CLAMP_FANOUT, detector="convergence_clamp",
                evidence=(f"windowed superseded-ratio {ratio:.0%} / merge-rate "
                          f"{merge_rate:.0%} over {n} resolved PRs"),
                self_applied=True)
    else:
        if ratio <= policy.convergence_release_ratio \
                and merge_rate >= policy.convergence_release_merge_rate:
            _release_convergence_clamp(ledger, ratio=ratio, merge_rate=merge_rate, n=n)
    return None


def _engage_convergence_clamp(ledger: Any, *, ratio: float, merge_rate: float,
                              n: int) -> None:
    """GL04 (GAP-5): trip — set the flag ``runtime_cap`` reads, record the decision,
    raise ONE deduped alert. Called only on the not-clamped -> clamped transition, so
    the alert is keyed once per clamp EPISODE by construction (the flag blocks
    re-entry until a release). Best-effort — never fails the loop."""
    try:
        ledger.set_run_state(convergence_clamped=True)
    except Exception:  # noqa: BLE001
        return
    try:
        ledger.record_decision(
            title="run convergence clamp engaged", context="convergence",
            choice="convergence_clamped",
            rationale=(f"windowed superseded-ratio {ratio:.0%} / merge-rate "
                       f"{merge_rate:.0%} over {n} resolved PRs crossed the clamp "
                       "band; forced serial integration and froze new fan-out to let "
                       "the run drain — soft, releasable, not a stop"))
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import attention
        attention.raise_review_alert(
            str(getattr(ledger, "project_id", "") or ""), stage="development",
            title="run convergence clamp engaged",
            summary=(f"the run is superseding {ratio:.0%} of its recent PRs "
                     f"(merge-rate {merge_rate:.0%}); fan-out is clamped to serial "
                     "until churn recovers."),
            store=ledger)
    except Exception:  # noqa: BLE001 — the alert is best-effort
        pass


def _release_convergence_clamp(ledger: Any, *, ratio: float, merge_rate: float,
                               n: int) -> None:
    """GL04 (GAP-5): release — clear the flag so ``runtime_cap`` restores the policy
    cap and fan-out resumes. Only reached from the clamped branch past the tighter
    release band. Best-effort — never fails the loop."""
    try:
        ledger.set_run_state(convergence_clamped=False)
    except Exception:  # noqa: BLE001
        return
    try:
        ledger.record_decision(
            title="run convergence clamp released", context="convergence",
            choice="convergence_released",
            rationale=(f"windowed superseded-ratio {ratio:.0%} / merge-rate "
                       f"{merge_rate:.0%} over {n} resolved PRs recovered past the "
                       "release band; restored fan-out"))
    except Exception:  # noqa: BLE001
        pass


# --- SPEC-27 (Items 2-4) — the intervention ladder --------------------------- #
#
# THE NON-WEDGE INVARIANT, generalised from GL04's clamp (*"it never makes a
# dispatchable task non-dispatchable — a clamped run with one ready task still
# dispatches, serially"*). EVERY `NARROW_*` action inherits all three parts, and
# each has its own test:
#
#   1. It may only REDUCE CONCURRENCY or DEFER NEW WORK. A run under any narrowing
#      that has one dispatchable task still dispatches it, serially.
#   2. It must carry a RELEASE CONDITION — GL04's hysteretic band, "nothing left
#      to merge", or "a worker turn ran".
#   3. It must FORCE-LIFT at a cap even if its release condition never arrives,
#      with a decision and a monitor signal — `_account_hot_file_freeze`'s
#      existing pattern. A narrowing that never releases IS a wedge.
#
# `NARROW_FORCE_INTEGRATION` is the one that could violate (1) if written
# carelessly: refusing dispatch while nothing was mergeable would go quiescent and
# manufacture a `dispatch_wedged` out of a churn alarm. So it ENGAGES ONLY when at
# least one PR is `mergeable` — i.e. only when a `Merge` action is provably
# available to `decide_next` / `plan_next_batch` — and releases the moment that
# stops being true.
#
# Δ NOTE ON WHAT THESE ACTIONS ACTUALLY DO IN *THIS* ENGINE. Both planners are
# ALREADY merge-first (`decide_next` step 0 / `plan_next_batch`'s exclusive
# `Merge` batch) and the sequential loop is already serial, so "force integration"
# cannot mean "put merges first" — that is not a change. What it can mean, and
# what it does mean here, is: while a merge is available, the CONCURRENT planner
# stops fanning out and hands out at most one worker assign per tick, so approved
# work drains instead of the run opening more fronts against a moving base. Same
# discipline for `NARROW_CLAMP_PLANNING`: `plan_next_batch` already prefers a
# worker assign over a `Plan`, so the only honest way to *force* a worker turn is
# to look HARDER for one — the clamp widens the ready-task over-fetch so a role
# whose head tasks are all gated/excluded finds a dispatchable task behind them
# instead of handing the PM another plan turn. Both are strictly ADDITIVE to
# dispatchability or strictly concurrency-reducing, which is invariant (1).

_NARROW_TITLES = {
    NARROW_CLAMP_FANOUT: "fan-out clamped",
    NARROW_FORCE_INTEGRATION: "integration forced",
    NARROW_CLAMP_PLANNING: "planning clamped",
}

# The two run-state flags this spec adds. `convergence_clamped` is GL04's and is
# NOT re-declared here — `NARROW_CLAMP_FANOUT` engages GL04's own path.
_NARROW_FLAG_KEY = {
    NARROW_FORCE_INTEGRATION: "integration_only",
    NARROW_CLAMP_PLANNING: "planning_clamped",
}

# `_engage_narrow` outcomes.
_NARROW_ENGAGED = "engaged"      # the flag went up — charges `narrow_limit`
_NARROW_SATISFIED = "satisfied"  # already in effect — recorded, NOT charged
_NARROW_NOOP = "noop"            # the mechanism is disabled or inapplicable


def narrow_flags(ledger: Any) -> dict[str, bool]:
    """The narrowing flags the DISPATCH phase reads, as one guarded read.

    Returned as plain bools so a `None`/absent key is falsy and pre-spec run state
    dispatches byte-identically. Never raises: an unreadable run state means "no
    narrowing", which is the permissive direction."""
    try:
        state = ledger.get_run_state() or {}
    except Exception:  # noqa: BLE001 — dispatch must never break on a state read
        return {"integration_only": False, "planning_clamped": False}
    return {"integration_only": bool(state.get("integration_only")),
            "planning_clamped": bool(state.get("planning_clamped"))}


def _mergeable_pr_count(ledger: Any) -> int:
    """PRs sitting at `mergeable` — reviewer-approved AND tests-green, i.e. the
    exact state both planners turn into a `Merge` action. This is the ENGAGE
    PRECONDITION for `NARROW_FORCE_INTEGRATION` (non-wedge invariant 1): the
    narrowing only exists while integration is provably available."""
    try:
        return sum(1 for p in ledger.list_prs() if p.get("status") == "mergeable")
    except Exception:  # noqa: BLE001
        return 0


def _ladder_progress(ledger: Any, c: LoopCounters,
                     policy: CodingAutonomyPolicy) -> bool:
    """PROGRESS — the ONE condition that resets a ladder. Defined by reusing what
    already exists; no new notion of progress is introduced:

    **(a)** ``merged_pr_count > M₀`` — the exact signal `_account_revise_livelock`
    already trusts to reset its own window (*"any merge anywhere is progress"*),
    and the one SPEC-23's bound 2 keys on; or
    **(b)** GL04's window has RECOVERED past the release band
    (``superseded_ratio <= convergence_release_ratio`` **and** ``merge_rate >=
    convergence_release_merge_rate``) over the last ``convergence_window`` resolved
    PRs. Clause (b) is the sharper signal once a full window has resolved; clause
    (a) covers the pre-window case, where GL04's metric deliberately abstains.

    On PROGRESS every ladder's RUNG INDEX resets to 0 and ``M₀`` is re-anchored.
    **Narrowing FLAGS are not cleared by a ladder reset** — each keeps its own
    release condition (GL04's hysteresis; the drain/planning clears in
    `_release_narrow_flags`). Two release paths for one flag is how a clamp starts
    flapping, and hysteresis is the entire reason GL04's bands are separate.

    Neither budget (`narrows_used`, `narrow_deferrals`) is refunded — a reset gives
    the run new STRATEGY, never new BUDGET, which is what keeps bound 3 a bound."""
    merges = _merged_pr_count(ledger)
    if c.narrow_anchor_merges < 0:
        c.narrow_anchor_merges = merges
        return False
    progressed = merges > c.narrow_anchor_merges
    if not progressed:
        stats = _convergence_window_stats(ledger, policy.convergence_window)
        if stats is not None:
            ratio, merge_rate, _n = stats
            progressed = (ratio <= policy.convergence_release_ratio
                          and merge_rate >= policy.convergence_release_merge_rate)
    if progressed:
        c.narrow_anchor_merges = merges
        if c.narrow_rungs:
            c.narrow_rungs = {}
    return progressed


def _narrow_deferral_cap(policy: CodingAutonomyPolicy) -> int:
    """Bound 2' — the HARD ceiling on extra iterations this spec can add to a run.

    Every narrowing rung defers a stop by AT MOST one iteration — the detector
    re-trips next iteration and takes the next rung, and several detectors narrowing
    in the SAME iteration (a `Narrow` falls through) share that one iteration — so
    ``extra iterations <= narrow_deferrals <= cap``. ``narrow_limit *
    narrow_drain_iters`` = 3 * 5 = **15** by default, against a `max_iterations` of
    200: a <=7.5% ceiling on run length.

    A ceiling, not a cost. The ladder itself makes ZERO model calls — no narrowing
    rung dispatches anything, and the only turn-spending rung is SPEC-23's, drawn
    from `last_word_limit`. The iterations bought here run the run's OWN next turns
    against the run's OWN unchanged `max_iterations` / `max_model_calls` budgets,
    both of which are HARD and checked BEFORE any detector runs."""
    return max(0, policy.narrow_limit) * max(0, policy.narrow_drain_iters)


def _ladder_rung(ledger: Any, c: LoopCounters, policy: CodingAutonomyPolicy,
                 detector: str) -> str:
    """Which rung this detector's trip lands on — the ONE place the table is read.

    ``narrow_limit == 0`` collapses every ladder to its escalate/stop TAIL, which
    is exactly the pre-spec control flow (SPEC-23's `_intervene`, which has its own
    `0` disable). Bounds 2' and 3 collapse it the same way once they are spent, so
    an exhausted budget degrades to today's behaviour rather than to a new one."""
    ladder = _DETECTOR_LADDERS.get(detector)
    if not ladder:
        return RUNG_STOP
    tail = RUNG_ESCALATE if RUNG_ESCALATE in ladder else RUNG_STOP
    if policy.narrow_limit <= 0:
        return tail
    _ladder_progress(ledger, c, policy)
    idx = min(int(c.narrow_rungs.get(detector, 0)), len(ladder) - 1)
    rung = ladder[idx]
    if rung in (RUNG_ESCALATE, RUNG_STOP):
        return rung
    # A narrowing rung, but only if the run can still afford one. Bound 3 caps
    # CHARGED engagements; bound 2' caps deferrals whether charged or not (a no-op
    # rung still costs the iteration it defers, and is deliberately not charged to
    # bound 3 — so without 2' a permanently-inapplicable rung would be free).
    if c.narrows_used >= max(0, policy.narrow_limit):
        return tail
    if c.narrow_deferrals >= _narrow_deferral_cap(policy):
        return tail
    return rung


def _trip(ledger: Any, c: LoopCounters, policy: CodingAutonomyPolicy,
          reason: str, evidence: "DetectorEvidence | str",
          **detail: Any) -> DetectorOutcome:
    """A tripped threshold -> the outcome its ladder prescribes.

    The `detail` payload is built EXACTLY as `_stop_with_evidence` builds it, so a
    `Stop` lowered by `_apply_detector_outcome` produces a `LoopResult` that is
    byte-identical to the one the detector returned before this spec."""
    d = dict(detail)
    if isinstance(evidence, DetectorEvidence):
        d["evidence"] = evidence.to_dict()
        text = evidence.reason
    else:
        text = str(evidence or reason)
    rung = _ladder_rung(ledger, c, policy, reason)
    if rung == RUNG_STOP:
        return Stop(reason=reason, detector=reason, evidence=text, detail=d)
    if rung == RUNG_ESCALATE:
        return Escalate(reason=reason, detector=reason, evidence=text, detail=d)
    return Narrow(action=rung, detector=reason, evidence=text, detail=d)


def _advance_rung(ledger: Any, c: LoopCounters, policy: CodingAutonomyPolicy,
                  detector: str) -> None:
    """Monotone rung advance (bound 1). Inert when the ladder is disabled, so
    ``narrow_limit == 0`` writes no counter and no run state — the byte-for-byte
    lock."""
    if policy.narrow_limit <= 0:
        return
    ladder = _DETECTOR_LADDERS.get(detector)
    if not ladder:
        return
    c.narrow_rungs[detector] = min(
        int(c.narrow_rungs.get(detector, 0)) + 1, len(ladder) - 1)
    _publish_narrow_ladder(ledger, c)


def _publish_narrow_ladder(ledger: Any, c: LoopCounters) -> None:
    """Write the ladder to the P0.2-reserved ``narrow_ladder`` run-state key.

    Two jobs, one write: it is what `counters_from_run_state` rehydrates on
    `errorta continue` (without it a checkpoint/resume cycle hands a narrowed run a
    fresh ladder and bound 3 is fiction), and it is the operator-visible "you are
    on rung 2 of 4 for not_converging". Best-effort in every direction."""
    try:
        ledger.set_run_state(**{NARROW_LADDER_KEY: {
            "rungs": {k: int(v) for k, v in sorted(c.narrow_rungs.items())},
            "narrows_used": int(c.narrows_used),
            "deferrals": int(c.narrow_deferrals),
        }})
    except Exception:  # noqa: BLE001 — telemetry must never break the run loop
        pass


def _record_narrow(ledger: Any, out: "Narrow", status: str,
                   rationale: str) -> None:
    """One ledger decision per rung transition (Item 2's acceptance: *"rung
    transitions are ledger decisions"*). Best-effort."""
    try:
        ledger.record_decision(
            title=f"{_NARROW_TITLES.get(out.action, out.action)}: {out.detector}",
            context=f"narrow:{out.detector}",
            choice=f"narrow_{out.action}_{status}",
            rationale=rationale[:2000],
            extra={"detector": out.detector, "action": out.action,
                   "status": status, "evidence": out.evidence[:500]})
    except Exception:  # noqa: BLE001 — recording must never break the run loop
        pass


def _engage_narrow_action(ledger: Any, c: LoopCounters,
                          policy: CodingAutonomyPolicy, out: "Narrow") -> str:
    """Engage one narrowing action. Returns `engaged` / `satisfied` / `noop`.

    `satisfied` (the narrowing is already in effect — e.g. a second detector asking
    for a clamp GL04 already engaged) and `noop` (the mechanism is disabled by
    policy, or its engage precondition is absent) both ADVANCE the rung and are
    both recorded, but NEITHER charges `narrow_limit`: charging twice for one flag
    would let two detectors spend the whole budget on a single state change."""
    action = out.action
    if action == NARROW_CLAMP_FANOUT:
        # GL04's OWN engage path — not a second implementation of it, and not a
        # second release path either: the clamp continues to release only through
        # `_account_convergence_clamp`'s tighter hysteretic band.
        stats = _convergence_window_stats(ledger, policy.convergence_window)
        if stats is None:
            return _NARROW_NOOP  # disabled (`convergence_window == 0`) or no window
        try:
            if bool((ledger.get_run_state() or {}).get("convergence_clamped")):
                return _NARROW_SATISFIED
        except Exception:  # noqa: BLE001
            return _NARROW_NOOP
        ratio, merge_rate, n = stats
        _engage_convergence_clamp(ledger, ratio=ratio, merge_rate=merge_rate, n=n)
        return _NARROW_ENGAGED
    if action == NARROW_FORCE_INTEGRATION and _mergeable_pr_count(ledger) <= 0:
        # Non-wedge invariant 1: with nothing mergeable there is nothing to drain,
        # and deferring dispatch would manufacture the wedge this rung is meant to
        # avoid. A recorded no-op that advances the rung, exactly as the spec's
        # "requested with nothing mergeable" acceptance requires.
        return _NARROW_NOOP
    key = _NARROW_FLAG_KEY.get(action)
    if key is None:
        return _NARROW_NOOP
    if policy.narrow_drain_iters <= 0:
        return _NARROW_NOOP  # the force-lift cap is the flag's only hard release
    try:
        if bool((ledger.get_run_state() or {}).get(key)):
            return _NARROW_SATISFIED
        ledger.set_run_state(**{key: True})
    except Exception:  # noqa: BLE001 — a run-state hiccup narrows nothing
        return _NARROW_NOOP
    c.narrow_engaged_at[action] = c.iterations
    return _NARROW_ENGAGED


def _engage_narrow(ledger: Any, c: LoopCounters, policy: CodingAutonomyPolicy,
                   out: "Narrow") -> str:
    """Apply a `Narrow`: engage it, record it, advance the rung, charge the bounds.

    A `self_applied` narrow (F139 WS-A's heartbeat, F159's force-lift) is the
    DETECTOR's own permanent behaviour, already performed — it has no ladder, is
    recorded nowhere new, and charges nothing. Those two are reported as `Narrow`
    only so the contract is visibly TOTAL and so every detector's answer is
    countable."""
    if out.self_applied or out.action in _SELF_APPLIED_NARROWS:
        return _NARROW_SATISFIED
    status = _engage_narrow_action(ledger, c, policy, out)
    # Bound 2': the deferral is charged whatever the engage result, because the
    # STOP was deferred by an iteration either way.
    c.narrow_deferrals += 1
    if status == _NARROW_ENGAGED:
        c.narrows_used += 1
    _advance_rung(ledger, c, policy, out.detector)
    _record_narrow(
        ledger, out, status,
        rationale=(
            f"{out.detector} tripped ({out.evidence}); answered by narrowing the "
            f"run ({out.action}, {status}) instead of ending it — rung "
            f"{c.narrow_rungs.get(out.detector, 0)} of "
            f"{len(_DETECTOR_LADDERS.get(out.detector, ()))}, "
            f"{c.narrows_used}/{policy.narrow_limit} narrowings used"))
    return status


def _lift_narrow(ledger: Any, c: LoopCounters, action: str, *,
                 forced: bool, rationale: str) -> None:
    """Clear one narrowing flag. `forced` is invariant 3 — the cap lifted it
    although its release condition never arrived, so a human is told."""
    key = _NARROW_FLAG_KEY[action]
    try:
        ledger.set_run_state(**{key: False})
    except Exception:  # noqa: BLE001
        return
    c.narrow_engaged_at.pop(action, None)
    try:
        ledger.record_decision(
            title=f"{_NARROW_TITLES.get(action, action)} lifted",
            context=f"narrow:{action}",
            choice=f"narrow_{action}_{'force_lifted' if forced else 'released'}",
            rationale=rationale[:2000])
    except Exception:  # noqa: BLE001
        pass
    if forced:
        _maybe_raise_monitor(ledger, f"narrow_{action}_stalled", DetectorEvidence(
            detector=f"narrow_{action}_stalled", text=rationale))


def _release_narrow_flags(ledger: Any, c: LoopCounters,
                          policy: CodingAutonomyPolicy) -> None:
    """Non-wedge invariants 2 and 3, evaluated at the quiescent point in BOTH
    chains: release each narrowing flag on its own condition, and FORCE-LIFT it at
    ``narrow_drain_iters`` if that condition never arrives.

    Release conditions, one per action, deliberately distinct from the engage
    conditions (hysteresis — a shared boundary is how a clamp flaps):

    * ``integration_only`` — nothing is `mergeable` any more, so there is nothing
      left to drain;
    * ``planning_clamped`` — a worker turn ran (``plan_streak == 0``, which every
      worker branch of `_apply_outcome` already sets).

    GL04's ``convergence_clamped`` is deliberately ABSENT: it keeps its own
    hysteretic release inside `_account_convergence_clamp`. Two release paths for
    one flag is exactly the flap the bands exist to prevent.

    Reads the FLAGS, not just this process's engage marks: `narrow_engaged_at` is
    per-process while the flags live in run state and survive `errorta continue`,
    so a resumed run must be able to lift a narrowing it did not itself engage —
    otherwise the resume is the wedge. An orphan flag is re-anchored at the current
    iteration, i.e. the resumed run gets one fresh drain window, never none.
    Skipped entirely (no run-state read) for a run that has never narrowed."""
    if not c.narrow_engaged_at and c.narrows_used <= 0:
        return
    live = narrow_flags(ledger)
    engaged = [a for a, key in _NARROW_FLAG_KEY.items() if live.get(key)]
    for action in list(c.narrow_engaged_at):
        if action not in engaged:
            c.narrow_engaged_at.pop(action, None)
    cap = max(0, policy.narrow_drain_iters)
    for action in engaged:
        if action not in c.narrow_engaged_at:
            c.narrow_engaged_at[action] = c.iterations
        since = c.iterations - int(c.narrow_engaged_at.get(action, c.iterations))
        released = (
            _mergeable_pr_count(ledger) <= 0
            if action == NARROW_FORCE_INTEGRATION else c.plan_streak == 0)
        if released:
            _lift_narrow(ledger, c, action, forced=False,
                         rationale=("the narrowing's release condition was met "
                                    f"after {since} iteration(s); restoring normal "
                                    "dispatch"))
        elif since >= cap:
            _lift_narrow(ledger, c, action, forced=True,
                         rationale=(
                             f"the {action} narrowing did not release within "
                             f"{cap} iterations; force-lifting so dispatch resumes "
                             "— a narrowing that never releases is itself a wedge, "
                             "and a human may need to look"))


def _open_work_remains(ledger: Any) -> bool:
    """Does the ledger still hold open tasks or unresolved PRs?

    The guard on the ONE success-class rung (`no_actionable_work`): that reason is
    CLI SUCCESS-class, so it may only be escalated when it is a WEDGE WEARING A
    SUCCESS LABEL. Guarded read; silence means "no open work", i.e. leave the run
    exactly as it is today."""
    try:
        if any(str(getattr(t, "state", "") or "") in task_dedupe.OPEN_STATES
               for t in ledger.list_tasks()):
            return True
    except Exception:  # noqa: BLE001
        return False
    try:
        return any(p.get("status") not in _CONVERGENCE_RESOLVED
                   for p in ledger.list_prs())
    except Exception:  # noqa: BLE001
        return False


def _no_actionable_escalation(ledger: Any, c: LoopCounters,
                              policy: CodingAutonomyPolicy,
                              reason: str) -> Optional[Escalate]:
    """SPEC-27 Item 3 — the rung SPEC-23 Item 1 deferred here explicitly.

    Returns an `Escalate` only when `no_actionable_work` is a wedge wearing a
    success label: the ladder is enabled, the ledger still holds open work, and
    this detector has not already spent its escalate rung. Otherwise `None`, and
    the caller returns the stop exactly as it does today.

    Δ Item 5 — this CANNOT flip an exit code. A refused or abstaining escalation
    returns `LoopResult(NO_ACTIONABLE_WORK, …)` unchanged, which `classify_exit`
    still maps to `EXIT_OK`; there is no path here that converts a success-class
    reason into a failure-class one."""
    if reason != NO_ACTIONABLE_WORK or policy.narrow_limit <= 0:
        return None
    if _ladder_rung(ledger, c, policy, reason) != RUNG_ESCALATE:
        return None
    if not _open_work_remains(ledger):
        return None
    return Escalate(
        reason=NO_ACTIONABLE_WORK, detector=NO_ACTIONABLE_WORK,
        evidence=("nothing is dispatchable but the ledger still holds open work — "
                  "a wedge wearing a success label"),
        detail={"evidence": DetectorEvidence(
            detector=NO_ACTIONABLE_WORK,
            text=("nothing is dispatchable but open tasks or PRs remain"),
        ).to_dict()})


def _apply_detector_outcome(
    ledger: Any,
    members: list[tuple[str, str]],
    policy: CodingAutonomyPolicy,
    c: LoopCounters,
    out: "DetectorOutcome | LoopResult",
    *,
    run_turn: RunTurn,
    should_cancel: Optional[Callable[[], bool]] = None,
    rec: Optional[CodingReconciler] = None,
    delivery_review: Optional[Callable[[Any], Any]] = None,
) -> Optional[LoopResult]:
    """SPEC-27 Item 6 — the ONE application point, called from both chains.

    ``None`` back means KEEP GOING (nothing fired, a `Narrow` was engaged, or
    SPEC-23's PM proposed something actionable); a :class:`LoopResult` back is the
    result to return from the loop.

    Note the asymmetry the caller must honour and only the caller can: a `Narrow`
    does NOT short-circuit the detector chain (the remaining detectors still run
    this iteration), while an `Escalate`/`Stop` does — that is what makes the new
    contract a strict generalisation of today's early-return chain. Callers express
    it as ``if not isinstance(out, Narrow): continue``.

    A bare :class:`LoopResult` is accepted and handed straight to `_intervene`,
    which is byte-for-byte the pre-spec `_last_word` path — that is how the stop
    sites with no `_account_*` producer (hard blocker, member health, cancel,
    budget, F127's ladder) keep working untouched."""
    if out is None:
        return None
    if isinstance(out, Narrow):
        _engage_narrow(ledger, c, policy, out)
        return None  # falls through — the chain continues
    if isinstance(out, Stop):
        # Byte-identical to today's stop: same reason, same detail keys.
        return LoopResult(out.reason, c, detail=dict(out.detail))
    if isinstance(out, Escalate):
        _advance_rung(ledger, c, policy, out.detector)
        out = LoopResult(out.reason, c, detail=dict(out.detail))
    # SPEC-23 owns the intervention. There is no second path.
    return _intervene(ledger, members, policy, c, out, run_turn=run_turn,
                      should_cancel=should_cancel, rec=rec,
                      delivery_review=delivery_review)


def _account_revise_livelock(ledger: Any, c: LoopCounters,
                             policy: CodingAutonomyPolicy) -> DetectorOutcome:
    """Spec 16 (Phase 3): make the revise-chain livelock visible to the loop. The
    Phase 2 breaker blocks a wedged lineage and hands it to the PM — but if the
    PM's re-plan ALSO fails to make progress, the run would otherwise burn to the
    iteration cap. So: count broken lineages (`revise_chain_broken` decisions); when
    that count is non-zero and neither it nor the merged-PR count has changed for
    ``revise_livelock_limit`` iterations (any merge anywhere is progress and resets
    the window), stop `revise_livelock`. ``0`` disables the detector.

    This is the livelock every existing guard is blind to: the breaker keeps tasks
    completing, so the progress fingerprint keeps moving and `not_converging` never
    fires."""
    if policy.revise_livelock_limit <= 0:
        return None
    # Count UNRESOLVED breaker escalations: an open PM "revise chain broken:" task.
    # NOT the append-only `revise_chain_broken` decisions — those are monotonic, so
    # a break the PM later resolved (re-scoped/abandoned) would keep the detector
    # armed and could false-stop a benign no-merge tail. When the PM handles the
    # escalation (marks the task done/dropped), it stops counting — "the PM's re-plan
    # did not unstick anything" is exactly an escalation that stays open.
    broken = sum(
        1 for t in ledger.list_tasks()
        if getattr(t, "role", "") == "pm"
        and str(getattr(t, "title", "") or "").startswith("revise chain broken:")
        and getattr(t, "state", "") not in ("done", "dropped"))
    if broken <= 0:
        c.last_broken_count = 0
        c.last_broken_iter = c.iterations
        return None
    merges = sum(1 for p in ledger.list_prs() if p.get("status") == "merged")
    # A change in broken count (a new break) OR a merge (progress anywhere) is
    # motion — reset the window.
    if broken != c.last_broken_count or merges != c.last_broken_merges:
        c.last_broken_count = broken
        c.last_broken_merges = merges
        c.last_broken_iter = c.iterations
        return None
    stalled = c.iterations - c.last_broken_iter
    if stalled < policy.revise_livelock_limit:
        return None
    evidence = DetectorEvidence(
        detector="revise_livelock",
        text=(f"{broken} revise lineage(s) broke and the run has made no merge "
              f"progress for {policy.revise_livelock_limit} iterations"),
        value=stalled, threshold=policy.revise_livelock_limit)
    _maybe_raise_monitor(ledger, "revise_livelock", evidence)
    # SPEC-27 Item 3 — ladder `ESCALATE -> STOP`, and NOTHING ELSE: GL04's clamp is
    # ALREADY the rung below this stop in both chains (wired immediately before this
    # detector), so re-implementing a narrowing here would be a second clamp with a
    # second release path — the flap the hysteretic bands exist to prevent.
    return _trip(ledger, c, policy, REVISE_LIVELOCK, evidence)


def _open_backlog_shape(ledger: Any) -> tuple[int, int]:
    """``(open_task_count, distinct_open_title_count)`` over the backlog.

    The PAIR is the diagnosis: 130 open tasks across 35 distinct titles says
    "the PM is restating the same handful of jobs", which a depth alone does not.
    Best-effort — a ledger hiccup must never break the run loop."""
    try:
        tasks = list(ledger.list_tasks())
    except Exception:  # noqa: BLE001 - diagnostics must never break the loop
        return (0, 0)
    open_tasks = [
        t for t in tasks
        if str(getattr(t, "state", "") or "") in task_dedupe.OPEN_STATES
    ]
    # Normalized (filler-verb-stripped) token sets, so "Fix the harness" and
    # "Create the harness" collapse to ONE title — that is what makes a 130-vs-35
    # split legible as restatement rather than as 130 distinct jobs.
    titles = {
        task_dedupe.normalized_tokens(str(getattr(t, "title", "") or ""))
        for t in open_tasks
    }
    return (len(open_tasks), len(titles))


def _account_planning_churn(ledger: Any, c: LoopCounters,
                            policy: CodingAutonomyPolicy) -> DetectorOutcome:
    """Spec 07: detect a run that has degenerated into PM-ONLY planning — N
    consecutive plan turns with ZERO interleaved worker turns — and stop
    `planning_churn`.

    This case is invisible to both existing convergence detectors by construction:
    `_account_convergence` treats a newly-created task as motion BY DESIGN (see the
    caveat in ``_progress_fingerprint``), and `_account_gate_stall` needs an
    acceptance-gate signal, which only worker turns produce. Both structurally
    assume workers are running; this one covers the case where they are not.

    ``c.plan_streak`` is maintained in ``_apply_outcome`` — incremented ONLY on a
    PM `planned` turn, reset by every branch a worker turn reaches and by
    `governance_progress` (FIX 1) — so a legitimate up-front decomposition burst
    that actually dispatches work, and a governance design phase, never trip.
    ``plan_streak_limit == 0`` disables the detector."""
    if policy.plan_streak_limit <= 0:
        return None
    if c.plan_streak < policy.plan_streak_limit:
        return None
    depth, distinct = _open_backlog_shape(ledger)
    evidence = DetectorEvidence(
        detector="planning_churn",
        text=(f"{c.plan_streak} consecutive planning turns with no worker turn "
              f"(backlog {depth} open task(s) across {distinct} distinct title(s))"),
        value=c.plan_streak, threshold=policy.plan_streak_limit)
    _maybe_raise_monitor(ledger, "planning_churn", evidence)
    # SPEC-27 Item 3 — ladder `CLAMP_PLANNING -> ESCALATE -> STOP`. The detector's
    # own diagnosis is "PM plan turns with zero interleaved worker turns", so
    # making the next turn a worker turn IS the remedy, and it costs nothing.
    return _trip(ledger, c, policy, PLANNING_CHURN, evidence)


def _dispatch_wedge_culprits(ledger: Any, todo: list[Task]) -> str:
    """Spec 10 — NAME the culprit: walk the ``depends_on`` closure of the todo set
    and report which dep ids sit in a non-satisfiable state and how many todo tasks
    each (transitively) blocks.

    A dep is a CULPRIT when it can no longer progress a waiter toward dispatch:
    ``blocked`` (needs a human/PM), or a stranded ``doing`` (a task claimed but not
    completing). ``done`` and ``dropped`` are SATISFIED deps (Spec 09) — NOT
    culprits. A dep id present in no task record is reported as ``missing`` (a
    dangling reference that can never satisfy). Best-effort: a diagnostic read must
    never break the run loop."""
    try:
        by_id = {t.task_id: t for t in ledger.list_tasks()}
    except Exception:  # noqa: BLE001 - diagnostics must never break the loop
        by_id = {t.task_id: t for t in todo}

    def _nonsatisfiable(dep_id: str) -> Optional[str]:
        dep = by_id.get(dep_id)
        if dep is None:
            return "missing"
        state = str(getattr(dep, "state", "") or "")
        # done / dropped are satisfied (Spec 09). blocked + stranded doing wedge.
        if state in ("blocked", "doing"):
            return state
        return None

    blocks: dict[str, int] = {}
    states: dict[str, str] = {}
    for t in todo:
        seen: set[str] = set()
        stack = list(getattr(t, "depends_on", []) or [])
        culprits: set[str] = set()
        while stack:
            dep_id = stack.pop()
            if dep_id in seen:
                continue
            seen.add(dep_id)
            bad = _nonsatisfiable(dep_id)
            if bad is not None:
                culprits.add(dep_id)
                states[dep_id] = bad
            dep = by_id.get(dep_id)
            if dep is not None:
                stack.extend(getattr(dep, "depends_on", []) or [])
        for dep_id in culprits:
            blocks[dep_id] = blocks.get(dep_id, 0) + 1

    todo_n = len(todo)
    if not blocks:
        return (
            f"{todo_n} todo task(s) but none dispatchable, and no non-satisfiable "
            "dependency found — the backlog is role-invisible (no worker role can "
            "take any todo task)")
    ranked = sorted(blocks.items(), key=lambda kv: (-kv[1], kv[0]))
    named = "; ".join(
        f"{dep_id} ({states.get(dep_id, '?')}, blocks {n})"
        for dep_id, n in ranked[:5]
    )
    return f"{todo_n} todo task(s), none dispatchable — wedged on: {named}"


def _account_dispatch_wedge(ledger: Any, c: LoopCounters,
                            policy: CodingAutonomyPolicy) -> DetectorOutcome:
    """Spec 10 — detect a WEDGED graph and stop `dispatch_wedged` after naming the
    culprit deps.

    The observed pathology: 130 todo tasks, 6 healthy members, and ZERO worker
    turns for 10+ iterations — because every worker head is blocked behind a
    non-satisfiable dependency, so ``next_task`` returns None for every role and the
    run silently converts to PM plan turns. Every other detector is structurally
    blind to it: `not_converging` treats new tasks as motion, `gate_not_improving`
    needs a gate signal only worker turns produce, and `planning_churn` fires only
    when the PM keeps *planning* (here the backlog is already large and static).

    Trigger (ALL, sustained for ``wedge_stall_limit`` iterations):
    * ``len(list_tasks(state="todo")) >= wedge_min_tasks`` — a real backlog, so a
      legitimately small/empty queue never trips.
    * ``all(next_task(r) is None for r in _WORKER_PRIORITY)`` — nothing dispatchable.

    The sustained window keeps a task blocked on a genuinely in-flight ``doing``
    prerequisite from false-firing. ``wedge_stall_limit == 0`` disables the
    detector. Best-effort read; a ledger hiccup resets the streak, never stops."""
    if policy.wedge_stall_limit <= 0:
        return None
    try:
        todo = ledger.list_tasks(state="todo")
        dispatchable = any(
            ledger.next_task(role) is not None for role in _WORKER_PRIORITY)
    except Exception:  # noqa: BLE001 - detector must never break the run loop
        c.wedge_streak = 0
        return None
    if len(todo) < policy.wedge_min_tasks or dispatchable:
        c.wedge_streak = 0
        return None
    c.wedge_streak += 1
    if c.wedge_streak < policy.wedge_stall_limit:
        return None
    summary = _dispatch_wedge_culprits(ledger, todo)
    evidence = DetectorEvidence(
        detector="dispatch_wedged", text=summary,
        value=c.wedge_streak, threshold=policy.wedge_stall_limit)
    _maybe_raise_monitor(ledger, "dispatch_wedged", evidence)
    # SPEC-27 Item 3 — ladder `ESCALATE -> STOP`, with NO narrowing rung BY
    # CONSTRUCTION: narrowing a graph with nothing dispatchable makes it strictly
    # worse. The escalation carries `_dispatch_wedge_culprits`, which already names
    # the blocking dep ids and how many todo tasks each transitively blocks.
    return _trip(ledger, c, policy, DISPATCH_WEDGED, evidence, summary=summary)


# --- SPEC-24 (Items 1-3) — the published detector snapshot ------------------ #
#
# THE GAP: every reading above dies in its own stack frame. Each `_account_*`
# computes a value, compares it to a threshold, and has exactly ONE export path —
# the evidence string handed to `_maybe_raise_monitor`, which writes an attention
# signal, i.e. a HUMAN surface no prompt builder ever reads. So the PM, whose next
# plan turn is the only thing that could have changed the outcome, is never told
# that a detector is 6 iterations into an 8-iteration window.
#
# THE SEAM: the state the PM needs is SPLIT. Half of it (`pm_idle`, `plan_streak`,
# `wedge_streak`, the `last_*_iter` marks) is per-process window state that exists
# ONLY in `LoopCounters`; half is ledger-derived. The prompt is composed in
# `runner.py`, which has the ledger and does NOT have `LoopCounters`. So the loop
# publishes a compact, immutable snapshot into `run_state["detector_state"]` at the
# quiescent point, and `runner` reads it back from the store it already holds.
#
# WHY NOT thread `LoopCounters` through `build_run_turn` — three reasons, any one
# sufficient. (1) `RunTurn` is typed `Callable[[Any, Any], TurnOutcome]` and the
# factory has ~50 direct test callers. (2) It would not work: `build_run_turn` is
# invoked once at run start, BEFORE `run_coding_loop` creates or receives the
# counters, so the closure could only capture a mutable reference. (3) That
# reference would then be read from pool worker threads while the main thread
# mutates it in the apply phase — the exact hazard the per-turn capture scratch was
# made thread-local to avoid. A snapshot written at a quiescent point cannot race.
#
# WHY NOT recompute in `runner` at prompt time — half the readings have no ledger
# representation at all, so a second, partial implementation of every threshold
# would sit next to the first (the SPEC-19 "four declarations, two values" shape),
# and the prompt would eventually say 4-of-8 while the detector says 6-of-8.

# The rows the snapshot carries, in the FIXED order they render in (Item 2's
# table). Order is part of the contract: the block must be stable under re-render
# for unchanged inputs.
_SNAPSHOT_DETECTORS = (
    NO_PROGRESS,
    NOT_CONVERGING,
    GATE_NOT_IMPROVING,
    PLANNING_CHURN,
    DISPATCH_WEDGED,
    REVISE_LIVELOCK,
    DELIVERY_REVIEW_STALLED,
    WORKER_UNPRODUCTIVE,
    COMPLETION_BLOCKED,
    MEMBER_UNHEALTHY,
)

# Deliberately NOT rendered, recorded so a reader sees a DECISION rather than an
# oversight — and so the drift canary (`test_spec24_governance_visibility.py`) can
# assert that every stop reason is either published or explicitly excluded. A
# detector added later without a decision fails the build; invisibility is exactly
# how this gap happened the first time.
_SNAPSHOT_NOT_RENDERED = {
    DEFINITION_OF_DONE: "the DONE outcome — not a window and not an approach",
    NO_ACTIONABLE_WORK: (
        "an event from `decide_next` returning `Complete`, not a detector window; "
        "SPEC-27 owns it"),
    CANCELLED: "a human said stop — there is no countdown to observe",
    CHECKPOINT: "the operator's own cadence knob, resumable via `continue`",
    HARD_BLOCKER: "a member declared it in a turn — an event, not an approach",
    BUDGET_EXHAUSTED: (
        "rendered as the always-present budget line rather than as a near row, so "
        "iteration/model-call spend is visible whenever the block is"),
}


def _snapshot_row(detector: str, label: str, current: Any, threshold: Any,
                  unit: str, reading: str, ratio: float,
                  detail: Optional[Callable[[], str]] = None,
                  ) -> Optional[dict[str, Any]]:
    """One row, or ``None`` when the reading is not near its window.

    ``detail`` is a CALLABLE and is invoked only once the cheap counter reading has
    already passed the proximity test — that is the cost control: every counter
    field is free (already in memory), and the ledger-derived enrichments
    (`_dispatch_wedge_culprits` in particular, which walks the whole `depends_on`
    closure) are computed only for readings that are actually going to be rendered.
    A quiet run therefore adds no backlog or dependency reads at all."""
    if not _detector_state.is_near(current, threshold, ratio):
        return None
    extra = ""
    if detail is not None:
        try:
            extra = str(detail() or "")
        except Exception:  # noqa: BLE001 — an enrichment hiccup loses the detail only
            extra = ""
    return {"detector": detector, "label": label, "current": int(current),
            "threshold": int(threshold), "unit": unit, "reading": reading,
            "detail": extra}


def _detector_snapshot(ledger: Any, c: LoopCounters,
                       policy: CodingAutonomyPolicy) -> Optional[dict[str, Any]]:
    """Build the snapshot, or ``None`` when there is nothing to say.

    THE ABSENCE RULE (Item 3): ``None`` — and therefore no `run_state` key, no
    prompt segment, and a byte-identical PM prompt — when ALL of: no reading is
    near, the GL04 clamp is not engaged, and there are no open attention signals.
    This is verbatim the contract Spec 12 established for `gate_output`, and it is
    why a healthy run's prompt does not change by one byte.

    A DISABLED detector (`gate_stall_limit=0`, `wedge_stall_limit=0`, … — all this
    module's "0 disables" knobs) is never rendered: telling a PM about a window
    that cannot fire is pure noise. `trigger()` returns 0 for those.
    """
    ratio = float(getattr(policy, "governance_proximity", 0.0) or 0.0)
    if ratio <= 0:
        return None  # the kill switch: today's prompt bytes, near or not

    rows: list[Optional[dict[str, Any]]] = []
    rows.append(_snapshot_row(
        NO_PROGRESS, "PM progress", c.pm_idle, policy.pm_idle_limit, "iterations",
        f"{c.pm_idle} consecutive PM turn(s) recorded no progress", ratio))
    rows.append(_snapshot_row(
        NOT_CONVERGING, "run motion", c.iterations - c.last_progress_iter,
        max(1, policy.convergence_stall_limit), "iterations",
        (f"no merged progress, PR transition, or ladder activity for "
         f"{c.iterations - c.last_progress_iter} iteration(s)"), ratio))
    if c.last_gate_best >= 0:
        # A score < 0 is the no-signal sentinel: `_account_gate_stall` never trips
        # on it, so there is no window to report.
        rows.append(_snapshot_row(
            GATE_NOT_IMPROVING, "acceptance gate",
            c.iterations - c.last_gate_iter, policy.gate_stall_limit, "iterations",
            (f"score {c.last_gate_best}, unchanged for "
             f"{c.iterations - c.last_gate_iter} iteration(s)"), ratio,
            detail=lambda: ("The gate currently has failing commands."
                            if _gate_has_failure(ledger)
                            else "The gate currently reports nothing failing.")))
    rows.append(_snapshot_row(
        PLANNING_CHURN, "planning", c.plan_streak, policy.plan_streak_limit,
        "iterations",
        f"{c.plan_streak} consecutive plan turn(s) with no worker turn", ratio,
        detail=lambda: "Backlog: %d open task(s) across %d distinct title(s)."
                       % _open_backlog_shape(ledger)))
    rows.append(_snapshot_row(
        DISPATCH_WEDGED, "dispatch", c.wedge_streak, policy.wedge_stall_limit,
        "iterations",
        (f"{c.wedge_streak} iteration(s) with a todo backlog of at least "
         f"{policy.wedge_min_tasks} and nothing dispatchable"), ratio,
        detail=lambda: _dispatch_wedge_culprits(
            ledger, ledger.list_tasks(state="todo")) + "."))
    if c.last_broken_count > 0:
        rows.append(_snapshot_row(
            REVISE_LIVELOCK, "revise lineages", c.iterations - c.last_broken_iter,
            policy.revise_livelock_limit, "iterations",
            (f"{c.last_broken_count} open broken revise lineage(s) and no merge "
             f"for {c.iterations - c.last_broken_iter} iteration(s)"), ratio))
    rows.append(_snapshot_row(
        DELIVERY_REVIEW_STALLED, "delivery review", c.delivery_review_rounds,
        policy.delivery_review_round_limit, "rounds",
        (f"{c.delivery_review_rounds} consecutive rejection(s) of the integrated "
         f"head"), ratio))
    worst_unproductive = max(c.unproductive_counts.values(), default=0)
    rows.append(_snapshot_row(
        WORKER_UNPRODUCTIVE, "worker productivity", worst_unproductive,
        policy.worker_unproductive_limit, "turns",
        f"{worst_unproductive} consecutive unusable turn(s) on one task", ratio,
        detail=lambda: (
            f"Ladder spent: {c.task_reassignments} of "
            f"{policy.task_reassignment_limit} reassignment(s), "
            f"{c.model_escalations} of {policy.model_escalation_limit} "
            f"escalation(s), {c.pm_assists} of {policy.pm_assist_limit} PM "
            f"assist(s).")))
    rows.append(_snapshot_row(
        COMPLETION_BLOCKED, "completion claims", c.false_done_streak,
        policy.completion_refused_limit, "claims",
        (f"{c.false_done_streak} done-claim(s) refused because open work "
         f"remained"), ratio))
    worst_member = max(c.member_fail_counts.values(), default=0)
    rows.append(_snapshot_row(
        MEMBER_UNHEALTHY, "member health", worst_member,
        policy.member_failure_limit, "calls",
        f"{worst_member} consecutive call failure(s) by one member", ratio))
    near = [r for r in rows if r is not None]

    # GL04's clamp is the sharpest case in the whole spec: it does not stop the
    # run, it CHANGES HOW THE RUN BEHAVES (forced serial integration), and nothing
    # in the PM's prompt said so — a PM planning a wide batch into a clamped run is
    # planning against a machine it cannot see. It renders whenever it is ENGAGED
    # (a state, not a countdown) or when the superseded ratio has reached
    # `governance_proximity * convergence_clamp_ratio` over a full window. The
    # merge-rate is reported alongside but never triggers on its own: a
    # "lower is worse" floor does not compose with a fraction-of-threshold rule.
    clamped = False
    clamp_reading = ""
    try:
        clamped = bool(ledger.get_run_state().get("convergence_clamped"))
    except Exception:  # noqa: BLE001
        clamped = False
    stats = _convergence_window_stats(ledger, policy.convergence_window)
    if stats is not None:
        sup, merge_rate, n = stats
        if clamped or sup >= ratio * policy.convergence_clamp_ratio:
            clamp_reading = (
                f"{sup:.0%} of the last {n} resolved PRs were superseded and "
                f"{merge_rate:.0%} merged (the clamp band is "
                f"{policy.convergence_clamp_ratio:.0%} superseded).")

    signals: list[dict[str, Any]] = []
    residual = 0
    try:
        from . import attention
        open_signals = attention.list_open(
            str(getattr(ledger, "project_id", "") or ""), store=ledger)
        residual = max(0, len(open_signals) - _detector_state._SIGNAL_CAP)
        signals = [{"title": str(s.title or ""), "blocking": bool(s.blocking)}
                   for s in open_signals[:_detector_state._SIGNAL_CAP]]
    except Exception:  # noqa: BLE001 — a signal-store hiccup reports no signals
        signals, residual = [], 0

    # The run budget is rendered as a LINE rather than as a `near` row (it is
    # context, and it is free), but it still has to be able to summon the block on
    # its own — Item 3's table puts `budget_exhausted`'s first render at iteration
    # 120 of 200. `max_model_calls=None` has no proximity, so only the iteration
    # cap can trigger.
    budget_near = _detector_state.is_near(
        c.iterations, policy.max_iterations, ratio) or (
        policy.max_model_calls is not None
        and _detector_state.is_near(c.model_calls, policy.max_model_calls, ratio))

    if not near and not clamped and not clamp_reading and not signals \
            and not budget_near:
        return None

    budget_left = (policy.max_model_calls is None
                   or c.model_calls < policy.max_model_calls)
    return {
        "iteration": c.iterations,
        "model_calls": c.model_calls,
        "near": near,
        "clamped": clamped,
        "clamp_reading": clamp_reading,
        "budget": {"iterations": c.iterations,
                   "max_iterations": policy.max_iterations,
                   "model_calls": c.model_calls,
                   "max_model_calls": policy.max_model_calls},
        "signals": signals,
        "signals_residual": residual,
        # Item 4, rule 4: the closing line must name the mechanism that is actually
        # live. With SPEC-23's budget spent (or the knob at 0) the PM is NOT going
        # to be consulted, and saying otherwise is worse than the truth.
        "last_word_available": bool(
            policy.last_word_limit > 0
            and c.last_words < policy.last_word_limit
            and c.iterations < policy.max_iterations
            and budget_left),
    }


def publish_detector_state(ledger: Any, c: LoopCounters,
                           policy: CodingAutonomyPolicy) -> None:
    """SPEC-24 Item 1 — publish this iteration's readings for the PM to read.

    Called from BOTH loop chains at the quiescent point immediately after the
    detector chain, for the reason the tree already states at the concurrent
    loop's own detector block: *a hook in only one chain is dead code exactly
    where it is needed* — Spec 13 lifts the foundation clamp and real fanned-out
    runs go concurrent.

    Best-effort in every direction: a ledger failure inside leaves the run
    completely unaffected (the previous snapshot survives, and the iteration it
    states makes the staleness visible). Publishes ``None`` — i.e. clears the key —
    whenever nothing is near a threshold, which is what keeps a healthy run's PM
    prompt byte-identical to today's.
    """
    try:
        _detector_state.write(ledger, _detector_snapshot(ledger, c, policy))
    except Exception:  # noqa: BLE001 — telemetry must never break the run loop
        pass


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


# Spec 20: the per-task context-request budget is a ladder counter, so the two
# F127 rungs that ROUTE AROUND a failure — model escalation (same member, strictly
# stronger route) and member exclusion + reassignment (a different member) — must
# re-arm it, exactly as `attention.resolve_stale_worker_unproductive` already does
# for the run-start rescue. Those rungs exist to give a fresh or stronger route a
# fair attempt; leaving the counter saturated hands it a task whose context channel
# is already spent, so its FIRST well-formed turn scores `context_request_exhausted`
# and it never gets the channel it may actually need. The remembered question goes
# with it, so the verbatim-repeat guard cannot fire against an ask made by the
# PREVIOUS (failed) route. Still bounded, and terminal: each rung is itself capped
# (`model_escalation_limit` / `task_reassignment_limit`), so the worst case is
# _CONTEXT_REQUEST_LIMIT asks per rung, not an unbounded re-arm. Deliberately NOT
# applied to the pm-assist or terminal rungs in `common_patch`: neither installs a
# new dev route (PM assist re-scopes into fresh tasks, which get fresh budgets of
# their own), so re-arming there would only re-open the loop this bounded.
_CONTEXT_BUDGET_REARM = {
    "context_request_attempts": 0,
    "last_context_question_key": "",
}


def _account_blocked_turn(c: LoopCounters, policy: CodingAutonomyPolicy,
                          action: Any, outcome: TurnOutcome) -> None:
    """Spec 25 (Item 1) — bound the escape shape.

    A worker's `blocked` turn is legal, is progress-bearing (it resets `pm_idle`
    and `plan_streak` via `_apply_outcome`), and is deliberately NOT
    `unproductive` — that is the behavioural change the spec exists for. Which
    means, just as deliberately, it needs its own bound: without one, "blocked"
    is a turn an agent could emit forever while the run reports motion.

    The bound is per ``(member_id, task_id)`` and counts across re-opens (the
    task returns to the backlog when the PM acts on it, so the same member
    blocking the same task again is exactly the pathology). At
    ``blocked_turn_limit`` the turn is additionally marked ``unproductive``, which
    hands it to the EXISTING F127 recovery ladder — escalate the model, reassign
    the task, PM-assist — rather than inventing a second mechanism or a new stop
    reason. ``blocked_turn_limit=0`` disables the accounting entirely (and, since
    nothing else reads these counters, restores the pre-Spec-25 trace for a run
    whose agents never block).

    Mutates ``outcome`` in place: both loops call this BEFORE their
    ``outcome.unproductive`` branch, so the flag is read in the same iteration."""
    if outcome.kind != "task_blocked":
        return
    limit = max(0, int(policy.blocked_turn_limit))
    if limit <= 0:
        return
    member_id = str(getattr(action, "member_id", "") or outcome.member_id or "")
    task_id = str(getattr(action, "task_id", "") or "")
    if not (member_id and task_id):
        return
    key = (member_id, task_id)
    c.blocked_counts[key] = c.blocked_counts.get(key, 0) + 1
    if c.blocked_counts[key] >= limit:
        outcome.unproductive = True
        outcome.reason = f"blocked_turn_limit: {outcome.reason}"[:400]


def _handle_unproductive(
    ledger: Any, action: Any, outcome: TurnOutcome, c: LoopCounters,
    policy: CodingAutonomyPolicy, members: list[tuple[str, str]],
) -> Optional[str]:
    """F127 escalate-up ladder. Count an UNPRODUCTIVE worker turn for
    ``(member_id, task_id)``; at ``worker_unproductive_limit`` exclude that member
    from the task and reassign (the scheduler then prefers a higher tier). When
    same-role recovery is exhausted, schedule the bounded PM-assist rung; return
    ``WORKER_UNPRODUCTIVE`` only when no PM exists or the ladder itself fails.
    Never raises into the loop.

    SPEC-23 (Item 1 Δ note / Item 5): the two terminal returns mean DIFFERENT
    things and must stay distinguishable. The ladder-exhausted return below is a
    strategy problem — every rung was tried and the task is genuinely unexecutable,
    exactly what the PM should re-route, so it is HEURISTIC and gets a last word.
    The ``except`` arm is an ENGINE FAULT: the escalation code itself threw. Asking
    a PM to propose a strategy for an engine bug is noise, so it returns
    :data:`ENGINE_FAULT_UNPRODUCTIVE`, which the call sites map to the same
    ``worker_unproductive`` stop reason (no new reason, no exit-code change)
    carrying ``detail={"engine_fault": True}`` — and `_intervene` treats that as
    HARD."""
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
                # Spec 20: a strictly stronger route is a new attempt — re-arm the
                # context-request budget so it is not born already exhausted.
                #
                # Δ review: but NOT when the budget is what it just burned. A
                # stronger model asking the same corpus the same question gets the
                # same answers, so re-arming here multiplied the cap by the rung
                # count (~25 dev calls of pure asking on one task — the same order
                # as the unbounded pathology this bounds). Reassignment still
                # re-arms below: that is a genuinely different member, which may
                # phrase the ask differently. Escalation is the same member.
                **({} if outcome.reason == "context_request_exhausted"
                   else _CONTEXT_BUDGET_REARM),
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
                # Spec 20: a different member is a new attempt — re-arm the
                # context-request budget so it is not born already exhausted.
                **_CONTEXT_BUDGET_REARM,
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
        # SPEC-23: an engine fault, NOT a run condition — stop visibly and hard.
        return ENGINE_FAULT_UNPRODUCTIVE


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


# --- SPEC-23: the last word ------------------------------------------------- #
#
# THE WORST-CASE COST, stated numerically because the constraint governing this
# whole batch is "do not reintroduce the 2026-07-24 forever-loop":
#
#   * at most ``policy.last_word_limit`` interventions per run (default 2),
#   * each costing EXACTLY ONE PM turn -> 1 iteration + 1 model call,
#   * => <= 2 extra iterations and <= 2 extra model calls against a default
#     ``max_iterations`` of 200 (1%), and the budget survives `errorta continue`
#     (``c.last_words`` is persisted), so N continues do not buy N*limit turns.
#
# Both costs are counted through the ORDINARY counters, so ``budget_exhausted``
# (HARD) still dominates and can never be deferred by an intervention. Three
# independent bounds make the feature acyclic:
#
#   1. the run budget above;
#   2. same-detector-once-without-progress — a second intervention for the same
#      detector is refused BEFORE dispatch unless a PR merged in between, so a
#      detector that keeps re-tripping costs ONE turn, not one per trip;
#   3. non-recursion — the intervention turn is excluded from detector accounting
#      (`_apply_outcome`, plus the counter snapshot/restore below), so an
#      intervention can never manufacture the condition for another intervention.
#
# `test_spec23_continue_by_default.py::test_worst_case_intervention_cost` asserts
# the arithmetic; the other locks there assert each bound.

_LAST_WORD_ACCEPTED = "accepted"
_LAST_WORD_DONE = "done"
_LAST_WORD_CONFIRMED = "confirmed"
_LAST_WORD_UNPARSED = "unparsed"

# outcome -> the decision `choice` recorded for it (Item 6). "done" rides
# `last_word_accepted`: the PM proposed something the engine can act on, and the
# F128 completion gate — not the last word — judges whether it sticks.
_LAST_WORD_CHOICE = {
    _LAST_WORD_ACCEPTED: "last_word_accepted",
    _LAST_WORD_DONE: "last_word_accepted",
    _LAST_WORD_CONFIRMED: "last_word_confirmed",
    _LAST_WORD_UNPARSED: "last_word_unparsed",
}


def _delivery_review_evidence(c: LoopCounters,
                              policy: CodingAutonomyPolicy) -> DetectorEvidence:
    """F155's stop has no `_account_*` producer — it is counted inline in both
    loops — so its evidence is built here rather than re-derived inside
    `_intervene`. No monitor Problem is raised (there never was one for this
    reason); this is the prompt-facing value only."""
    return DetectorEvidence(
        detector=DELIVERY_REVIEW_STALLED,
        text=("the delivery review rejected the integrated head "
              f"{c.delivery_review_rounds} time(s) in a row"),
        value=c.delivery_review_rounds,
        threshold=policy.delivery_review_round_limit)


def _merged_pr_count(ledger: Any) -> int:
    """Merged PRs so far — the progress signal bound 2 keys on. Deliberately the
    SAME one `_account_revise_livelock` already trusts ("any merge anywhere is
    progress"), so this introduces no new notion of progress. Guarded: an
    unreadable ledger reports 0, which makes the bound STRICTER (no second
    intervention), never looser."""
    try:
        return sum(1 for p in ledger.list_prs() if p.get("status") == "merged")
    except Exception:  # noqa: BLE001 — a read hiccup must never break the loop
        return 0


def _last_word_evidence(stop: LoopResult) -> str:
    """The detector's own evidence sentence for the intervention prompt.

    Reads the value P0.3 made the detectors carry (`_stop_with_evidence`) rather
    than re-deriving the numbers here. Falls back to the bare detector id for the
    stops that have no `DetectorEvidence` (they are named in the loop instead)."""
    detail = stop.detail or {}
    ev = detail.get("evidence")
    if not isinstance(ev, dict):
        return str(detail.get("summary") or stop.stop_reason)
    text = str(ev.get("text") or stop.stop_reason)
    value, threshold = ev.get("value"), ev.get("threshold")
    if value is not None and threshold is not None:
        text = f"{text} [reading {value} against a threshold of {threshold}]"
    return text


def _reset_detector_window(c: LoopCounters, detector: str) -> None:
    """SPEC-23 Item 2's RESET MAP — enumerated, never inferred.

    Resetting the wrong counter is precisely how this becomes a silent
    forever-loop, so each detector resets exactly the field that bounds ITS window
    and nothing else. Note `gate_not_improving` resets the window anchor and NOT
    ``last_gate_best``: the best score observed is a FACT, not a window, and
    clearing it would let a run re-earn the same score forever."""
    if detector == NO_PROGRESS:
        c.pm_idle = 0
    elif detector == NOT_CONVERGING:
        c.last_progress_iter = c.iterations
    elif detector == GATE_NOT_IMPROVING:
        c.last_gate_iter = c.iterations
    elif detector == PLANNING_CHURN:
        c.plan_streak = 0
    elif detector == DISPATCH_WEDGED:
        c.wedge_streak = 0
    elif detector == REVISE_LIVELOCK:
        c.last_broken_iter = c.iterations
    elif detector == DELIVERY_REVIEW_STALLED:
        c.delivery_review_rounds = 0
    # WORKER_UNPRODUCTIVE: nothing to reset — the F127 ladder already zeroed
    # `c.unproductive_counts[key]`, and the PM's replacements are new tasks with
    # fresh budgets of their own.


def _record_last_word(ledger: Any, *, title: str, detector: str, choice: str,
                      rationale: str, related: Optional[list[str]] = None,
                      extra: Optional[dict[str, Any]] = None) -> None:
    """One ledger decision, best-effort. An intervention that leaves no trace
    repeats the very mistake SPEC-22 went first to fix, but a signal-store hiccup
    must still never break the run loop."""
    try:
        ledger.record_decision(
            title=title, context=f"last_word:{detector}", choice=choice,
            rationale=rationale[:2000], related_task_ids=related or [],
            extra=extra or {})
    except Exception:  # noqa: BLE001 — recording must never break the run loop
        pass


def _publish_last_words(ledger: Any, c: LoopCounters, *, detector: str,
                        outcome: str, rationale: str) -> None:
    """Item 6 — the run-state snapshot the CLI's second summary line reads.

    Distinct from ``counters.last_words`` (the budget, persisted at the terminal
    writers): this carries the OUTCOME, so an operator can tell "the PM agreed"
    from "we could not hear the PM" — a distinction the Spec 21 post-mortem shows
    is load-bearing."""
    try:
        ledger.set_run_state(last_words={
            "count": int(c.last_words),
            "detector": detector,
            "outcome": outcome,
            "rationale": rationale[:2000],
        })
    except Exception:  # noqa: BLE001 — a run-state hiccup must never break the loop
        pass


def _intervene(
    ledger: Any,
    members: list[tuple[str, str]],
    policy: CodingAutonomyPolicy,
    c: LoopCounters,
    stop: Optional[LoopResult],
    *,
    run_turn: RunTurn,
    should_cancel: Optional[Callable[[], bool]] = None,
    rec: Optional[CodingReconciler] = None,
    delivery_review: Optional[Callable[[Any], Any]] = None,
) -> Optional[LoopResult]:
    """Return ``None`` to CONTINUE the run (the PM proposed something actionable
    and that detector's window was reset), or the :class:`LoopResult` to return
    as-is.

    Called from every site in BOTH loop chains where a heuristic stop would have
    been returned. A HARD stop takes the first early return below and is byte-for-
    byte unchanged — that single early return is what the hard-stop regression lock
    asserts, parametrized over every member of :data:`HARD_STOP_REASONS`.

    The run still ends with the reason the detector named: this does not make
    stops advisory, it makes them *reviewable* by the one component placed to
    choose a different strategy."""
    if stop is None:
        return None
    # --- the hard-stop path: ONE early return, no model call, no ledger write --
    if stop.stop_reason not in _INTERVENABLE_STOP_REASONS:
        return stop
    detail = stop.detail or {}
    if detail.get("engine_fault"):
        return stop  # the escalation code threw — an engine bug, not a strategy
    # 0 disables the whole mechanism and restores today's trace exactly (the
    # batch's escape hatch, and this module's `0 disables` convention).
    if policy.last_word_limit <= 0:
        return stop
    if c.last_words >= policy.last_word_limit:
        return stop
    detector = stop.stop_reason
    merges = _merged_pr_count(ledger)
    prior = c.last_word_by_detector.get(detector)
    if prior is not None and merges <= prior[1]:
        # Bound 2: this detector already had its turn and nothing merged since, so
        # the PM's proposal did not unstick it. Refused BEFORE the model call —
        # "it stopped" must not be true of an implementation that dispatched a turn
        # and ignored the answer.
        return stop
    # A cancel must never wait on an intervention, and the budget must never be
    # overspent by one: both are re-checked immediately before the model call.
    if should_cancel is not None and should_cancel():
        return stop
    if c.iterations >= policy.max_iterations:
        return stop
    if (policy.max_model_calls is not None
            and c.model_calls >= policy.max_model_calls):
        return stop
    pm_ids = [mid for mid, role in members if role == PM]
    if not pm_ids:
        return stop  # nobody to ask (mirrors the F127 ladder's own PM guard)

    evidence = _last_word_evidence(stop)
    action = LastWord(member_id=pm_ids[0], detector=detector, evidence=evidence)
    c.last_words += 1
    c.last_word_by_detector[detector] = (c.iterations, merges)
    _record_last_word(
        ledger, title=f"last word requested: {detector}", detector=detector,
        choice="last_word_requested", rationale=evidence,
        extra={"detector": detector,
               "threshold": (detail.get("evidence") or {}).get("threshold"),
               "window_iters": (detail.get("evidence") or {}).get("value"),
               "intervention_index": c.last_words, "merged_prs": merges})

    # Bound 3 (non-recursion), belt: snapshot every counter a synthetic turn the
    # HARNESS initiated could otherwise feed. `_apply_outcome` already excludes a
    # LastWord planning turn; this also covers the done-claim path below, where the
    # ordinary completion machinery runs and would touch `pm_idle`.
    snap = (c.pm_idle, c.plan_streak, c.schema_rejects, c.false_done_streak,
            dict(c.unproductive_counts))
    outcome = _safe_run_turn(run_turn, action, ledger, 1)
    c.iterations += 1
    c.model_calls += max(0, int(outcome.model_calls))
    c.turns_repaired += max(0, int(outcome.repairs))
    (c.pm_idle, c.plan_streak, c.schema_rejects, c.false_done_streak) = snap[:4]
    c.unproductive_counts = snap[4]

    verdict = outcome.last_word or {}
    kind = str(verdict.get("outcome") or _LAST_WORD_UNPARSED)
    if kind not in _LAST_WORD_CHOICE:
        kind = _LAST_WORD_UNPARSED
    if _crashed(outcome):
        # The turn itself blew up. Treated as NO ANSWER, never as agreement — see
        # the unparsed branch below.
        kind = _LAST_WORD_UNPARSED
    rationale = str(verdict.get("rationale") or "")
    task_ids = [str(t) for t in (verdict.get("task_ids") or [])]

    if kind in (_LAST_WORD_ACCEPTED, _LAST_WORD_DONE):
        _reset_detector_window(c, detector)
        _record_last_word(
            ledger, title=f"last word accepted: {detector}", detector=detector,
            choice=_LAST_WORD_CHOICE[kind],
            rationale=rationale or "the PM proposed a concrete next action",
            related=task_ids,
            extra={"detector": detector, "claimed_done": kind == _LAST_WORD_DONE,
                   "intervention_index": c.last_words})
        _publish_last_words(ledger, c, detector=detector, outcome=kind,
                            rationale=rationale)
        if kind == _LAST_WORD_DONE and rec is not None:
            # Routed to the NORMAL done path: the F128 completion gate judges this
            # claim exactly as it judges any other, and `decide_next` returns
            # `Complete(definition_of_done)` next iteration if it sticks. The last
            # word gets no special authority to declare victory.
            _apply_outcome(rec, ledger, action, outcome, c, delivery_review, policy)
            (c.pm_idle, c.plan_streak, c.schema_rejects, c.false_done_streak) = snap[:4]
        return None  # CONTINUE

    # --- the run stops, with the ORIGINAL stop_reason, byte-identical --------- #
    if kind == _LAST_WORD_UNPARSED:
        # THE SPEC 21 LESSON, in its purest form: an unheard PM is NOT a
        # consenting one. Three of four PM retries in the 2026-07-26 run died on a
        # schema rejection while the model kept re-emitting a reasonable shape, and
        # the harness read repeated rejection as agreement-by-silence. Both the
        # decision and the stop summary must say the PM was not heard.
        note = rationale or "the last-word turn could not be parsed"
        _record_last_word(
            ledger, title=f"last word not heard: {detector}", detector=detector,
            choice="last_word_unparsed",
            rationale=(f"the PM was asked to propose a next action or confirm the "
                       f"halt, and its turn could not be read ({note}); stopping "
                       f"with the original reason {detector} — this is NOT a "
                       f"confirmation"),
            extra={"detector": detector, "intervention_index": c.last_words})
    else:
        _record_last_word(
            ledger, title=f"last word confirmed: {detector}", detector=detector,
            choice="last_word_confirmed",
            rationale=(rationale or "the PM proposed nothing the engine could act "
                       "on; the halt stands"),
            extra={"detector": detector, "intervention_index": c.last_words})
    _publish_last_words(ledger, c, detector=detector, outcome=kind,
                        rationale=rationale)
    return LoopResult(stop.stop_reason, c, detail={
        **detail,
        "last_word": {"detector": detector, "outcome": kind,
                      "pm_rationale": rationale},
    })


def _unproductive_result(sentinel: str, c: LoopCounters, action: Any) -> LoopResult:
    """Map `_handle_unproductive`'s return to a `LoopResult`, preserving the
    ladder-exhausted / engine-fault distinction (SPEC-23 Item 5). The stop REASON
    is `worker_unproductive` either way — only the detail differs, so no CLI set
    and no exit code moves."""
    detail: dict[str, Any] = {"task_id": getattr(action, "task_id", "")}
    if sentinel == ENGINE_FAULT_UNPRODUCTIVE:
        detail["engine_fault"] = True
    return LoopResult(WORKER_UNPRODUCTIVE, c, detail=detail)


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
    pool_members: Optional[list[tuple[str, str]]] = None,
) -> LoopResult:
    """The original one-action-per-iteration loop (max_parallel_workers <= 1)."""

    def _last_word(stop: "DetectorOutcome | LoopResult") -> Optional[LoopResult]:
        """SPEC-23 + SPEC-27 — route a detector outcome through the ONE apply
        point. ``None`` back means the run CONTINUES (nothing fired, a `Narrow`
        was engaged, or the PM proposed something actionable); anything else is the
        result to return as-is. Reads the enclosing ``policy``, which
        `policy_provider` may have re-read this iteration.

        A caller answers ``None`` with ``continue`` UNLESS the outcome was a
        `Narrow`, which by contract does not short-circuit the chain (SPEC-27
        Item 1). For everything else `continue` is deliberate and costs nothing:
        each of these sites RETURNS today, so `continue` skips exactly what the
        return already skips — while fall-through would run code the stopping path
        has never reached (e.g. the `pm_assist_exhausted` outcome also carries
        ``hard_blocker``, so falling through would swap `worker_unproductive` for
        `hard_blocker` and break batch regression lock 1)."""
        return _apply_detector_outcome(
            ledger, members, policy, c, stop, run_turn=run_turn,
            should_cancel=should_cancel, rec=rec,
            delivery_review=delivery_review)

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
                member_tiers=member_tiers, delivery_review=delivery_review,
                pool_members=pool_members)
        if should_cancel is not None and should_cancel():
            return LoopResult(CANCELLED, c)

        action = decide_next(ledger, members, member_tiers)
        if isinstance(action, Complete):
            # SPEC-27 Item 3: `no_actionable_work` is SUCCESS-class, so it gets one
            # escalate rung and ONLY when open work remains — a wedge wearing a
            # success label. `definition_of_done` and every other reason fall
            # straight through to today's return.
            na = _no_actionable_escalation(ledger, c, policy, action.reason)
            if na is not None:
                na_stop = _last_word(na)
                if na_stop is not None:
                    return na_stop
                continue
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

        milestone = _apply_outcome(rec, ledger, action, outcome, c, delivery_review,
                                   policy)

        # F155: the delivery review kept rejecting the integrated head. A filed
        # finding counts as progress (resets pm_idle) and changes the head, so
        # no_progress / not_converging never trip — cap the rejected rounds here so
        # a persistently-failing delivery ends truthfully, not at budget_exhausted.
        if c.delivery_review_rounds >= policy.delivery_review_round_limit:
            # SPEC-27 Item 3 — ladder `FORCE_INTEGRATION -> ESCALATE -> STOP`: a
            # delivery review judges the INTEGRATED head, so draining the pending
            # merges changes the thing under review before anyone is asked.
            dr_stop = _last_word(_trip(
                ledger, c, policy, DELIVERY_REVIEW_STALLED,
                _delivery_review_evidence(c, policy)))
            if dr_stop is not None:
                return dr_stop
            continue

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

        # Spec 25: bound the blocked turn — past `blocked_turn_limit` for this
        # (member, task) it is ALSO unproductive, so the ladder below takes it.
        _account_blocked_turn(c, policy, action, outcome)

        # F127: a worker that keeps producing unusable turns gets its task
        # reassigned to a different/stronger member; if every member has failed it,
        # a blocking Problem is raised and the run stops (never a silent no_progress).
        if outcome.unproductive:
            up_stop = _handle_unproductive(ledger, action, outcome, c, policy, members)
            if up_stop is not None:
                # SPEC-23 Item 4: the last word is a new rung 5 on the F127 ladder —
                # the cheap, mechanical, task-scoped rungs run first, and only a
                # genuinely exhausted ladder asks the run-scoped question.
                kept = _last_word(_unproductive_result(up_stop, c, action))
                if kept is not None:
                    return kept
                continue
        else:
            _reset_unproductive_count(c, action, outcome)

        if outcome.kind == "pm_assist_exhausted":
            # The same ladder ending by a different door — hooked identically.
            pa_stop = _last_word(LoopResult(
                WORKER_UNPRODUCTIVE, c,
                detail={"task_id": getattr(action, "task_id", "")}))
            if pa_stop is not None:
                return pa_stop
            continue

        if outcome.hard_blocker:
            _maybe_raise_monitor(ledger, "hard_blocker", DetectorEvidence(
                detector="hard_blocker", text=outcome.reason))
            return LoopResult(HARD_BLOCKER, c, detail={"reason": outcome.reason})

        # PM made no progress N times in a row -> nothing left to do.
        if c.pm_idle >= policy.pm_idle_limit:
            np_evidence = DetectorEvidence(
                detector="no_progress", text="PM made no progress",
                value=c.pm_idle, threshold=policy.pm_idle_limit)
            _maybe_raise_monitor(ledger, "no_progress", np_evidence)
            np_stop = _last_word(
                _trip(ledger, c, policy, NO_PROGRESS, np_evidence))
            if np_stop is not None:
                return np_stop
            continue

        # SPEC-27 Item 6, invariants 2+3: release or force-lift the narrowing flags
        # BEFORE the chain re-reads its windows, so a narrowing whose condition was
        # met never survives into the iteration that judges it.
        _release_narrow_flags(ledger, c, policy)

        # Spec 07: the PM-only pathology — plan turn after plan turn with no worker
        # ever running. Checked beside NO_PROGRESS because it is the same class of
        # stop (the PM is the only thing moving), just the case where each plan turn
        # looks productive so pm_idle never climbs.
        churn_out = _account_planning_churn(ledger, c, policy)
        if churn_out is not None:
            churn_stop = _last_word(churn_out)
            if churn_stop is not None:
                return churn_stop
            # SPEC-27 Item 1: a `Narrow` FALLS THROUGH to the next detector; an
            # `Escalate`/`Stop` the PM accepted short-circuits, exactly as today.
            if not isinstance(churn_out, Narrow):
                continue

        # F139 WS-A/WS-E: surface a stuck foundation, and stop a run where nothing
        # is moving anywhere (distinct from NO_PROGRESS, which is a PM-idle stop).
        # Both are pure-`Narrow` detectors: they apply their own side effect and the
        # run continues, so their outcome is recorded, never returned.
        _last_word(_account_foundation_stall(ledger, c, policy))
        _last_word(_account_hot_file_freeze(ledger, c, policy))
        conv_out = _account_convergence(ledger, c, policy)
        if conv_out is not None:
            conv_stop = _last_word(conv_out)
            if conv_stop is not None:
                return conv_stop
            if not isinstance(conv_out, Narrow):
                continue
        # Spec 04: stop a run whose acceptance gate result keeps repeating without
        # improving (the 6/12-with-a-churning-head loop). Keyed on the gate result,
        # so it catches churn that `not_converging` (progress fingerprint) misses.
        gate_out = _account_gate_stall(ledger, c, policy)
        if gate_out is not None:
            gate_stop = _last_word(gate_out)
            if gate_stop is not None:
                return gate_stop
            if not isinstance(gate_out, Narrow):
                continue
        # GL04 (GAP-5): the run-level convergence brake — SOFT (clamp fan-out to
        # serial, releasable) and wired BEFORE Spec 16's hard livelock stop, so the
        # run gets a chance to drain before the stop lands underneath. Never returns
        # a stop; only sets/clears the clamp flag `runtime_cap` reads.
        _last_word(_account_convergence_clamp(ledger, c, policy))
        # Spec 16: a revise-chain livelock the breaker couldn't unstick.
        livelock_out = _account_revise_livelock(ledger, c, policy)
        if livelock_out is not None:
            livelock_stop = _last_word(livelock_out)
            if livelock_stop is not None:
                return livelock_stop
            if not isinstance(livelock_out, Narrow):
                continue
        # Spec 10: a wedged graph — a large todo backlog with nothing dispatchable —
        # named and stopped instead of silently converted into PM plan turns.
        wedge_out = _account_dispatch_wedge(ledger, c, policy)
        if wedge_out is not None:
            wedge_stop = _last_word(wedge_out)
            if wedge_stop is not None:
                return wedge_stop
            if not isinstance(wedge_out, Narrow):
                continue

        # SPEC-24: the quiescent point — every detector above has just computed its
        # reading against its threshold, so this is where the snapshot the PM's
        # next prompt renders is published. Writes only on change (a quiet run,
        # whose snapshot is `None` iteration after iteration, writes nothing) and
        # never raises.
        publish_detector_state(ledger, c, policy)

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
    pool_members: Optional[list[tuple[str, str]]] = None,
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
    # SPEC-26 (Item 3): size from the PRE-CLOSURE roster when one was supplied, so a
    # role re-seated mid-run cannot push `runtime_cap` above the pool and silently
    # serialize dispatch. The pool is an upper bound only (see the note above), so a
    # wider pool changes nothing for a run with no deferred roles.
    pool = ThreadPoolExecutor(
        max_workers=effective_parallelism(policy, pool_members or members) + 2)
    in_flight: dict[Any, Any] = {}   # future -> action
    busy: set[str] = set()           # member_ids currently running a turn
    model_in_flight = 0              # non-merge turns in flight (cap + budget)
    # SPEC-27 Item 6: this stages an OUTCOME, not only a `LoopResult`. The apply
    # phase may run while other futures are still in flight, so a staged outcome is
    # only APPLIED at a drain point — a `Narrow` that mutated dispatch state
    # mid-batch would change the rules under live futures.
    pending_stop: "DetectorOutcome | LoopResult" = None
    milestone = False

    def _last_word(stop: "DetectorOutcome | LoopResult") -> Optional[LoopResult]:
        """SPEC-23 + SPEC-27 — route a detector outcome through the ONE apply point
        (see the sequential loop's twin). Wiring BOTH chains is the dead-code lock:
        real fanned-out runs live HERE once Spec 13 lifts the foundation clamp, so a
        hook only in the sequential path would never fire where it is needed."""
        return _apply_detector_outcome(
            ledger, members, policy, c, stop, run_turn=run_turn,
            should_cancel=should_cancel, rec=rec,
            delivery_review=delivery_review)

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
                    member_tiers=member_tiers, delivery_review=delivery_review,
                    pool_members=pool_members)

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
                _hot_blocked_by_task = hot_owned_paths_by_task(ledger, _hot)
                # GL05 (Item 2): the strict a-priori file-ownership partition — the
                # paths already held by every in-flight/doing task, so a fan-out can't
                # hand two tasks the same DECLARED file from tick 0 (the reactive
                # hot-file gate only engages after N conflicts). Empty when the flag is
                # off, restoring pre-GL05 dispatch exactly.
                _owned_by_task = (
                    inflight_owned_paths_by_task(ledger)
                    if policy.strict_file_partition else None
                )
                _frozen = frozen_paths(ledger)
                # SPEC-27 Item 2: the narrowing flags the dispatch phase honours.
                # Read ONCE per iteration beside the hot/frozen picture, at the
                # same seam `hot_paths` / `frozen` / `owned_paths` already use.
                # Both are absent->falsy, so a pre-spec run state dispatches
                # byte-identically.
                _narrow = narrow_flags(ledger)
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
                        hot_paths=_hot_paths,
                        hot_blocked_by_task=_hot_blocked_by_task,
                        frozen=_frozen, frozen_owner_task_id=_frozen_owner,
                        owned_by_task=_owned_by_task,
                        integration_only=_narrow["integration_only"],
                        planning_clamped=_narrow["planning_clamped"])
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
                    # Spec 12 (S1): a GateRun is mechanical (0 model calls) like a
                    # Merge, but does NOT mutate master, so it needs no serial-merge
                    # treatment — it runs against the master checkout while workers
                    # write their own worktrees.
                    is_mechanical = isinstance(action, (Merge, GovernanceMaterialize, GateRun))
                    is_merge = isinstance(action, Merge)
                    flight = in_flight.values()
                    if is_merge:
                        # Integration is serial (GL05 Item 3 — asserted invariant,
                        # not new behavior): a Merge mutates master and
                        # revalidates other PRs' worktrees, so it must not run
                        # while worker turns write in parallel. Defer it until the
                        # in-flight workers drain (we stop adding new work below,
                        # so they will), then run the merge alone. A GateRun reads
                        # the master checkout (`workspace.root()`); a Merge mutating
                        # master concurrently would leave the gate testing a mixed
                        # tree and binding its verdict to a head it never cleanly
                        # ran — so defer on an in-flight GateRun too. (The inverse —
                        # a GateRun deferring on an in-flight Merge — is the `else`
                        # branch below, so the two are mutually exclusive.)
                        if any(isinstance(a, (Assign, GateRun)) for a in flight):
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
                            _hot_blocked_by_task = hot_owned_paths_by_task(
                                ledger, _hot)
                        # GL05 (Item 2): the just-assigned task is now `doing`, so it
                        # owns its declared paths a priori; recompute so the next
                        # plan_next_batch call holds any task declaring the same file.
                        if policy.strict_file_partition:
                            _owned_by_task = inflight_owned_paths_by_task(ledger)
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
                    # HOOK SITE 3 of 4 (SPEC-23 Item 5). This is the return point
                    # that is easy to miss: nothing is running and the dispatch
                    # phase was skipped because a stop was already staged, so a
                    # staged `delivery_review_stalled` / `worker_unproductive`
                    # escapes un-intervened if only the drain block below is hooked.
                    kept = _last_word(pending_stop)
                    if kept is not None:
                        return kept
                    # Accepted: clear the staged stop and re-enter dispatch with the
                    # PM's new work. `continue` (not fall-through) is load-bearing —
                    # falling through would reach `decide_next` and return
                    # NO_ACTIONABLE_WORK, i.e. a DIFFERENT stop reason.
                    pending_stop = None
                    continue
                action = decide_next(ledger, members, member_tiers)
                if isinstance(action, Complete):
                    na = _no_actionable_escalation(ledger, c, policy, action.reason)
                    if na is not None:
                        na_stop = _last_word(na)
                        if na_stop is not None:
                            return na_stop
                        continue
                    return LoopResult(action.reason, c)
                if _over_budget():
                    return LoopResult(BUDGET_EXHAUSTED, c)
                # SPEC-27 Item 3: nothing is running, `decide_next` DID name an
                # action, and the dispatch phase placed none of it — a wedge
                # wearing a success label. One escalate rung; a refused escalation
                # returns this exact reason and still exits EXIT_OK.
                na = _no_actionable_escalation(ledger, c, policy, NO_ACTIONABLE_WORK)
                if na is not None:
                    na_stop = _last_word(na)
                    if na_stop is not None:
                        return na_stop
                    continue
                return LoopResult(NO_ACTIONABLE_WORK, c)

            # --- wait for the next turn to finish, apply its outcome --------
            done, _pending = wait(set(in_flight), return_when=FIRST_COMPLETED)
            for fut in done:
                action = in_flight.pop(fut)
                busy.discard(getattr(action, "member_id", None))
                outcome = fut.result()  # _safe_run_turn never raises
                if not isinstance(action, (Merge, GovernanceMaterialize, GateRun)):
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
                    rec, ledger, action, outcome, c, delivery_review,
                    policy) or milestone
                # F155: cap delivery-review reject rounds (mirrors the sequential
                # loop). A filed finding resets pm_idle + changes the head, so
                # no_progress / not_converging never trip — stop truthfully instead
                # of looping to budget_exhausted. Drain-stop like the other caps.
                if (c.delivery_review_rounds >= policy.delivery_review_round_limit
                        and pending_stop is None):
                    # SPEC-27: STAGED as an outcome and applied only at the drain
                    # point below — a `FORCE_INTEGRATION` engaged mid-batch would
                    # change dispatch under live futures.
                    pending_stop = _trip(
                        ledger, c, policy, DELIVERY_REVIEW_STALLED,
                        _delivery_review_evidence(c, policy))
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
                # Spec 25: bound the blocked turn here TOO — regression lock 5 of
                # the batch plan (a hook in only one loop chain is dead code
                # exactly where real fanned-out runs live).
                _account_blocked_turn(c, policy, action, outcome)
                # F127: reassign-up an unproductive worker turn; drain-stop with a
                # blocking Problem only if every member of the role has failed it.
                if outcome.unproductive:
                    up_stop = _handle_unproductive(
                        ledger, action, outcome, c, policy, members)
                    if up_stop is not None and pending_stop is None:
                        # SPEC-23: preserves the ladder-exhausted / engine-fault
                        # split (same stop reason, different detail).
                        pending_stop = _unproductive_result(up_stop, c, action)
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
                    # HOOK SITE 4 of 4 (SPEC-23 Item 5) — the drain point. Placed
                    # BEFORE the member-health / hard-blocker special cases below:
                    # both reasons are HARD, so `_intervene` returns them on its
                    # first line and their handling is byte-for-byte unchanged.
                    kept = _last_word(pending_stop)
                    if kept is None:
                        pending_stop = None
                        continue  # the PM proposed work — keep running
                    pending_stop = kept
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
                            ledger, "hard_blocker", DetectorEvidence(
                                detector="hard_blocker",
                                text=str((pending_stop.detail or {})
                                         .get("reason", "") or "")))
                    return pending_stop
                if c.pm_idle >= policy.pm_idle_limit:
                    np_evidence = DetectorEvidence(
                        detector="no_progress", text="PM made no progress",
                        value=c.pm_idle, threshold=policy.pm_idle_limit)
                    _maybe_raise_monitor(ledger, "no_progress", np_evidence)
                    np_stop = _last_word(
                        _trip(ledger, c, policy, NO_PROGRESS, np_evidence))
                    if np_stop is not None:
                        return np_stop
                    continue
                # SPEC-27 Item 6 — the same release/force-lift pass as the
                # sequential chain, at the IDENTICAL position. This is the loop a
                # wide fanned-out run lives on, so a lift-check in only one chain
                # would leave a narrowing engaged exactly where it bites hardest.
                _release_narrow_flags(ledger, c, policy)
                # Spec 07: PM-only planning churn (mirrors the sequential loop),
                # checked at this same quiescent point.
                churn_out = _account_planning_churn(ledger, c, policy)
                if churn_out is not None:
                    churn_stop = _last_word(churn_out)
                    if churn_stop is not None:
                        return churn_stop
                    if not isinstance(churn_out, Narrow):
                        continue
                # F139 WS-A/WS-E: foundation-stall surfacing + convergence stop,
                # checked at this quiescent (in-flight empty) point so a resume
                # continues cleanly.
                _last_word(_account_foundation_stall(ledger, c, policy))
                _last_word(_account_hot_file_freeze(ledger, c, policy))
                conv_out = _account_convergence(ledger, c, policy)
                if conv_out is not None:
                    conv_stop = _last_word(conv_out)
                    if conv_stop is not None:
                        return conv_stop
                    if not isinstance(conv_out, Narrow):
                        continue
                # Spec 04: gate-repeat stall stop, at this same quiescent point.
                gate_out = _account_gate_stall(ledger, c, policy)
                if gate_out is not None:
                    gate_stop = _last_word(gate_out)
                    if gate_stop is not None:
                        return gate_stop
                    if not isinstance(gate_out, Narrow):
                        continue
                # GL04 (GAP-5): the run-level convergence brake, wired BEFORE Spec
                # 16's livelock in the CONCURRENT loop too — this is the very loop a
                # wide, churning fan-out runs on once Spec 13 lifts the foundation
                # clamp, so the metric would be dead code exactly where it's needed if
                # it lived only in the sequential path. Soft (clamp, never a stop).
                _last_word(_account_convergence_clamp(ledger, c, policy))
                # Spec 16: revise-chain livelock probe (mirrors the sequential loop);
                # wiring BOTH chains is the dead-code lock — Spec 13 lifts the clamp
                # and real runs go concurrent, so a detector only in the sequential
                # path would never fire where it's needed.
                livelock_out = _account_revise_livelock(ledger, c, policy)
                if livelock_out is not None:
                    livelock_stop = _last_word(livelock_out)
                    if livelock_stop is not None:
                        return livelock_stop
                    if not isinstance(livelock_out, Narrow):
                        continue
                # Spec 10: wedged-graph probe (mirrors the sequential loop).
                wedge_out = _account_dispatch_wedge(ledger, c, policy)
                if wedge_out is not None:
                    wedge_stop = _last_word(wedge_out)
                    if wedge_stop is not None:
                        return wedge_stop
                    if not isinstance(wedge_out, Narrow):
                        continue
                # SPEC-24: publish at the IDENTICAL position in this chain too.
                # A publisher in one chain only is dead code exactly where it
                # matters — this is the loop a wide, fanned-out run lives on once
                # Spec 13 lifts the foundation clamp, i.e. the runs whose PM most
                # needs to see the countdown.
                publish_detector_state(ledger, c, policy)
                if _checkpoint_due(policy, c, milestone):
                    c.since_checkpoint = 0
                    return LoopResult(CHECKPOINT, c)
                milestone = False
    finally:
        pool.shutdown(wait=True)


def _apply_outcome(rec: CodingReconciler, ledger: Any, action: Any,
                   outcome: TurnOutcome, c: LoopCounters,
                   delivery_review: Optional[Callable[[Any], Any]] = None,
                   policy: Optional[CodingAutonomyPolicy] = None) -> bool:
    """Apply the reconciler mutation for a turn's outcome. Returns whether this
    turn was a milestone (a fully-validated unit / project completion).

    F146 Slice B: ``delivery_review`` (when provided) verifies the INTEGRATED
    delivered head as a unit before a ``project_done`` is allowed to stick."""
    milestone = False
    # SPEC-23 (Item 3, bound 3) — NON-RECURSION. A last-word turn is a turn the
    # HARNESS initiated, not the PM's own move, so it must be excluded from
    # detector accounting: charging `pm_idle` / `plan_streak` for it would let an
    # intervention feed the very counter that triggered it, and an intervention
    # could then manufacture the condition for another one. Scoped to the PLANNING
    # kinds: a `project_done` last word is deliberately routed through the normal
    # completion path below (Item 2), where the F128 gate judges it like any other
    # done claim.
    if isinstance(action, LastWord) and outcome.kind in {"planned",
                                                         "governance_progress"}:
        return milestone
    # Spec 25 (Item 3a): a turn that PARSED clears the shape-rejection window,
    # whatever else it did — the window bounds consecutive rejections, so one
    # malformed response between good ones must cost nothing.
    if not outcome.schema_rejected:
        c.schema_rejects = 0
    if outcome.kind in {"planned", "governance_progress"}:
        # Spec 25 (Item 3a): SHAPE and PROGRESS are accounted separately. A PM
        # turn the validator refused is not a PM sitting idle: it tried to say
        # something and the schema would not let it, and charging `pm_idle` for
        # that made compliance ACCELERATE termination (the gravity-golf run:
        # four rejected turns, `pm_idle_limit=2`, stopped `no_progress` with two
        # PRs open and no recorded reason). `plan_streak` is likewise untouched —
        # a turn that never parsed is not a plan.
        #
        # THE BOUND, so this can never become an infinite retry: only
        # `schema_reject_limit` consecutive rejections are absorbed. Past it they
        # count as idle again and the ordinary `no_progress` stop lands, with the
        # `pm turn rejected` decisions on the ledger naming the validator — the
        # diagnostic the old accounting destroyed. `schema_reject_limit=0`
        # reproduces today's trace exactly (every rejection counts as idle).
        if outcome.schema_rejected:
            c.schema_rejects += 1
            reject_limit = max(0, (policy or CodingAutonomyPolicy()).schema_reject_limit)
            if c.schema_rejects <= reject_limit:
                return milestone
        if outcome.made_progress:
            c.pm_idle = 0
        else:
            c.pm_idle += 1
        # Spec 07: plan-streak accounting. ONLY a `planned` (PM planning) turn is
        # the churn pathology — a PM that keeps creating dispatchable-looking tasks
        # while no worker ever runs. `made_progress` must not exempt it.
        #
        # `governance_progress` is NOT counted: governance turns (GovernancePlan /
        # Review / Materialize) all return `governance_progress` during the design
        # phase, when NO worker turn exists to reset the streak — so counting them
        # would trip `planning_churn` on a legitimate light/strict governance run
        # before implementation tasks are ever created. Governance advancing is
        # legitimate bounded progress toward implementation (independently guarded
        # by max_review_rounds / governance_review_not_converging), so it RESETS
        # the streak instead.
        if outcome.kind == "planned":
            c.plan_streak += 1
        else:  # governance_progress
            c.plan_streak = 0
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
        c.plan_streak = 0  # Spec 07: a worker turn — planning is not the only motion
        return False
    if outcome.kind == "pr_merged":
        c.tasks_done += 1
        c.since_checkpoint += 1
        c.pm_idle = 0
        c.plan_streak = 0  # Spec 07
        return True

    if outcome.kind == "task_blocked" and outcome.task is not None:
        rec.block_task(outcome.task, reason=outcome.reason or "blocked")
        c.pm_idle = 0
        c.plan_streak = 0  # Spec 07
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
        c.plan_streak = 0  # Spec 07
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
        c.plan_streak = 0  # Spec 07
        return milestone

    # noop / unknown — no reconciler transition fired, so an assigned task is
    # still sitting in `doing` from `rec.assign`. Spec 09 §3: return it to the
    # queue rather than stranding it (a stranded `doing` task is invisible to
    # `next_task` AND blocks every dependent forever).
    _requeue_stranded(ledger, action, outcome)
    return milestone
