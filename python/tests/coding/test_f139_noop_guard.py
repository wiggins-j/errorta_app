"""F139 WS-C (slices 3–4) — empty work never looks like progress.

Slice 3: the dev write path makes no empty commit, and the turn reports its real
net tree delta (`net_changed_files`). Slice 4: the runner scores a write-intent
turn with zero net change as `unproductive` (feeding the F127 ladder) instead of
closing the task `done` "already satisfied" (the Navigation-rewritten-100× bug).
"""
from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.turn_controller import CodingTurnController
from errorta_council.coding.workspace import CodingWorkspace
from errorta_tools.runner.apply_workspace import ApplyWorkspace


def _new_ws(project_id: str, tmp_path: Path) -> tuple[LedgerStore, CodingWorkspace]:
    store = LedgerStore(project_id, root=tmp_path / f"ledger-{project_id}")
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    ws = CodingWorkspace(project_id, store)
    ws.setup(target="new", repo_path=None)
    return store, ws


def _merge_file(ws: CodingWorkspace, task_id: str, path: str, content: str) -> None:
    branch = ws.start_task_branch(task_id)
    ws.write_file(path, content, task_id=task_id)
    assert ws.merge_pr(branch).get("merged")


# --- slice 3: allow_empty + net_changed_files -------------------------------


def test_dev_write_reemit_makes_no_commit(tmp_errorta_home: Path, tmp_path: Path) -> None:
    _store, ws = _new_ws("ng1", tmp_path)
    ws.start_task_branch("t1")
    head1 = ws.write_file("a.py", "x = 1\n", task_id="t1")
    head2 = ws.write_file("a.py", "x = 1\n", task_id="t1")  # identical → no-op
    assert head2 == head1, "re-emitting identical content must not create a commit"
    head3 = ws.write_file("a.py", "x = 2\n", task_id="t1")  # real change
    assert head3 != head1, "a real content change must commit"


def test_write_and_commit_default_allows_empty(tmp_errorta_home: Path, tmp_path: Path) -> None:
    # The seed / F039 / validation callers rely on the default allow_empty=True.
    seed = tmp_path / "seed"
    seed.mkdir()
    aw = ApplyWorkspace(run_id="coding-ng-empty")
    aw.ensure(seed)
    h1 = aw.head_ref()
    h2 = aw.write_and_commit("README.md", "hello\n")          # default True
    h3 = aw.write_and_commit("README.md", "hello\n")          # identical, still commits
    assert h2 != h1 and h3 != h2, "default allow_empty=True keeps historical behaviour"


def test_execute_dev_turn_net_changed_files_zero_on_reemit(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    store, ws = _new_ws("ng2", tmp_path)
    # master already has a.py (a prior merged slice).
    _merge_file(ws, "seed", "a.py", "x = 1\n")

    # A fresh task branch whose dev re-emits the SAME a.py → zero net change.
    ws.start_task_branch("t1")
    reemit = {"tool_calls": [
        {"tool": "code_write", "args": {"path": "a.py", "content": "x = 1\n"}}]}
    summary = CodingTurnController(store, ws).execute_dev_turn(
        task=_task(store, "t1"), member={"id": "m-dev"}, data=reemit)
    assert summary.success_count == 1          # the call "succeeded"
    assert summary.net_changed_files == 0      # but changed nothing vs master

    # A fresh task branch that writes a genuinely new file → net change 1.
    ws.start_task_branch("t2")
    newfile = {"tool_calls": [
        {"tool": "code_write", "args": {"path": "b.py", "content": "y = 2\n"}}]}
    summary2 = CodingTurnController(store, ws).execute_dev_turn(
        task=_task(store, "t2"), member={"id": "m-dev"}, data=newfile)
    assert summary2.net_changed_files == 1


def _task(store: LedgerStore, task_id: str):
    """A minimal Task carrying the given id (the runner sets these ids as branch
    names). We fabricate one rather than depend on add_task's generated id."""
    from errorta_council.coding.ledger import Task
    return Task(task_id=task_id, title="impl", role="dev")


# --- slice 4: runner scores a write-intent no-op as unproductive -------------


def test_runner_write_intent_reemit_is_unproductive(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    """The Navigation-100× case end to end: a dev that re-emits a file already on
    master (byte-identical) makes no net change → the runner marks the turn
    unproductive (feeding F127), re-queues the task, and records
    superseded_on_master — instead of the old auto-`done`."""
    import json

    from errorta_council.coding.runner import (
        build_run_turn,
        members_by_coding_role,
    )
    from errorta_council.coding.topology import DEV, Assign

    store = LedgerStore("ng-runner", root=tmp_path / "ledger-ng-runner")
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    ws = CodingWorkspace("ng-runner", store)
    ws.setup(target="new", repo_path=None)
    # master already has nav.tsx.
    _merge_file(ws, "seed", "nav.tsx", "export const Nav = 1\n")

    task = store.add_task(title="build the nav", role=DEV)

    def caller(_member, prompt):
        tid = __import__("re").search(
            r"developer for task id '([^']+)'", prompt).group(1)
        # re-emit the SAME nav.tsx already on master → zero net change
        return json.dumps({"schema_version": "coding_turn.v1", "role": "dev",
            "task_id": tid, "intent": {"kind": "tool_plan",
            "task_type": "implementation", "tool_calls": [
                {"tool": "code_write",
                 "args": {"path": "nav.tsx", "content": "export const Nav = 1\n"}}]}})

    rt = build_run_turn(store, ws, members_by_coding_role([
        {"id": "m-dev", "enabled": True, "metadata": {"coding_role": "dev"}}]),
        caller, guardrail_enabled=True)
    out = rt(Assign(member_id="m-dev", task_id=task.task_id, role=DEV), store)

    assert out.kind == "noop" and out.unproductive is True
    assert out.reason == "no_net_change"
    assert out.member_role == "dev"
    assert store.list_prs() == []
    assert any(d["choice"] == "superseded_on_master" for d in store.list_decisions())
    assert {t.task_id: t.state for t in store.list_tasks()}[task.task_id] == "todo"
    # master is unchanged (still just the seed + .gitignore) — the re-emit added nothing
    assert set(ws.list_files(scope="master")) == {".gitignore", "nav.tsx"}
