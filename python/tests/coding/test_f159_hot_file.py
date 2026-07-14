"""F159 — hot-file serialization: path helpers, the hot-file map, the merge-scoped
dispatch gate, and the conflict-driven centralize + freeze (with never-lift)."""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding import paths, runner
from errorta_council.coding.autonomy import (
    CodingAutonomyPolicy,
    LoopCounters,
    _account_hot_file_freeze,
    frozen_paths,
    hot_files,
    hot_owned_paths,
)
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.topology import (
    DEV,
    PM,
    REVIEWER,
    Assign,
    plan_next_batch,
)

TEAM = [("m-pm", PM), ("m-dev1", DEV), ("m-dev2", DEV), ("m-rev", REVIEWER)]


def _store(tmp_path: Path, name: str = "hf") -> LedgerStore:
    s = LedgerStore(name, root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    return s


def _assigns(batch) -> list[Assign]:
    return [a for a in batch if isinstance(a, Assign)]


def _conflicted_pr(store: LedgerStore, task_id: str, branch: str, paths_: list[str]) -> None:
    pr = store.record_pr(task_id=task_id, branch=branch, head="h", dev_member="m-dev1")
    store.update_pr(pr["pr_id"], status="conflict", conflicts=paths_)


# --------------------------------------------------------------------------- #
# Phase 1 — path helpers + declared target_files
# --------------------------------------------------------------------------- #

def test_paths_intersect_bridges_basename_and_fullpath():
    assert paths.paths_intersect({"src/mockData.ts"}, {"mockData.ts"})
    assert paths.paths_intersect({"mockData.ts"}, {"src/mockData.ts"})
    assert not paths.paths_intersect({"src/a.ts"}, {"src/b.ts"})


def test_task_touched_paths_prefers_declared_then_prose():
    class T:
        title = "Create PostCard"
        detail = "edits src/mockData.ts for the feed"
        _extras = {"target_files": ["components/Post.tsx"]}
    tp = paths.task_touched_paths(T())
    assert tp == {"components/Post.tsx", "src/mockData.ts"}


def test_target_files_round_trips_through_add_task(tmp_path: Path):
    s = _store(tmp_path)
    t = s.add_task(title="x", role=DEV, target_files=["src/mockData.ts"])
    got = next(x for x in s.list_tasks() if x.task_id == t.task_id)
    assert got._extras.get("target_files") == ["src/mockData.ts"]
    assert paths.task_touched_paths(got) == {"src/mockData.ts"}


# --------------------------------------------------------------------------- #
# Phase 2 — hot-file map
# --------------------------------------------------------------------------- #

def test_hot_files_counts_conflicts_over_threshold(tmp_path: Path):
    s = _store(tmp_path)
    a = s.add_task(title="a", role=DEV)
    b = s.add_task(title="b", role=DEV)
    _conflicted_pr(s, a.task_id, "br-a", ["src/mockData.ts", "src/one.ts"])
    _conflicted_pr(s, b.task_id, "br-b", ["src/mockData.ts"])
    hot = hot_files(s, threshold=2)
    assert hot == {"src/mockData.ts": 2}  # one.ts conflicted once → not hot


# --------------------------------------------------------------------------- #
# Phase 3 — merge-scoped dispatch gate
# --------------------------------------------------------------------------- #

def test_no_hot_files_dispatches_identically(tmp_path: Path):
    s = _store(tmp_path)
    t1 = s.add_task(title="A", role=DEV, target_files=["src/mockData.ts"])
    t2 = s.add_task(title="B", role=DEV, target_files=["src/mockData.ts"])
    # No conflict history → nothing hot → both dispatch concurrently (as today).
    assigns = _assigns(plan_next_batch(
        s, TEAM, hot_paths=set(), hot_blocked=set()))
    assert {a.task_id for a in assigns} == {t1.task_id, t2.task_id}


def test_hot_file_owned_by_open_pr_blocks_second_toucher(tmp_path: Path):
    s = _store(tmp_path)
    # The file earned "hot" via two prior conflicts (those tasks are finished).
    for i in range(2):
        h = s.add_task(title=f"hist{i}", role=DEV)
        _conflicted_pr(s, h.task_id, f"br-hist{i}", ["src/mockData.ts"])
        s.update_task(h.task_id, state="done")
    # owner: an OPEN (un-merged) PR that touches the hot file — merge-scoped hold.
    owner = s.add_task(title="owner", role=DEV, target_files=["src/mockData.ts"])
    s.update_task(owner.task_id, state="doing")
    pr = s.record_pr(task_id=owner.task_id, branch="br-own", head="h", dev_member="m-dev1")
    assert pr["status"] == "open"
    # a second todo task that also touches the hot file
    second = s.add_task(title="second", role=DEV, target_files=["src/mockData.ts"])
    # an unrelated task the free dev CAN do
    other = s.add_task(title="other", role=DEV, target_files=["src/other.ts"])

    hot = hot_files(s, threshold=2)
    assert "src/mockData.ts" in hot
    owned = hot_owned_paths(s, hot)
    assert "src/mockData.ts" in owned  # held by the open-PR owner

    idle = [("m-dev2", DEV)]  # dev1 is busy on the owner
    assigns = _assigns(plan_next_batch(
        s, idle, hot_paths=set(hot), hot_blocked=owned))
    ids = {a.task_id for a in assigns}
    assert second.task_id not in ids       # blocked — waits for the owner to merge
    assert other.task_id in ids            # non-colliding work still dispatched


def test_hot_file_without_active_owner_dispatches_one_toucher(tmp_path: Path):
    s = _store(tmp_path)
    # The file is hot from history, but no current task or open PR owns it.
    for i in range(2):
        h = s.add_task(title=f"hist{i}", role=DEV)
        _conflicted_pr(s, h.task_id, f"br-hist{i}", ["src/mockData.ts"])
        s.update_task(h.task_id, state="done")
        pr = next(p for p in s.list_prs() if p["task_id"] == h.task_id)
        s.update_pr(pr["pr_id"], status="merged")
    first = s.add_task(title="first", role=DEV, target_files=["src/mockData.ts"])
    second = s.add_task(title="second", role=DEV, target_files=["src/mockData.ts"])

    hot = hot_files(s, threshold=2)
    assert hot_owned_paths(s, hot) == set()

    assigns = _assigns(plan_next_batch(
        s, [("m-dev1", DEV), ("m-dev2", DEV)], hot_paths=set(hot),
        hot_blocked=hot_owned_paths(s, hot)))
    ids = {a.task_id for a in assigns}
    assert len(ids & {first.task_id, second.task_id}) == 1


def test_frozen_path_only_owner_may_touch(tmp_path: Path):
    s = _store(tmp_path)
    owner = s.add_task(title="centralize", role=DEV, target_files=["src/mockData.ts"])
    blocked = s.add_task(title="feature", role=DEV, target_files=["src/mockData.ts"])
    frozen = {"src/mockData.ts"}
    # The frozen owner is allowed through; the other is held.
    a_owner = _assigns(plan_next_batch(
        s, [("m-dev1", DEV)], frozen=frozen, frozen_owner_task_id=owner.task_id))
    assert any(a.task_id == owner.task_id for a in a_owner)
    # With the owner already done, the other still can't touch the frozen file.
    s.update_task(owner.task_id, state="done")
    a_blocked = _assigns(plan_next_batch(
        s, [("m-dev1", DEV)], frozen=frozen, frozen_owner_task_id=owner.task_id))
    assert all(a.task_id != blocked.task_id for a in a_blocked)


# --------------------------------------------------------------------------- #
# Phase 5 — conflict-driven centralize + freeze, and the never-lift guard
# --------------------------------------------------------------------------- #

def test_escalation_centralizes_and_freezes(tmp_path: Path):
    s = _store(tmp_path)
    # Four PRs conflicting on the same file → crosses the escalation threshold (4).
    for i in range(4):
        t = s.add_task(title=f"t{i}", role=DEV)
        _conflicted_pr(s, t.task_id, f"br-{i}", ["src/mockData.ts"])
    runner._maybe_escalate_hot_files(s, ["src/mockData.ts"])
    # a single centralize owner task exists + is recorded
    owner_id = s.get_run_state().get("contract_owner_task_id")
    assert owner_id
    owner = next(t for t in s.list_tasks() if t.task_id == owner_id)
    assert "centralize" in owner.title.lower()
    # the file is frozen
    assert "src/mockData.ts" in frozen_paths(s)
    # a decision was recorded
    assert any(d.get("choice") == "hot_file_escalated" for d in s.list_decisions())
    # dedup: escalating again does not spawn a second owner
    runner._maybe_escalate_hot_files(s, ["src/mockData.ts"])
    assert s.get_run_state().get("contract_owner_task_id") == owner_id


def test_freeze_force_lifts_after_stall_limit(tmp_path: Path):
    s = _store(tmp_path)
    s.set_run_state(frozen_paths=["src/mockData.ts"])
    policy = CodingAutonomyPolicy(hot_file_freeze_stall_limit=3)
    c = LoopCounters()
    # Under the limit: freeze persists.
    for _ in range(2):
        _account_hot_file_freeze(s, c, policy)
    assert frozen_paths(s) == {"src/mockData.ts"}
    # Crossing the limit force-lifts + records the stall decision.
    _account_hot_file_freeze(s, c, policy)
    assert frozen_paths(s) == set()
    assert any(d.get("choice") == "hot_file_freeze_stalled" for d in s.list_decisions())


def test_freeze_lifts_when_no_paths(tmp_path: Path):
    s = _store(tmp_path)
    c = LoopCounters()
    c.hot_freeze_stall = 5
    _account_hot_file_freeze(s, c, CodingAutonomyPolicy())
    assert c.hot_freeze_stall == 0  # reset when nothing is frozen
