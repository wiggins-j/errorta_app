"""F043 runner workspace and artifact isolation tests."""
from __future__ import annotations

import hashlib

import pytest

from errorta_tools.runner import (
    RunnerArtifactStore,
    ToolRunnerRequest,
    WorkspaceEscapeError,
    build_git_checkpoint_plan,
    resolve_workspace_path,
    safe_workspace_relative_path,
)


def test_workspace_path_resolves_inside_granted_root(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    child = workspace / "pkg"
    child.mkdir()

    assert resolve_workspace_path(workspace, "pkg") == child.resolve()
    assert safe_workspace_relative_path(workspace, child) == "pkg"


def test_workspace_path_rejects_parent_escape(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(WorkspaceEscapeError):
        resolve_workspace_path(workspace, "../outside")


def test_workspace_path_rejects_absolute_paths_even_inside_root(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(WorkspaceEscapeError):
        resolve_workspace_path(workspace, workspace)


def test_workspace_path_rejects_symlink_escape(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "leak").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkspaceEscapeError):
        resolve_workspace_path(workspace, "leak")


def test_runner_artifact_store_writes_hashed_run_local_artifact(tmp_path) -> None:
    store = RunnerArtifactStore(root=tmp_path / "artifacts")
    data = b"runner output"
    ref = store.write_bytes(
        run_id="run-1",
        request_id="req-1",
        name="stdout.txt",
        data=data,
        kind="stdout",
    )

    assert ref.sha256 == hashlib.sha256(data).hexdigest()
    assert ref.bytes == len(data)
    assert ref.metadata["name"] == "stdout.txt"
    assert ref.path == "run-1/req-1/stdout.txt"


def test_git_checkpoint_plan_is_safe_metadata_only(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = ToolRunnerRequest(
        request_id="req.1",
        run_id="run-1",
        tool_call_id="tc-1",
        argv=("python", "-V"),
        workspace_root=str(workspace),
    )

    plan = build_git_checkpoint_plan(request)

    assert plan.workspace_root == str(workspace.resolve())
    assert plan.checkpoint_ref == "refs/errorta/runner/run-1/req.1"
    assert plan.to_dict()["tool_call_id"] == "tc-1"
