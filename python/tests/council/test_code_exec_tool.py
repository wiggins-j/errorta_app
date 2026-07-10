"""F039 slice 6 — code_exec handler wired to the F043 LocalToolRunner."""
from __future__ import annotations

import json
import sys

import pytest

from errorta_tools.builtins.code_exec import CodeExecHandler
from errorta_tools.gateway import FatalToolError, ToolCallRequest


def _req(arguments, *, tool_policy, run_id="run-1"):
    return ToolCallRequest(
        call_id="tc-exec-1", run_id=run_id, turn_id="t-1", member_id="m-1",
        tool_id="code_exec", arguments=arguments,
        metadata={"round": 1, "tool_policy": tool_policy},
    )


def _policy(workspace, **exec_kw):
    return {
        "code_read": {"enabled": True, "workspace_path": str(workspace)},
        "code_exec": {"enabled": True, **exec_kw},
        "execution": {"location": "local"},
    }


@pytest.fixture
def workspace(tmp_path, tmp_errorta_home):
    (tmp_path / "run.py").write_text("print('EXEC_OK')\n")
    return tmp_path


@pytest.mark.asyncio
async def test_code_exec_runs_in_workspace_and_returns_output(workspace):
    result = await CodeExecHandler().invoke(
        _req({"argv": [sys.executable, "run.py"]}, tool_policy=_policy(workspace))
    )
    assert result.status == "completed"
    payload = json.loads(result.content)
    assert payload["exit_code"] == 0
    assert "EXEC_OK" in payload["stdout_preview"]
    # Output hashes recorded; raw output lives in the runner's capped preview.
    assert payload["stdout_sha256"]


@pytest.mark.asyncio
async def test_code_exec_argv_must_be_list(workspace):
    with pytest.raises(FatalToolError) as e:
        await CodeExecHandler().invoke(
            _req({"argv": "python run.py"}, tool_policy=_policy(workspace))  # string!
        )
    assert "list_of_strings" in str(e.value)


@pytest.mark.asyncio
async def test_code_exec_no_workspace_is_fatal():
    with pytest.raises(FatalToolError) as e:
        await CodeExecHandler().invoke(
            _req({"argv": ["echo", "hi"]}, tool_policy={"code_exec": {"enabled": True}})
        )
    assert "no_workspace" in str(e.value)


@pytest.mark.asyncio
async def test_code_exec_nonzero_exit_surfaces_as_failed(workspace):
    (workspace / "boom.py").write_text("import sys; sys.exit(3)\n")
    result = await CodeExecHandler().invoke(
        _req({"argv": [sys.executable, "boom.py"]}, tool_policy=_policy(workspace))
    )
    payload = json.loads(result.content)
    assert payload["exit_code"] == 3
    # The runner classifies a nonzero exit; the gateway result reflects it.
    assert result.status in ("failed", "completed")
    assert result.provenance["runner_status"] == result.status


@pytest.mark.asyncio
async def test_code_exec_network_grant_fails_closed(workspace):
    # The unsandboxed (sandbox="none") tier can't firewall the network, so a
    # network grant there is refused — it requires a network-isolating sandbox.
    with pytest.raises(FatalToolError) as e:
        await CodeExecHandler().invoke(
            _req({"argv": [sys.executable, "run.py"]},
                 tool_policy=_policy(workspace, network=True))
        )
    assert "network_requires_sandbox" in str(e.value)


@pytest.mark.asyncio
async def test_code_exec_does_not_inherit_ambient_secret(workspace, monkeypatch):
    # A secret-shaped env var must NOT be visible to the spawned process.
    monkeypatch.setenv("MY_API_KEY", "sk-SHOULD-NOT-LEAK-123456")
    (workspace / "envdump.py").write_text(
        "import os; print('KEY=' + os.environ.get('MY_API_KEY', 'ABSENT'))\n"
    )
    result = await CodeExecHandler().invoke(
        _req({"argv": [sys.executable, "envdump.py"]}, tool_policy=_policy(workspace))
    )
    payload = json.loads(result.content)
    assert "sk-SHOULD-NOT-LEAK-123456" not in json.dumps(payload)
    assert "KEY=ABSENT" in payload["stdout_preview"]


@pytest.mark.asyncio
async def test_code_exec_uses_synthetic_home_and_temp(workspace, tmp_errorta_home):
    (workspace / "whereami.py").write_text(
        "import json, os\n"
        "print(json.dumps({\n"
        "  'HOME': os.environ.get('HOME'),\n"
        "  'TMPDIR': os.environ.get('TMPDIR'),\n"
        "  'TMP': os.environ.get('TMP'),\n"
        "  'TEMP': os.environ.get('TEMP'),\n"
        "}))\n"
    )

    result = await CodeExecHandler().invoke(
        _req({"argv": [sys.executable, "whereami.py"]},
             tool_policy=_policy(workspace), run_id="run-synthetic-home")
    )

    payload = json.loads(result.content)
    env = json.loads(payload["stdout_preview"])
    runtime_root = tmp_errorta_home / ".errorta" / "council" / "runner-runtime"
    assert env["HOME"].startswith(str(runtime_root))
    assert env["TMPDIR"].startswith(str(runtime_root))
    assert env["TMP"].startswith(str(runtime_root))
    assert env["TEMP"].startswith(str(runtime_root))
    assert env["HOME"] != str(tmp_errorta_home)
