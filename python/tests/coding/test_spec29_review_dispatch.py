"""SPEC-29 — review dispatch and merge liveness.

**This file is the deliverable.** SPEC-29 Item 2 says so in as many words — "the fix
is one predicate; the test is the spec." The predicate landed without it, so the one
guard that existed was itself untested and its two siblings still carried the deadlock.

The wedge, in one paragraph. GL05's a-priori partition holds any task whose touched
paths overlap a path owned by an in-flight task, and ownership releases only when the
owner's PR MERGES. A review task's touched paths are INFERRED from its title
("review PR: Create ball.js" -> {ball.js}) — and that path is owned by the very PR the
review exists to approve. So: the review waits for ownership to release, ownership
releases on merge, and merge requires the `reviewer_approved` that this review would
have produced. Nothing breaks the cycle. Run 4 delivered a background gradient and an
empty `update()` while six finished module PRs sat unreviewed, then stopped
`planning_churn`.

The exemption is sound because the partition exists to stop two WRITERS racing onto one
file before the first lands. A REVIEWER/TESTER reads the PR; it never opens a competing
PR on that file. Exempting non-writers removes exactly zero protection against the
collision GL05 was built for — which is the same reasoning `topology.py` already
recorded for F159's freeze teeth, applied to the gates that were missing it.

**Which of these were red, stated plainly.** Against the tree before ANY SPEC-29
follow-up work, four: `test_a_dispatched_reviewer_does_not_claim_the_path` (claim
half), `test_review_of_a_hot_owned_path_dispatches` (Item 3), and the two freeze-gate
locks (`test_review_of_a_frozen_path_dispatches`,
`test_frozen_wedge_pre_fix_shape_is_plan_pm`). The rest passed already, because the
GL05 *skip* predicate had landed — untested — and the protections they pin were never
broken. Those are regression locks, not red-green tests, and are worth no less for it:
an untested predicate is one refactor from silently reverting, and this file exists
precisely because that predicate shipped without a lock.

**The freeze gate is here because a review of the first fix caught it.** That attempt
guarded GL05's claim and F159's hot gate but left the freeze-intersect gate role-blind,
on the argument that `hot_file_freeze_stall_limit` force-lifts the freeze so it cannot
wedge. The lift is real and does fire — but it RACES `planning_churn`, and under the
documented `narrow_limit=0` disable value the churn ladder STOPs first. A bounded
escape that loses the race is not a defence, and the wedge it leaves is byte-for-byte
run 4's `[Plan(PM)]`.
"""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding.autonomy import (
    hot_owned_paths_by_task,
    inflight_owned_paths,
    inflight_owned_paths_by_task,
)
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.topology import (
    DEV,
    PM,
    REVIEWER,
    TESTER,
    Assign,
    plan_next_batch,
)


def _store(tmp_path: Path, name: str = "spec29") -> LedgerStore:
    s = LedgerStore(name, root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new",
                     repo_path=None)
    return s


def _assigns(batch) -> list[Assign]:
    return [a for a in batch if isinstance(a, Assign)]


def _module_pr_awaiting_review(s, path: str, *, branch: str):
    """Run 4's shape: a DEV task finished, its PR OPEN, and a review task queued.

    The DEV task is `done` with an open PR, so `inflight_owned_paths` still counts it
    as a live owner (that is what "releases only on merge" means). The review task
    declares no target_files — its paths come from the title, exactly as the real
    planner produces them.
    """
    dev = s.add_task(title=f"Create {path}", role=DEV, target_files=[path])
    s.update_task(dev.task_id, state="doing")
    pr = s.record_pr(task_id=dev.task_id, branch=branch, head=f"h-{branch}",
                     dev_member="m-dev1")
    s.update_pr(pr["pr_id"], changed_paths=[path])
    s.update_task(dev.task_id, state="done")
    review = s.add_task(title=f"review PR: Create {path}", role=REVIEWER,
                        pr_id=pr["pr_id"])
    return dev, pr, review


# --------------------------------------------------------------------------- #
# Item 1 — the GL05 non-writer exemption (skip half)
# --------------------------------------------------------------------------- #

