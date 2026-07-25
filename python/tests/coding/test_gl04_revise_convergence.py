"""GL04 — revise convergence.

Two net-new convergence guards composed on top of committed Spec 16:

* GAP-4 — the DIFF-LEVEL breaker: diff-stasis (near-identical resubmission) and
  oscillation/revert (A->B->A with a distinct finding class per hop), tripping
  Spec 16's ONE breaker as a THIRD condition, BEFORE its depth+class cap.
* GAP-5 — the RUN-LEVEL convergence clamp: past a windowed superseded-ratio /
  merge-rate band it clamps fan-out to serial (soft, releasable), one rung below
  Spec 16's lineage-scoped hard `revise_livelock` stop.

The regression locks are the point: a lineage making REAL progress (distinct
diffs, distinct classes) is never broken, and the clamp never wedges the run.
"""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding import runner
from errorta_council.coding.autonomy import (
    CodingAutonomyPolicy,
    LoopCounters,
    _account_convergence_clamp,
    _account_dispatch_wedge,
    runtime_cap,
    save_policy,
)
from errorta_council.coding.ledger import LedgerStore


def _store(pid: str, tmp_path: Path) -> LedgerStore:
    s = LedgerStore(pid, root=tmp_path / f"l-{pid}")
    s.create_project(north_star="n", definition_of_done="d", target="new",
                     repo_path=None)
    return s


# --------------------------------------------------------------------------- #
# Diff fixtures. Distinct files/lines so each change is genuinely different; the
# revert is the exact sign-flip of DIFF_B.
# --------------------------------------------------------------------------- #

def _add(path: str, *lines: str) -> str:
    body = "".join(f"+{ln}\n" for ln in lines)
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n{body}")


def _remove(path: str, *lines: str) -> str:
    body = "".join(f"-{ln}\n" for ln in lines)
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1,{len(lines)} +0,0 @@\n{body}")


DIFF_A = _add("foo.py", "def foo():", "    return 1")
DIFF_B = _add("bar.py", "def bar():", "    return 2")
DIFF_REVERT_B = _remove("bar.py", "def bar():", "    return 2")   # sign-flip of DIFF_B
DIFF_C = _add("baz.py", "def baz():", "    return 3")
DIFF_D = _add("qux.py", "def qux():", "    return 4")


def _fp(diff: str) -> dict:
    return runner._diff_fingerprint(diff)


# --------------------------------------------------------------------------- #
# GAP-4 Phase A — fingerprint primitives.
# --------------------------------------------------------------------------- #

def test_identical_diffs_have_zero_stasis_distance() -> None:
    assert runner._fp_stasis_distance(_fp(DIFF_B), _fp(DIFF_B)) == 0.0


def test_whitespace_only_reshuffle_is_still_stasis() -> None:
    # Indentation change on the same content normalizes away -> still near-identical.
    spaced = _add("bar.py", "def bar():", "        return 2")
    assert runner._fp_stasis_distance(_fp(DIFF_B), _fp(spaced)) == 0.0


def test_distinct_diffs_are_far_apart() -> None:
    assert runner._fp_stasis_distance(_fp(DIFF_A), _fp(DIFF_B)) == 1.0


def test_signflip_is_a_full_revert() -> None:
    assert runner._fp_revert_overlap(_fp(DIFF_REVERT_B), _fp(DIFF_B)) == 1.0


def test_distinct_change_is_not_a_revert() -> None:
    assert runner._fp_revert_overlap(_fp(DIFF_C), _fp(DIFF_B)) == 0.0


def test_partial_revert_below_threshold_is_a_fraction() -> None:
    # Undo ONE of the two added lines -> 0.5 overlap, below the 0.7 default.
    partial = _remove("bar.py", "def bar():")
    assert runner._fp_revert_overlap(_fp(partial), _fp(DIFF_B)) == 0.5


def test_empty_diff_never_reads_as_stasis_or_revert() -> None:
    assert runner._fp_stasis_distance(_fp(""), _fp(DIFF_B)) == 1.0
    assert runner._fp_revert_overlap(_fp(""), _fp(DIFF_B)) == 0.0


