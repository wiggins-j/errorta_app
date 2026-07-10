"""F039 — auto-apply merge-back into the user's tree (human-accept-gated)."""
from __future__ import annotations

import pytest

from errorta_tools.builtins.code import CodeWriteHandler
from errorta_tools.gateway import ToolCallRequest
from errorta_tools.runner.apply_workspace import ApplyWorkspace, ApplyWorkspaceError


def _write_req(arguments, *, workspace, run_id):
    pol = {
        "code_read": {"enabled": True, "workspace_path": str(workspace)},
        "code_write": {"enabled": True, "mode": "auto_apply"},
        "execution": {"location": "local"},
    }
    return ToolCallRequest(
        call_id="tc-1", run_id=run_id, turn_id="t-1", member_id="m-1",
        tool_id="code_write", arguments=arguments,
        metadata={"round": 1, "tool_policy": pol},
    )


@pytest.fixture
def project(tmp_path, tmp_errorta_home):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "app.py").write_text("x = 1\n")
    (proj / "keep.py").write_text("untouched = True\n")
    return proj


async def _apply_change(project, run_id, path="app.py", content="x = 999\n"):
    await CodeWriteHandler().invoke(
        _write_req({"path": path, "content": content}, workspace=project, run_id=run_id)
    )


@pytest.mark.asyncio
async def test_preview_lists_changes_without_writing(project):
    await _apply_change(project, "mb-1")
    aw = ApplyWorkspace(run_id="mb-1")
    preview = aw.merge_back_preview()
    assert preview["has_changes"] is True
    assert {"path": "app.py", "status": "M"} in preview["changed_files"]
    assert preview["conflicts"] == []
    assert "+x = 999" in preview["diff"]
    # The user's tree is STILL untouched after a preview.
    assert (project / "app.py").read_text() == "x = 1\n"


@pytest.mark.asyncio
async def test_merge_back_writes_to_user_tree(project):
    await _apply_change(project, "mb-2")
    aw = ApplyWorkspace(run_id="mb-2")
    result = aw.merge_back()
    assert result["applied"] is True
    assert "app.py" in result["written"]
    # NOW the user's file carries the council's change.
    assert (project / "app.py").read_text() == "x = 999\n"
    # An untouched file is left alone.
    assert (project / "keep.py").read_text() == "untouched = True\n"


@pytest.mark.asyncio
async def test_merge_back_new_file_created_in_user_tree(project):
    await _apply_change(project, "mb-3", path="sub/new.py", content="print('new')\n")
    aw = ApplyWorkspace(run_id="mb-3")
    result = aw.merge_back()
    assert result["applied"] is True
    assert (project / "sub" / "new.py").read_text() == "print('new')\n"


@pytest.mark.asyncio
async def test_conflict_blocks_merge_back_and_writes_nothing(project):
    await _apply_change(project, "mb-4", content="x = 2\n")
    # The user edits the same file AFTER the run started -> divergence from both
    # baseline (x=1) and our proposed result (x=2).
    (project / "app.py").write_text("x = 1000  # user edit\n")
    aw = ApplyWorkspace(run_id="mb-4")
    preview = aw.merge_back_preview()
    assert "app.py" in preview["conflicts"]
    result = aw.merge_back()  # allow_conflicts defaults False
    assert result["applied"] is False
    assert result["reason"] == "conflicts"
    # Fail-closed: the user's concurrent edit is preserved, nothing clobbered.
    assert (project / "app.py").read_text() == "x = 1000  # user edit\n"


@pytest.mark.asyncio
async def test_conflict_can_be_forced(project):
    await _apply_change(project, "mb-5", content="x = 2\n")
    (project / "app.py").write_text("x = 1000  # user edit\n")
    aw = ApplyWorkspace(run_id="mb-5")
    result = aw.merge_back(allow_conflicts=True)
    assert result["applied"] is True
    assert (project / "app.py").read_text() == "x = 2\n"


@pytest.mark.asyncio
async def test_user_edit_matching_proposal_is_not_a_conflict(project):
    # If the user already made the same edit we propose, it's not a conflict.
    await _apply_change(project, "mb-6", content="x = 7\n")
    (project / "app.py").write_text("x = 7\n")
    aw = ApplyWorkspace(run_id="mb-6")
    assert aw.merge_back_preview()["conflicts"] == []


@pytest.mark.asyncio
async def test_preview_fails_closed_when_source_gone(project):
    await _apply_change(project, "mb-7")
    aw = ApplyWorkspace(run_id="mb-7")
    import shutil
    shutil.rmtree(project)
    with pytest.raises(ApplyWorkspaceError) as e:
        aw.merge_back_preview()
    assert "source_missing" in str(e.value)
