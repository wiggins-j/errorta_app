"""F039 slice 7 — code_write auto_apply into an isolated git workspace."""
from __future__ import annotations

import json
import sys

import pytest

from errorta_tools.builtins.code import CodeWriteHandler
from errorta_tools.builtins.code_exec import CodeExecHandler
from errorta_tools.gateway import ToolCallRequest
from errorta_tools.runner.apply_workspace import ApplyWorkspace


def _req(tool_id, arguments, *, tool_policy, run_id):
    return ToolCallRequest(
        call_id="tc-1", run_id=run_id, turn_id="t-1", member_id="m-1",
        tool_id=tool_id, arguments=arguments,
        metadata={"round": 1, "tool_policy": tool_policy},
    )


def _policy(ws, mode="auto_apply"):
    return {
        "code_read": {"enabled": True, "workspace_path": str(ws)},
        "code_write": {"enabled": True, "mode": mode},
        "code_exec": {"enabled": True},
        "execution": {"location": "local"},
    }


@pytest.fixture
def workspace(tmp_path, tmp_errorta_home):
    # The granted workspace is a user project, distinct from ERRORTA_HOME
    # (where the isolated apply workspace lives).
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "app.py").write_text("x = 1\n")
    return proj


@pytest.mark.asyncio
async def test_auto_apply_writes_to_isolated_workspace_not_user_tree(workspace):
    original = (workspace / "app.py").read_text()
    result = await CodeWriteHandler().invoke(
        _req("code_write", {"path": "app.py", "content": "x = 42\n"},
             tool_policy=_policy(workspace), run_id="run-aa-1")
    )
    # The user's tree is UNCHANGED — the apply landed in the isolated copy.
    assert (workspace / "app.py").read_text() == original
    assert result.provenance["applied"] is True
    assert result.provenance["isolated_workspace"] is True
    assert result.provenance["requires_human_accept_final"] is True
    # The isolated workspace has the applied change + a cumulative diff.
    aw = ApplyWorkspace(run_id="run-aa-1")
    assert (aw.root / "app.py").read_text() == "x = 42\n"
    assert "+x = 42" in result.content


@pytest.mark.asyncio
async def test_auto_apply_copy_skips_symlinks_secrets_and_bulk_dirs(workspace, tmp_path):
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("EXTERNAL_SECRET\n")
    try:
        (workspace / "linked-secret.txt").symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks unavailable on this platform")
    (workspace / ".env").write_text("TOKEN=SHOULD_NOT_PERSIST\n")
    (workspace / ".env.example").write_text("TOKEN=example\n")
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "pkg.js").write_text("large dependency\n")

    await CodeWriteHandler().invoke(
        _req("code_write", {"path": "app.py", "content": "x = 99\n"},
             tool_policy=_policy(workspace), run_id="run-aa-sanitized-copy")
    )

    aw = ApplyWorkspace(run_id="run-aa-sanitized-copy")
    assert not (aw.root / "linked-secret.txt").exists()
    assert not (aw.root / ".env").exists()
    assert not (aw.root / "node_modules").exists()
    assert (aw.root / ".env.example").read_text() == "TOKEN=example\n"


@pytest.mark.asyncio
async def test_auto_apply_rollback_restores_checkpoint(workspace):
    h = CodeWriteHandler()
    r1 = await h.invoke(
        _req("code_write", {"path": "app.py", "content": "x = 2\n"},
             tool_policy=_policy(workspace), run_id="run-aa-2")
    )
    checkpoint = r1.provenance["checkpoint"]  # state before the write
    aw = ApplyWorkspace(run_id="run-aa-2")
    assert (aw.root / "app.py").read_text() == "x = 2\n"
    # Roll back to the checkpoint -> the write is undone in the isolated copy.
    await h.invoke(
        _req("code_write", {"rollback": checkpoint},
             tool_policy=_policy(workspace), run_id="run-aa-2")
    )
    assert (aw.root / "app.py").read_text() == "x = 1\n"


@pytest.mark.asyncio
async def test_auto_apply_then_exec_runs_against_applied_change(workspace):
    # Apply a program, then code_exec runs it from the isolated workspace.
    await CodeWriteHandler().invoke(
        _req("code_write",
             {"path": "run.py", "content": "print('APPLIED_OUTPUT')\n"},
             tool_policy=_policy(workspace), run_id="run-aa-3")
    )
    res = await CodeExecHandler().invoke(
        _req("code_exec", {"argv": [sys.executable, "run.py"]},
             tool_policy=_policy(workspace), run_id="run-aa-3")
    )
    payload = json.loads(res.content)
    assert payload["exit_code"] == 0
    assert "APPLIED_OUTPUT" in payload["stdout_preview"]
    # run.py exists only in the isolated workspace, never the user's tree.
    assert not (workspace / "run.py").exists()


@pytest.mark.asyncio
async def test_auto_apply_path_traversal_blocked(workspace):
    from errorta_tools.gateway import FatalToolError

    pol = {
        "code_read": {"enabled": True, "workspace_path": str(workspace)},
        "code_write": {"enabled": True, "mode": "auto_apply"},
    }
    with pytest.raises(FatalToolError) as e:
        await CodeWriteHandler().invoke(
            _req("code_write", {"path": "../escape.py", "content": "x"},
                 tool_policy=pol, run_id="run-aa-4")
        )
    assert "escape" in str(e.value)


@pytest.mark.asyncio
async def test_auto_apply_refuses_workspace_containing_errorta_home(tmp_path, monkeypatch):
    # Misconfiguration: the granted workspace CONTAINS ${ERRORTA_HOME}. The
    # isolated copy would live inside the source -> ensure() must refuse, not
    # recurse. (Guard compares resolved paths so it holds under symlinks.)
    from errorta_tools.runner.apply_workspace import ApplyWorkspace, ApplyWorkspaceError

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "app.py").write_text("x = 1\n")
    monkeypatch.setenv("ERRORTA_HOME", str(proj / "nested_home"))
    aw = ApplyWorkspace(run_id="run-nested")
    with pytest.raises(ApplyWorkspaceError) as e:
        aw.ensure(proj)
    assert "inside_source" in str(e.value)


def test_apply_workspace_rejects_unsafe_run_id():
    from errorta_tools.runner.apply_workspace import ApplyWorkspace, ApplyWorkspaceError

    for bad in ["../escape", "a/b", "a\\b", "..", ""]:
        with pytest.raises(ApplyWorkspaceError):
            ApplyWorkspace(run_id=bad)