# --------------------------------------------------------------------------- #
# GAP-4 Phase B — the breaker on a lineage.
# --------------------------------------------------------------------------- #

def _revise(s, *, branch, prev_pr, depth, cls, supersedes_diff):
    """A revise task that supersedes ``prev_pr``, carrying that PR's diff fingerprint
    (what the runner stores at revise-task creation), plus its own new PR."""
    t = s.add_task(title=f"revise: {branch}", role="dev", pr_id=prev_pr["pr_id"],
                   revise_depth=depth, finding_class=cls,
                   diff_fingerprint=_fp(supersedes_diff))
    pr = s.record_pr(task_id=t.task_id, branch=branch, head=f"h-{branch}",
                     dev_member="m")
    return t, pr


def _reject(s, *, pr, task, findings, diff, source="reviewer"):
    runner._handle_review_rejection(s, None, pr=pr, task=task, findings=findings,
                                    source=source, diff=diff)


def _revises(s):
    return [t for t in s.list_tasks() if t.title.startswith("revise:")]


def test_oscillation_breaks_even_with_a_distinct_class_per_hop(
        tmp_errorta_home, tmp_path) -> None:
    # A->B->A: p0 adds A, p1 adds B, p2 REVERTS B. Each hop a DISTINCT finding class,
    # so Spec 16's class-only breaker never fires; the diff shows the loop is
    # spinning, and GAP-4 breaks it at the reverting round (depth 2 < the depth-3 cap).
    s = _store("osc", tmp_path)
    t0 = s.add_task(title="impl", role="dev")
    p0 = s.record_pr(task_id=t0.task_id, branch="b0", head="h0", dev_member="m")
    _r1, p1 = _revise(s, branch="b1", prev_pr=p0, depth=1, cls=["alpha"],
                      supersedes_diff=DIFF_A)   # r1 opened p1 (adds B)
    r2, p2 = _revise(s, branch="b2", prev_pr=p1, depth=2, cls=["beta"],
                     supersedes_diff=DIFF_B)     # r2 opened p2 (reverts B)
    _reject(s, pr=p2, task=r2, findings=[{"blocking": True, "title": "gamma"}],
            diff=DIFF_REVERT_B)

    assert s.get_pr(p2["pr_id"])["status"] == "blocked"          # broke, terminal
    assert len(_revises(s)) == 2                                  # NO 3rd revise
    broke = [d for d in s.list_decisions() if d["choice"] == "revise_chain_broken"]
    assert len(broke) == 1 and broke[0].get("trigger") == "diff_deadlock"
    assert any(t.role == "pm" and t.title.startswith("revise chain broken")
               for t in s.list_tasks())                          # one PM re-plan


def test_near_identical_resubmission_breaks_on_round_two(
        tmp_errorta_home, tmp_path) -> None:
    # The dev resubmits essentially the same change: p1's diff == p0's diff. Diff-
    # stasis trips at round two (depth 1), long before the class cap.
    s = _store("stasis", tmp_path)
    t0 = s.add_task(title="impl", role="dev")
    p0 = s.record_pr(task_id=t0.task_id, branch="b0", head="h0", dev_member="m")
    r1, p1 = _revise(s, branch="b1", prev_pr=p0, depth=1, cls=["alpha"],
                     supersedes_diff=DIFF_A)     # r1 stores fp(DIFF_A); p1 re-adds A
    _reject(s, pr=p1, task=r1, findings=[{"blocking": True, "title": "alpha again"}],
            diff=DIFF_A)                          # p1's diff == the predecessor's

    assert s.get_pr(p1["pr_id"])["status"] == "blocked"
    assert len(_revises(s)) == 1                  # NO 2nd revise on the stuck lineage
    assert any(d["choice"] == "revise_chain_broken" for d in s.list_decisions())


