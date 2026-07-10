"""F087 Slice 3a — worktree registry concurrency + branch-resolution fixes.

The worktree registry is a single JSON file mutated by every dev/tester/merge
turn. Under concurrent dispatch it must (1) never be left half-written or lose an
entry in a read-modify-write race, and (2) never silently swap a task onto the
default ``task-<id>`` branch when a caller asks for the worktree without naming a
branch (head_ref / write_and_commit / task_root do this).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from errorta_tools.runner.apply_workspace import ApplyWorkspace


def _ready_workspace(run_id: str) -> ApplyWorkspace:
    ws = ApplyWorkspace(run_id=run_id)
    seed = Path(ws.root).parent / f"{run_id}-seed"
    seed.mkdir(parents=True, exist_ok=True)
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    ws.ensure(seed)
    return ws


def test_concurrent_worktree_for_no_lost_entries(tmp_errorta_home: Path) -> None:
    ws = _ready_workspace("wt-reg-conc")
    n = 12
    errors: list[BaseException] = []

    def make(i: int) -> None:
        try:
            ws.worktree_for(f"t{i}", branch=f"task-t{i}", base="master")
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=make, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not any(t.is_alive() for t in threads)
    assert errors == []
    # Registry is valid JSON and holds every distinct task (none lost in r-m-w).
    registry = json.loads(
        ws._worktree_registry_path.read_text(encoding="utf-8"))
    assert {f"t{i}" for i in range(n)} <= set(registry)


def test_worktree_for_keeps_branch_when_unspecified(tmp_errorta_home: Path) -> None:
    ws = _ready_workspace("wt-reg-branch")
    # Register a task on a CUSTOM branch (not the default task-<id>).
    ws.worktree_for("task1", branch="feature/custom", base="master")
    first = ws.head_ref(task_id="task1")

    # A follow-up call WITHOUT a branch (as head_ref/write_and_commit do) must
    # keep it on feature/custom instead of evicting + recreating on task-task1.
    again = ws.worktree_for("task1")
    registry = json.loads(
        ws._worktree_registry_path.read_text(encoding="utf-8"))
    assert registry["task1"]["branch"] == "feature/custom"
    assert ws.head_ref(task_id="task1") == first
    assert again.exists()


def test_save_registry_is_atomic(tmp_errorta_home: Path) -> None:
    ws = _ready_workspace("wt-reg-atomic")
    ws.worktree_for("t1", branch="task-t1")
    # No stale temp files left behind by the atomic tempfile+os.replace write.
    leftovers = list(ws._worktree_registry_path.parent.glob(".worktrees-*.tmp"))
    assert leftovers == []
    assert ws._worktree_registry_path.exists()