def test_review_of_an_owned_path_dispatches(tmp_path: Path) -> None:
    """THE WEDGE LOCK. Fails red without the `role == DEV` guard at the GL05 skip."""
    s = _store(tmp_path)
    _dev, _pr, review = _module_pr_awaiting_review(s, "src/ball.js", branch="br-ball")

    owned = inflight_owned_paths(s)
    assert owned == {"src/ball.js"}, "the open PR must still own its path"
    # The review's inferred paths collide with the owner — this is the trap.
    from errorta_council.coding import paths as _paths
    assert _paths.paths_intersect(
        _paths.task_touched_paths(review), owned), "fixture must reproduce the overlap"

    ids = {a.task_id for a in _assigns(plan_next_batch(
        s, [("m-rev1", REVIEWER)], owned_paths=owned,
        owned_by_task=inflight_owned_paths_by_task(s)))}
    assert review.task_id in ids, (
        "the review of an owned path must dispatch — otherwise merge waits on the "
        "review, and the review waits on the merge")


def test_all_six_module_reviews_dispatch(tmp_path: Path) -> None:
    """Run 4 in miniature: six finished modules, six open PRs, nothing reviewed."""
    s = _store(tmp_path)
    modules = ["src/ball.js", "src/hole.js", "src/input.js",
               "src/gravity.js", "src/levels.js", "src/render.js"]
    reviews = [_module_pr_awaiting_review(s, p, branch=f"br-{i}")[2]
               for i, p in enumerate(modules)]

    idle = [(f"m-rev{i}", REVIEWER) for i in range(len(modules))]
    ids = {a.task_id for a in _assigns(plan_next_batch(
        s, idle, owned_paths=inflight_owned_paths(s),
        owned_by_task=inflight_owned_paths_by_task(s)))}
    assert {r.task_id for r in reviews} <= ids, (
        "every module review must dispatch; run 4 dispatched none and stopped "
        "planning_churn with six unreviewed PRs")


def test_tester_is_exempt_too(tmp_path: Path) -> None:
    # _WORKER_PRIORITY = (TESTER, REVIEWER, DEV) — the exemption is "non-writer",
    # not "reviewer", so TESTER must pass the gate on the same reasoning.
    s = _store(tmp_path)
    dev = s.add_task(title="Create src/api.py", role=DEV,
                     target_files=["src/api.py"])
    s.update_task(dev.task_id, state="doing")
    test_task = s.add_task(title="test src/api.py behaviour", role=TESTER)

    ids = {a.task_id for a in _assigns(plan_next_batch(
        s, [("m-test1", TESTER)], owned_paths=inflight_owned_paths(s),
        owned_by_task=inflight_owned_paths_by_task(s)))}
    assert test_task.task_id in ids


def test_two_devs_on_one_owned_file_still_serialize(tmp_path: Path) -> None:
    """The exemption must remove ZERO protection against the real collision.

    If this regresses, the fix has traded a deadlock for the file-thrash GL05 exists
    to prevent — which would be the worse bug.
    """
    s = _store(tmp_path)
    owner = s.add_task(title="owner", role=DEV, target_files=["src/store.ts"])
    s.update_task(owner.task_id, state="doing")
    second = s.add_task(title="second", role=DEV, target_files=["src/store.ts"])
    other = s.add_task(title="other", role=DEV, target_files=["src/other.ts"])

    ids = {a.task_id for a in _assigns(plan_next_batch(
        s, [("m-dev2", DEV)], owned_paths=inflight_owned_paths(s),
        owned_by_task=inflight_owned_paths_by_task(s)))}
    assert second.task_id not in ids, "two writers on one owned file must serialize"
    assert other.task_id in ids, "a disjoint file still fans out"


# --------------------------------------------------------------------------- #
# Item 1 — the CLAIM half
# --------------------------------------------------------------------------- #

def test_a_dispatched_reviewer_does_not_claim_the_path(tmp_path: Path) -> None:
    """Fails red without the `role == DEV` guard on the GL05 claim.

    Roles are walked TESTER -> REVIEWER -> DEV, so the reviewer is placed first and
    an unguarded claim would poison the same batch for the DEV behind it — a DEV that
    genuinely writes the file and holds no competing owner.
    """
    s = _store(tmp_path)
    # A review whose inferred path is src/widget.js, with NO in-flight owner, so the
    # only thing that could block the dev is the reviewer's own claim.
    review = s.add_task(title="review PR: Create src/widget.js", role=REVIEWER)
    dev = s.add_task(title="Create src/widget.js", role=DEV,
                     target_files=["src/widget.js"])

    batch = _assigns(plan_next_batch(
        s, [("m-rev1", REVIEWER), ("m-dev1", DEV)],
        owned_paths=set(), owned_by_task={}))
    ids = {a.task_id for a in batch}
    assert review.task_id in ids, "fixture: the reviewer should dispatch"
    assert dev.task_id in ids, (
        "a reviewer must not claim a path it never writes — the DEV behind it in the "
        "same batch was blocked by a claim staked on an inferred title path")