def test_distinct_diff_each_round_is_never_broken(
        tmp_errorta_home, tmp_path) -> None:
    # The real-progress lock: distinct diffs AND distinct classes each hop is healthy
    # work through a hard defect — neither stasis nor revert, and Spec 16's class cap
    # resets on a new class. A 4th revise DOES spawn even at the depth-3 cap.
    s = _store("progress", tmp_path)
    t0 = s.add_task(title="impl", role="dev")
    p0 = s.record_pr(task_id=t0.task_id, branch="b0", head="h0", dev_member="m")
    _r1, p1 = _revise(s, branch="b1", prev_pr=p0, depth=1, cls=["alpha"],
                      supersedes_diff=DIFF_A)
    _r2, p2 = _revise(s, branch="b2", prev_pr=p1, depth=2, cls=["beta"],
                      supersedes_diff=DIFF_B)
    r3, p3 = _revise(s, branch="b3", prev_pr=p2, depth=3, cls=["gamma"],
                     supersedes_diff=DIFF_C)      # p3 is a genuinely new change (D)
    _reject(s, pr=p3, task=r3, findings=[{"blocking": True, "title": "delta"}],
            diff=DIFF_D)

    assert s.get_pr(p3["pr_id"])["status"] != "blocked"
    assert len(_revises(s)) == 4                  # a 4th revise DID spawn
    assert not any(d["choice"] == "revise_chain_broken" for d in s.list_decisions())


def test_anchor_regression_feeds_the_oscillation_signal(
        tmp_errorta_home, tmp_path) -> None:
    # GL01's anchor lock recorded that this head flipped a green test anchor red —
    # oscillation at the artifact level. GAP-4 consumes that decision and breaks even
    # though this round's diff is genuinely NEW (no stasis, no revert).
    s = _store("anchor", tmp_path)
    t0 = s.add_task(title="impl", role="dev")
    p0 = s.record_pr(task_id=t0.task_id, branch="b0", head="h0", dev_member="m")
    r1, p1 = _revise(s, branch="b1", prev_pr=p0, depth=1, cls=["alpha"],
                     supersedes_diff=DIFF_A)      # p1 head == "h-b1"
    s.record_decision(title="test anchor regressed", context="anchor",
                      choice="anchor_regressed",
                      rationale="anchor 'web:probe' green at h0 now red at h-b1")
    _reject(s, pr=p1, task=r1, findings=[{"blocking": True, "title": "beta"}],
            diff=DIFF_C)                          # a NEW diff — only the anchor trips

    assert s.get_pr(p1["pr_id"])["status"] == "blocked"
    assert len(_revises(s)) == 1
    assert any(d["choice"] == "revise_chain_broken" for d in s.list_decisions())


def test_both_seams_guarded_pm_review_arm_also_breaks(
        tmp_errorta_home, tmp_path) -> None:
    # The strict-mode PM-review arm routes through the same seam, so an oscillation
    # breaks there too — otherwise the spiral has a wide-open back door.
    s = _store("pmarm", tmp_path)
    t0 = s.add_task(title="impl", role="dev")
    p0 = s.record_pr(task_id=t0.task_id, branch="b0", head="h0", dev_member="m")
    _r1, p1 = _revise(s, branch="b1", prev_pr=p0, depth=1, cls=["alpha"],
                      supersedes_diff=DIFF_A)
    r2, p2 = _revise(s, branch="b2", prev_pr=p1, depth=2, cls=["beta"],
                     supersedes_diff=DIFF_B)
    _reject(s, pr=p2, task=r2, findings=[{"blocking": True, "title": "gamma"}],
            diff=DIFF_REVERT_B, source="pm_review")
    assert s.get_pr(p2["pr_id"])["status"] == "blocked"
    assert len(_revises(s)) == 2


def test_no_diff_keeps_spec16_class_only_behaviour(
        tmp_errorta_home, tmp_path) -> None:
    # A caller without a diff (the Spec 16 unit-test path) keeps class-only behaviour:
    # an oscillation with a distinct class does NOT break, a revise still spawns.
    s = _store("nodiff", tmp_path)
    t0 = s.add_task(title="impl", role="dev")
    p0 = s.record_pr(task_id=t0.task_id, branch="b0", head="h0", dev_member="m")
    _r1, p1 = _revise(s, branch="b1", prev_pr=p0, depth=1, cls=["alpha"],
                      supersedes_diff=DIFF_A)
    r2, p2 = _revise(s, branch="b2", prev_pr=p1, depth=2, cls=["beta"],
                     supersedes_diff=DIFF_B)
    runner._handle_review_rejection(s, None, pr=p2, task=r2,
                                    findings=[{"blocking": True, "title": "gamma"}],
                                    source="reviewer")   # no diff=
    assert s.get_pr(p2["pr_id"])["status"] != "blocked"
    assert len(_revises(s)) == 3


