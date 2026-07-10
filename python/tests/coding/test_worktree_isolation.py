from __future__ import annotations

import json
import threading
from pathlib import Path

from errorta_council.coding import testing as coding_testing
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.run_recovery import recover_orphaned_run
from errorta_council.coding.topology import REVIEWER, TESTER, Assign
from errorta_council.coding.workspace import CodingWorkspace

MEMBERS = [
    {"id": "m-dev", "enabled": True, "metadata": {"coding_role": "dev"}},
    {"id": "m-rev", "enabled": True, "metadata": {"coding_role": "reviewer"}},
    {"id": "m-test", "enabled": True, "metadata": {"coding_role": "tester"}},
]


def _workspace(project_id: str) -> tuple[LedgerStore, CodingWorkspace]:
    store = LedgerStore(project_id)
    store.create_project(
        north_star="Build a calculator",
        definition_of_done="tests pass",
        target="new",
        repo_path=None,
    )
    workspace = CodingWorkspace(project_id, store)
    workspace.setup(target="new", repo_path=None)
    return store, workspace


def _pass_session(command_ids: list[str]) -> coding_testing.TestRunSession:
    result = coding_testing.TestRunResult(
        command_id=command_ids[0],
        argv_sha256="sha",
        status="completed",
        exit_code=0,
        passed=True,
        duration_ms=1,
        stdout_sha256="out",
        stdout_preview="",
        stderr_preview="",
    )
    return coding_testing.TestRunSession(
        command_ids=command_ids,
        results=[result],
        unknown_ids=[],
        passed=True,
        sandbox="none",
    )