def test_two_devs_in_one_batch_still_cannot_share_a_file(tmp_path: Path) -> None:
    # The claim guard narrows the claim to writers; it must not disable it.
    s = _store(tmp_path)
    first = s.add_task(title="a", role=DEV, target_files=["src/shared.ts"])
    second = s.add_task(title="b", role=DEV, target_files=["src/shared.ts"])

    ids = {a.task_id for a in _assigns(plan_next_batch(
        s, [("m-dev1", DEV), ("m-dev2", DEV)],
        owned_paths=set(), owned_by_task={}))}
    assert len({first.task_id, second.task_id} & ids) == 1, (
        "exactly one of two same-file writers may be placed in a batch")


# --------------------------------------------------------------------------- #
# Item 3 — the same exemption for F159's merge-scoped hot-file gate
# --------------------------------------------------------------------------- #

def test_review_of_a_hot_owned_path_dispatches(tmp_path: Path) -> None:
    """F159's hot hold is GL05's sibling: it also releases only on merge.

    It was inert on run 4 (a greenfield run has no conflict history, so `hot` is
    empty) which is why GL05 fired first. The moment a file conflicts twice this gate
    reproduces the identical deadlock — the same wedge, one conflict later.
    """
    s = _store(tmp_path)
    _dev, _pr, review = _module_pr_awaiting_review(
        s, "src/mockData.ts", branch="br-mock")

    hot = {"src/mockData.ts"}
    ids = {a.task_id for a in _assigns(plan_next_batch(
        s, [("m-rev1", REVIEWER)],
        hot_paths=hot, hot_blocked=hot,
        hot_blocked_by_task=hot_owned_paths_by_task(s, {"src/mockData.ts": 2})))}
    assert review.task_id in ids, (
        "a review of a HOT owned path must dispatch for the same reason as GL05 — "
        "hot ownership also releases only on merge")


def test_two_devs_on_a_hot_file_still_serialize(tmp_path: Path) -> None:
    s = _store(tmp_path)
    hot = {"src/mockData.ts"}
    blocker = s.add_task(title="edit mock", role=DEV,
                         target_files=["src/mockData.ts"])
    s.update_task(blocker.task_id, state="doing")
    second = s.add_task(title="edit mock again", role=DEV,
                        target_files=["src/mockData.ts"])

    ids = {a.task_id for a in _assigns(plan_next_batch(
        s, [("m-dev2", DEV)], hot_paths=hot, hot_blocked=hot,
        hot_blocked_by_task=hot_owned_paths_by_task(s, {"src/mockData.ts": 2})))}
    assert second.task_id not in ids, "hot-file writers must still serialize"


# --------------------------------------------------------------------------- #
# The freeze gate — the third sibling, found by review of the first attempt
# --------------------------------------------------------------------------- #

def test_review_of_a_frozen_path_dispatches(tmp_path: Path) -> None:
    """The freeze gate wedges exactly like GL05 did, and the force-lift is no defence.

    The first attempt at SPEC-29 left this gate role-blind, reasoning that
    `hot_file_freeze_stall_limit` force-lifts the freeze so it cannot wedge. It does
    exist and it does fire — but it RACES `planning_churn` (plan_streak_limit=6, and
    the streak resets each trip), and with the documented `narrow_limit=0` disable
    value the churn ladder collapses to its stop tail and STOPs several iterations
    BEFORE the lift. A bounded escape that loses the race is not a defence.

    The cycle is GL05's: the freeze lifts only when the centralize owner's PR merges,
    and that merge needs the reviewer_approved this very review would produce.
    """
    s = _store(tmp_path)
    path = "src/mockData.ts"
    owner, _pr, review = _module_pr_awaiting_review(s, path, branch="br-central")

    batch = plan_next_batch(
        s, [("m-rev1", REVIEWER), ("pm-1", PM)],
        frozen={path}, frozen_owner_task_id=owner.task_id)
    ids = {a.task_id for a in _assigns(batch)}
    assert review.task_id in ids, (
        "a review of a FROZEN path must dispatch; pre-fix this returned [Plan(PM)] — "
        "run 4's exact stop signature")