def test_diff_deadlock_disabled_keeps_the_chain_running(
        tmp_errorta_home, tmp_path) -> None:
    # diff_deadlock=False disables GAP-4: an oscillation with a distinct class runs on.
    s = _store("off", tmp_path)
    save_policy(s, CodingAutonomyPolicy(diff_deadlock=False))
    t0 = s.add_task(title="impl", role="dev")
    p0 = s.record_pr(task_id=t0.task_id, branch="b0", head="h0", dev_member="m")
    _r1, p1 = _revise(s, branch="b1", prev_pr=p0, depth=1, cls=["alpha"],
                      supersedes_diff=DIFF_A)
    r2, p2 = _revise(s, branch="b2", prev_pr=p1, depth=2, cls=["beta"],
                     supersedes_diff=DIFF_B)
    _reject(s, pr=p2, task=r2, findings=[{"blocking": True, "title": "gamma"}],
            diff=DIFF_REVERT_B)
    assert s.get_pr(p2["pr_id"])["status"] != "blocked"
    assert len(_revises(s)) == 3


# --------------------------------------------------------------------------- #
# GAP-5 — the run-level convergence clamp.
# --------------------------------------------------------------------------- #

_MEMBERS = [("m1", "dev"), ("m2", "dev")]   # base parallelism 2


def _resolved_pr(s, i: int, status: str) -> None:
    t = s.add_task(title=f"impl {i}", role="dev")
    pr = s.record_pr(task_id=t.task_id, branch=f"b{i}", head=f"h{i}", dev_member="m")
    s.update_pr(pr["pr_id"], status=status)


def _seed_window(s, *, superseded: int, merged: int, blocked: int, base: int = 0):
    """Seed ``base`` benign merged PRs then the churny window (created last, so it is
    ``resolved[-window:]``)."""
    n = 0
    for status, count in (("merged", base), ("superseded", superseded),
                          ("merged", merged), ("blocked", blocked)):
        for _ in range(count):
            _resolved_pr(s, n, status)
            n += 1


def test_high_superseded_ratio_clamps_fanout_to_serial(
        tmp_errorta_home, tmp_path) -> None:
    s = _store("clamp", tmp_path)
    policy = CodingAutonomyPolicy()                       # window 20
    # The 53/96-superseded shape scaled into a 20-PR window: 11 superseded, 6 merged,
    # 3 blocked -> ratio 0.55 (>= 0.5). Fan-out is 2 before the clamp.
    _seed_window(s, superseded=11, merged=6, blocked=3)
    assert runtime_cap(policy, _MEMBERS, s) == 2          # unclamped fan-out

    _account_convergence_clamp(s, LoopCounters(), policy)

    assert s.get_run_state().get("convergence_clamped") is True
    assert runtime_cap(policy, _MEMBERS, s) == 1          # clamped to serial
    assert any(d["choice"] == "convergence_clamped" for d in s.list_decisions())


def test_low_merge_rate_alone_clamps(tmp_errorta_home, tmp_path) -> None:
    # The OR arm: even with a modest superseded-ratio, a merge-rate <= 0.35 trips it.
    s = _store("mrate", tmp_path)
    policy = CodingAutonomyPolicy()
    _seed_window(s, superseded=6, merged=6, blocked=8)    # ratio 0.30, merge 0.30
    _account_convergence_clamp(s, LoopCounters(), policy)
    assert s.get_run_state().get("convergence_clamped") is True


def test_healthy_window_never_clamps(tmp_errorta_home, tmp_path) -> None:
    s = _store("healthy", tmp_path)
    policy = CodingAutonomyPolicy()
    _seed_window(s, superseded=3, merged=15, blocked=2)   # ratio 0.15, merge 0.75
    _account_convergence_clamp(s, LoopCounters(), policy)
    assert not s.get_run_state().get("convergence_clamped")
    assert runtime_cap(policy, _MEMBERS, s) == 2