def test_task_worktrees_mutate_without_shared_head_clobber(
    tmp_errorta_home: Path,
) -> None:
    _store, workspace = _workspace("wt-parallel")
    branch_a = workspace.start_task_branch("a")
    branch_b = workspace.start_task_branch("b")
    root_a = workspace.task_root("a", branch=branch_a)
    root_b = workspace.task_root("b", branch=branch_b)

    assert root_a != root_b
    assert root_a != workspace.root()
    assert root_b != workspace.root()

    errors: list[BaseException] = []

    def write(task_id: str, path: str, content: str) -> None:
        try:
            workspace.write_file(path, content, task_id=task_id)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [
        threading.Thread(target=write, args=("a", "a.py", "A = 1\n")),
        threading.Thread(target=write, args=("b", "b.py", "B = 2\n")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert workspace._ws.current_branch() == "master"
    assert workspace._ws.read_file("a.py") is None
    assert workspace._ws.read_file("b.py") is None

    assert workspace.merge_pr(branch_a)["merged"] is True
    assert workspace.merge_pr(branch_b)["merged"] is True
    assert workspace._ws.read_file("a.py") == "A = 1\n"
    assert workspace._ws.read_file("b.py") == "B = 2\n"


def test_reviewer_uses_ref_only_diff_without_checkout(
    tmp_errorta_home: Path,
) -> None:
    from errorta_council.coding.runner import build_run_turn, members_by_coding_role

    store, workspace = _workspace("wt-review")
    dev = store.add_task(title="implement", role="dev")
    branch = workspace.start_task_branch(dev.task_id)
    workspace.write_file("calc.py", "def add(a, b):\n    return a + b\n", task_id=dev.task_id)
    pr = store.record_pr(
        task_id=dev.task_id,
        branch=branch,
        head=workspace.branch_head(branch),
        dev_member="m-dev",
    )
    review = store.add_task(title="review PR", role=REVIEWER, pr_id=pr["pr_id"])

    def forbidden_checkout(branch_name: str) -> None:
        raise AssertionError(f"checkout should not be called for {branch_name}")

    workspace.checkout = forbidden_checkout  # type: ignore[method-assign]

    def caller(member: dict, prompt: str) -> str:
        assert "calc.py" in prompt
        return json.dumps({
            "schema_version": "coding_turn.v1",
            "role": "reviewer",
            "task_id": review.task_id,
            "intent": {
                "kind": "review_verdict",
                "reviewed_head": pr["head"],
                "approved": True,
                "findings": [],
            },
        })

    run_turn = build_run_turn(
        store, workspace, members_by_coding_role(MEMBERS), caller,
        guardrail_enabled=True,
    )
    outcome = run_turn(
        Assign(member_id="m-rev", task_id=review.task_id, role=REVIEWER),
        store,
    )

    assert outcome.kind == "pr_reviewed"
    assert store.get_pr(pr["pr_id"])["reviewer_approved"] is True


def test_tester_runs_in_task_worktree_not_primary_root(
    tmp_errorta_home: Path,
    monkeypatch,
) -> None:
    import errorta_council.coding.runner as runner_mod
    from errorta_council.coding.runner import build_run_turn, members_by_coding_role

    store, workspace = _workspace("wt-test")
    dev = store.add_task(title="implement", role="dev")
    branch = workspace.start_task_branch(dev.task_id)
    workspace.write_file("calc.py", "def add(a, b):\n    return a + b\n", task_id=dev.task_id)
    pr = store.record_pr(
        task_id=dev.task_id,
        branch=branch,
        head=workspace.branch_head(branch),
        dev_member="m-dev",
    )
    store.update_pr(pr["pr_id"], reviewer_approved=True, reviewed_head=pr["head"])
    tester = store.add_task(title="test PR", role=TESTER, pr_id=pr["pr_id"])
    store.set_test_commands({"unit": {"argv": ["python", "-c", "pass"], "cwd": "."}})
    expected_root = workspace.task_root(dev.task_id, branch=branch)
    seen: dict[str, Path] = {}

    def fake_run_test_commands(workspace_root, registry, command_ids, **kwargs):
        seen["root"] = Path(workspace_root)
        assert registry == store.get_test_commands()
        assert command_ids == ["unit"]
        assert (Path(workspace_root) / "calc.py").exists()
        return _pass_session(command_ids)

    monkeypatch.setattr(runner_mod, "run_test_commands", fake_run_test_commands)

    def caller(member: dict, prompt: str) -> str:
        return json.dumps({
            "schema_version": "coding_turn.v1",
            "role": "tester",
            "task_id": tester.task_id,
            "intent": {
                "kind": "test_plan",
                "command_ids": ["unit"],
                "scope": "full_project",
                "rationale": "run unit tests",
            },
        })

    run_turn = build_run_turn(
        store, workspace, members_by_coding_role(MEMBERS), caller,
        guardrail_enabled=True,
    )
    outcome = run_turn(
        Assign(member_id="m-test", task_id=tester.task_id, role=TESTER),
        store,
    )

    assert outcome.kind == "pr_tested"
    assert seen["root"] == expected_root
    assert seen["root"] != workspace.root()


def test_recovery_reaps_orphaned_task_worktree(tmp_errorta_home: Path) -> None:
    store, workspace = _workspace("wt-recovery")
    task = store.add_task(title="implement", role="dev")
    branch = workspace.start_task_branch(task.task_id)
    task_root = workspace.task_root(task.task_id, branch=branch)
    workspace.write_file("calc.py", "x = 1\n", task_id=task.task_id)
    store.update_task(task.task_id, state="doing", assignee_member_id="m-dev")
    store.set_run_state(
        status="running",
        workspace_fingerprint=workspace.workspace_fingerprint(),
    )

    result = recover_orphaned_run(store, live=False, reason="test_restart")

    assert result.recovered is True
    assert result.requeued_task_ids == [task.task_id]
    assert store.list_tasks()[0].state == "todo"
    assert not task_root.exists()
    assert not workspace._ws.has_worktree(task.task_id)
    state = store.get_run_state()
    assert task.task_id not in (state.get("workspace_fingerprint") or {}).get(
        "worktrees", {}
    )