def test_frozen_wedge_pre_fix_shape_is_plan_pm(tmp_path: Path) -> None:
    """Pin the run-4 OBSERVABLE, not just the negative.

    SPEC-29 Item 2 assertion 1 is stated in terms of what the batch IS — a lone
    Plan(PM) — not merely that the review is absent. With the fix the PM must no
    longer be the only thing the planner can find to do.
    """
    s = _store(tmp_path)
    path = "src/mockData.ts"
    owner, _pr, _review = _module_pr_awaiting_review(s, path, branch="br-c")

    batch = plan_next_batch(
        s, [("m-rev1", REVIEWER), ("pm-1", PM)],
        frozen={path}, frozen_owner_task_id=owner.task_id)
    assert _assigns(batch), (
        f"batch must contain a worker Assign, not a lone planning turn: {batch}")


def test_two_devs_on_a_frozen_file_still_serialize(tmp_path: Path) -> None:
    # The freeze must keep its teeth against WRITERS — that is what it is for.
    s = _store(tmp_path)
    path = "src/mockData.ts"
    owner = s.add_task(title=f"centralize {path}", role=DEV, target_files=[path])
    s.update_task(owner.task_id, state="doing")
    intruder = s.add_task(title=f"edit {path}", role=DEV, target_files=[path])

    ids = {a.task_id for a in _assigns(plan_next_batch(
        s, [("m-dev2", DEV)], frozen={path}, frozen_owner_task_id=owner.task_id))}
    assert intruder.task_id not in ids, "a frozen path must still hold other writers"


def test_prose_silent_dev_writers_are_still_held_under_a_freeze(
        tmp_path: Path) -> None:
    """The F159 teeth (`role == DEV and not tp`) must survive the exemption.

    A dev task that declares nothing cannot be proven safe while a freeze is
    active — that was the mockData.ts non-convergence — so it is still held.
    """
    s = _store(tmp_path)
    path = "src/mockData.ts"
    owner = s.add_task(title=f"centralize {path}", role=DEV, target_files=[path])
    s.update_task(owner.task_id, state="doing")
    silent = s.add_task(title="Add real-time activity feed", role=DEV)

    ids = {a.task_id for a in _assigns(plan_next_batch(
        s, [("m-dev2", DEV)], frozen={path}, frozen_owner_task_id=owner.task_id))}
    assert silent.task_id not in ids


# --------------------------------------------------------------------------- #
# The title-union path the spec calls load-bearing
# --------------------------------------------------------------------------- #

def test_review_dispatches_when_the_pr_touched_a_sibling_path(
        tmp_path: Path) -> None:
    """SPEC-29 names this as the difference between four-of-six and six-of-six.

    The review's title infers `hole.js`, but the PR's observed `changed_paths` is the
    `hole.test.js` sibling, so ownership is the UNION of the two. A review must
    dispatch against that union just as it does against the simple case.
    """
    s = _store(tmp_path)
    dev = s.add_task(title="Create src/hole.js", role=DEV,
                     target_files=["src/hole.js"])
    s.update_task(dev.task_id, state="doing")
    pr = s.record_pr(task_id=dev.task_id, branch="br-hole", head="h-hole",
                     dev_member="m-dev1")
    s.update_pr(pr["pr_id"], changed_paths=["src/hole.test.js"])
    s.update_task(dev.task_id, state="done")
    review = s.add_task(title="review PR: Create src/hole.js", role=REVIEWER,
                        pr_id=pr["pr_id"])

    ids = {a.task_id for a in _assigns(plan_next_batch(
        s, [("m-rev1", REVIEWER)], owned_paths=inflight_owned_paths(s),
        owned_by_task=inflight_owned_paths_by_task(s)))}
    assert review.task_id in ids


# --------------------------------------------------------------------------- #
# The escape hatch: partition off => unchanged dispatch
# --------------------------------------------------------------------------- #

def test_partition_off_is_unchanged(tmp_path: Path) -> None:
    """`owned_paths=None` and `owned_by_task=None` => partition_on is False.

    The `=1` sequential path and every pre-GL05 caller pass None, so this pins that
    SPEC-29 changed nothing for them.
    """
    s = _store(tmp_path)
    _dev, _pr, review = _module_pr_awaiting_review(s, "src/ball.js", branch="br-b")
    second = s.add_task(title="second", role=DEV, target_files=["src/ball.js"])

    ids = {a.task_id for a in _assigns(plan_next_batch(
        s, [("m-rev1", REVIEWER), ("m-dev2", DEV)]))}
    assert review.task_id in ids
    assert second.task_id in ids, (
        "with the partition off nothing is held — including the second writer")