def test_partial_window_does_not_trip(tmp_errorta_home, tmp_path) -> None:
    # Fewer than a full window of resolved PRs is noise — never clamp on it.
    s = _store("partial", tmp_path)
    policy = CodingAutonomyPolicy()
    _seed_window(s, superseded=8, merged=0, blocked=0)    # only 8 resolved < 20
    _account_convergence_clamp(s, LoopCounters(), policy)
    assert not s.get_run_state().get("convergence_clamped")


def test_clamp_releases_when_churn_recovers_and_never_wedges(
        tmp_errorta_home, tmp_path) -> None:
    s = _store("release", tmp_path)
    policy = CodingAutonomyPolicy()
    _seed_window(s, superseded=11, merged=6, blocked=3)   # trip window
    _account_convergence_clamp(s, LoopCounters(), policy)
    assert s.get_run_state().get("convergence_clamped") is True

    # The window recovers: 20 clean merges become resolved[-20:]. Past the release
    # band (ratio <= 0.35 AND merge-rate >= 0.5) the clamp lifts.
    for i in range(100, 120):
        _resolved_pr(s, i, "merged")
    _account_convergence_clamp(s, LoopCounters(), policy)
    assert s.get_run_state().get("convergence_clamped") is False
    assert runtime_cap(policy, _MEMBERS, s) == 2          # fan-out restored
    assert any(d["choice"] == "convergence_released" for d in s.list_decisions())


def test_dead_band_holds_the_clamp_no_flapping(
        tmp_errorta_home, tmp_path) -> None:
    # Hysteresis: a window BETTER than the trip band but not yet past the tighter
    # release band leaves the clamp engaged — distinct bands prevent boundary flap.
    s = _store("hyst", tmp_path)
    policy = CodingAutonomyPolicy()
    s.set_run_state(convergence_clamped=True)
    # ratio 0.40 (> release 0.35, < trip 0.50), merge-rate 0.45 (< release 0.5).
    _seed_window(s, superseded=8, merged=9, blocked=3)
    _account_convergence_clamp(s, LoopCounters(), policy)
    assert s.get_run_state().get("convergence_clamped") is True   # still clamped


def test_clamped_run_with_a_dispatchable_task_still_dispatches(
        tmp_errorta_home, tmp_path) -> None:
    # The non-wedge lock: the clamp only NARROWS concurrency (runtime_cap -> 1); it
    # never makes a dispatchable task non-dispatchable, so `_account_dispatch_wedge`
    # does not fire and a ready task still runs, serially.
    s = _store("nowedge", tmp_path)
    policy = CodingAutonomyPolicy()
    s.set_run_state(convergence_clamped=True)
    s.add_task(title="ready work", role="dev")            # a dispatchable dev head
    assert runtime_cap(policy, _MEMBERS, s) == 1          # clamped to serial
    assert s.next_task("dev") is not None                 # still dispatchable
    assert _account_dispatch_wedge(s, LoopCounters(), policy) is None   # not wedged


def test_window_zero_disables_the_detector(tmp_errorta_home, tmp_path) -> None:
    s = _store("zero", tmp_path)
    policy = CodingAutonomyPolicy(convergence_window=0)
    _seed_window(s, superseded=18, merged=0, blocked=2)   # would clamp if enabled
    _account_convergence_clamp(s, LoopCounters(), policy)
    assert not s.get_run_state().get("convergence_clamped")
    assert runtime_cap(policy, _MEMBERS, s) == 2


def test_clamp_composes_with_spec16_livelock_soft_before_hard() -> None:
    # The escalation ladder: GAP-5's soft clamp is wired BEFORE Spec 16's hard
    # `revise_livelock` stop, in BOTH loops (the concurrent path is where a churning
    # fan-out actually runs). Source-level dead-code lock, mirroring Spec 16's own.
    src = Path(runner.__file__).with_name("autonomy.py").read_text("utf-8")
    assert src.count("_account_convergence_clamp(ledger, c, policy)") >= 2
    clamp = src.index("_account_convergence_clamp(ledger, c, policy)")
    livelock = src.index("_account_revise_livelock(ledger, c, policy)")
    assert clamp < livelock          # soft clamp precedes the hard stop
