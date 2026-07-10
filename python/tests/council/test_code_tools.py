"""F039 slices 4-5 — code_read + code_write (propose_only) handlers."""
from __future__ import annotations

import pytest

from errorta_tools.builtins.code import CodeReadHandler, CodeWriteHandler
from errorta_tools.gateway import FatalToolError, ToolCallRequest


def _req(tool_id, arguments, *, tool_policy):
    return ToolCallRequest(
        call_id="tc-1", run_id="run-1", turn_id="t-1", member_id="m-1",
        tool_id=tool_id, arguments=arguments,
        metadata={"round": 1, "tool_policy": tool_policy},
    )


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hello')\n")
    (tmp_path / "secret.txt").write_text("top secret\n")
    return tmp_path


@pytest.mark.asyncio
async def test_code_read_returns_file_in_workspace(workspace):
    pol = {"code_read": {"enabled": True, "workspace_path": str(workspace)}}
    result = await CodeReadHandler().invoke(
        _req("code_read", {"path": "src/app.py"}, tool_policy=pol)
    )
    assert "hello" in result.content
    assert result.egress_class == "local"
    assert result.provenance["path"] == "src/app.py"


@pytest.mark.asyncio
async def test_code_read_blocks_path_traversal(workspace):
    pol = {"code_read": {"enabled": True, "workspace_path": str(workspace / "src")}}
    # ../secret.txt escapes the granted src/ workspace -> blocked.
    with pytest.raises(FatalToolError) as e:
        await CodeReadHandler().invoke(
            _req("code_read", {"path": "../secret.txt"}, tool_policy=pol)
        )
    assert "escape" in str(e.value)


@pytest.mark.asyncio
async def test_code_read_rejects_absolute_path(workspace):
    pol = {"code_read": {"enabled": True, "workspace_path": str(workspace)}}
    with pytest.raises(FatalToolError):
        await CodeReadHandler().invoke(
            _req("code_read", {"path": "/etc/passwd"}, tool_policy=pol)
        )


@pytest.mark.asyncio
async def test_code_read_no_workspace_is_fatal():
    with pytest.raises(FatalToolError) as e:
        await CodeReadHandler().invoke(_req("code_read", {"path": "x"}, tool_policy={}))
    assert "no_workspace" in str(e.value)


@pytest.mark.asyncio
async def test_code_write_propose_only_emits_diff_without_writing(workspace):
    pol = {
        "code_read": {"enabled": True, "workspace_path": str(workspace)},
        "code_write": {"enabled": True, "mode": "propose_only"},
    }
    original = (workspace / "src" / "app.py").read_text()
    result = await CodeWriteHandler().invoke(
        _req("code_write",
             {"path": "src/app.py", "content": "print('hi there')\n"},
             tool_policy=pol)
    )
    # A real unified diff is returned...
    assert "--- a/src/app.py" in result.content
    assert "+print('hi there')" in result.content
    assert result.provenance["applied"] is False
    # ...and the file on disk is UNCHANGED (propose_only never writes).
    assert (workspace / "src" / "app.py").read_text() == original


@pytest.mark.asyncio
async def test_code_write_new_file_diff(workspace):
    pol = {
        "code_read": {"enabled": True, "workspace_path": str(workspace)},
        "code_write": {"enabled": True, "mode": "propose_only"},
    }
    result = await CodeWriteHandler().invoke(
        _req("code_write", {"path": "src/new.py", "content": "x = 1\n"}, tool_policy=pol)
    )
    assert result.provenance["is_new_file"] is True
    assert "+x = 1" in result.content
    assert not (workspace / "src" / "new.py").exists()  # not written


@pytest.mark.asyncio
async def test_code_write_blocks_traversal(workspace):
    pol = {
        "code_read": {"enabled": True, "workspace_path": str(workspace / "src")},
        "code_write": {"enabled": True, "mode": "propose_only"},
    }
    with pytest.raises(FatalToolError) as e:
        await CodeWriteHandler().invoke(
            _req("code_write", {"path": "../evil.py", "content": "x"}, tool_policy=pol)
        )
    assert "escape" in str(e.value)


@pytest.mark.asyncio
async def test_code_write_unknown_mode_fails_closed(workspace):
    # propose_only + auto_apply are supported; any other mode fails closed.
    pol = {
        "code_read": {"enabled": True, "workspace_path": str(workspace)},
        "code_write": {"enabled": True, "mode": "yolo_apply"},
    }
    with pytest.raises(FatalToolError) as e:
        await CodeWriteHandler().invoke(
            _req("code_write", {"path": "src/app.py", "content": "x"}, tool_policy=pol)
        )
    assert "mode_unsupported" in str(e.value)
