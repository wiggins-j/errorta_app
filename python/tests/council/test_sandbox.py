"""F039 — code_exec hardened sandbox (seatbelt + docker + fail-closed)."""
from __future__ import annotations

import json
import sys

import pytest

from errorta_tools.builtins.code_exec import CodeExecHandler
from errorta_tools.gateway import FatalToolError, ToolCallRequest
from errorta_tools.runner import sandbox as sb
from errorta_tools.runner.artifacts import RunnerArtifactStore
from errorta_tools.runner.local import LocalToolRunner
from errorta_tools.runner.types import ToolRunnerRequest

_SEATBELT = sb.is_available(sb.SANDBOX_SEATBELT)
_DOCKER = sb.is_available(sb.SANDBOX_DOCKER)


# --------------------------------------------------------------------------- #
# wrap_argv / detection — pure unit
# --------------------------------------------------------------------------- #

def test_none_backend_returns_argv_verbatim(tmp_path):
    out = sb.wrap_argv(backend="none", argv=["echo", "hi"], workspace_root=tmp_path)
    assert out == ["echo", "hi"]


def test_unknown_backend_fails_closed(tmp_path):
    with pytest.raises(sb.SandboxUnavailable) as e:
        sb.wrap_argv(backend="microvm", argv=["echo"], workspace_root=tmp_path)
    assert e.value.reason_code == "sandbox_backend_unknown"


def test_empty_argv_fails_closed(tmp_path):
    with pytest.raises(sb.SandboxUnavailable) as e:
        sb.wrap_argv(backend="none", argv=[], workspace_root=tmp_path)
    assert e.value.reason_code == "sandbox_empty_argv"


@pytest.mark.skipif(not _SEATBELT, reason="sandbox-exec unavailable")
def test_seatbelt_wrap_shape_and_profile(tmp_path):
    out = sb.wrap_argv(
        backend="seatbelt",
        argv=["python3", "x.py"],
        workspace_root=tmp_path,
        writable_paths=[tmp_path / "home"],
        network_allowed=False,
    )
    assert out[0] == "sandbox-exec" and out[1] == "-p"
    profile = out[2]
    assert out[3:] == ["python3", "x.py"]
    assert "(deny network*)" in profile
    assert "(deny file-write*)" in profile
    assert str(tmp_path.resolve()) in profile  # workspace is write-granted


@pytest.mark.skipif(not _SEATBELT, reason="sandbox-exec unavailable")
def test_seatbelt_network_allowed_omits_deny(tmp_path):
    out = sb.wrap_argv(
        backend="seatbelt", argv=["true"], workspace_root=tmp_path,
        network_allowed=True,
    )
    assert "(deny network*)" not in out[2]


def test_bwrap_unavailable_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "is_available", lambda b: b == sb.SANDBOX_NONE)
    with pytest.raises(sb.SandboxUnavailable) as e:
        sb.wrap_argv(backend="bwrap", argv=["true"], workspace_root=tmp_path)
    assert e.value.reason_code == "sandbox_unavailable_bwrap"


def test_bwrap_wrap_shape_denies_network_and_confines_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "is_available", lambda b: True)
    home = tmp_path / "home"
    out = sb.wrap_argv(
        backend="bwrap", argv=["pytest", "-q"], workspace_root=tmp_path,
        writable_paths=[home], network_allowed=False,
    )
    assert out[0] == "bwrap"
    assert "--unshare-net" in out                      # network denied
    assert "--ro-bind" in out and out[out.index("--ro-bind") + 1] == "/"
    assert "--remount-ro" in out                       # root recursively ro
    # /tmp + /run are masked with an ephemeral tmpfs (not the host's).
    assert out.count("--tmpfs") == 2
    # workspace + home are rw-bound; argv is last after the -- separator.
    joined = " ".join(out)
    assert str(tmp_path.resolve()) in joined
    assert str(home.resolve()) in joined
    assert out[-2:] == ["pytest", "-q"]
    assert "--" in out and out.index("--") < len(out) - 2


def test_bwrap_network_allowed_omits_unshare_net(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "is_available", lambda b: True)
    out = sb.wrap_argv(
        backend="bwrap", argv=["true"], workspace_root=tmp_path,
        network_allowed=True,
    )
    assert "--unshare-net" not in out


def test_docker_unavailable_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "is_available", lambda b: b == sb.SANDBOX_NONE)
    with pytest.raises(sb.SandboxUnavailable) as e:
        sb.wrap_argv(backend="docker", argv=["true"], workspace_root=tmp_path)
    assert e.value.reason_code == "sandbox_unavailable_docker"


def test_docker_wrap_shape_when_available(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "is_available", lambda b: True)
    out = sb.wrap_argv(
        backend="docker", argv=["pytest", "-q"], workspace_root=tmp_path,
        network_allowed=False, docker_image="python:3.12-slim",
    )
    assert out[:3] == ["docker", "run", "--rm"]
    assert "--network" in out and "none" in out
    assert "python:3.12-slim" in out
    assert out[-2:] == ["pytest", "-q"]


def test_docker_network_allowed_omits_isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "is_available", lambda b: True)
    out = sb.wrap_argv(
        backend="docker", argv=["true"], workspace_root=tmp_path,
        network_allowed=True, docker_image="img",
    )
    assert "--network" not in out


@pytest.mark.parametrize("bad", ["--privileged", "-v", "--", "im age", "img;rm"])
def test_docker_image_injection_rejected(tmp_path, monkeypatch, bad):
    # A flag-shaped or malformed image would be parsed by `docker run` as an
    # argument in the image position -> reject before building the argv.
    monkeypatch.setattr(sb, "is_available", lambda b: True)
    with pytest.raises(sb.SandboxUnavailable) as e:
        sb.wrap_argv(
            backend="docker", argv=["true"], workspace_root=tmp_path,
            docker_image=bad,
        )
    assert e.value.reason_code == "sandbox_docker_image_invalid"


# --------------------------------------------------------------------------- #
# runner-level fail-closed
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_runner_blocks_when_sandbox_unavailable(tmp_path, tmp_errorta_home, monkeypatch):
    monkeypatch.setattr(sb, "is_available", lambda b: b == sb.SANDBOX_NONE)
    store = RunnerArtifactStore(root=tmp_path / "artifacts")
    runner = LocalToolRunner(artifact_store=store, policy={"action": "allow"})
    req = ToolRunnerRequest(
        request_id="r1", run_id="run-1", tool_call_id="tc-1",
        argv=("true",), workspace_root=str(tmp_path),
        sandbox="docker",
    )
    result = await runner.run(req)
    assert result.status == "blocked"
    assert result.reason_code == "sandbox_unavailable_docker"


# --------------------------------------------------------------------------- #
# code_exec policy wiring
# --------------------------------------------------------------------------- #

def _req(arguments, *, tool_policy, run_id="run-sbx"):
    return ToolCallRequest(
        call_id="tc-1", run_id=run_id, turn_id="t-1", member_id="m-1",
        tool_id="code_exec", arguments=arguments,
        metadata={"round": 1, "tool_policy": tool_policy},
    )


@pytest.fixture
def workspace(tmp_path, tmp_errorta_home):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "ok.py").write_text("print('ok')\n")
    return proj


@pytest.mark.asyncio
async def test_network_without_sandbox_fails_closed(workspace):
    pol = {
        "code_read": {"enabled": True, "workspace_path": str(workspace)},
        "code_exec": {"enabled": True, "network": True},
        "execution": {"location": "local", "sandbox": "none"},
    }
    with pytest.raises(FatalToolError) as e:
        await CodeExecHandler().invoke(
            _req({"argv": [sys.executable, "ok.py"]}, tool_policy=pol)
        )
    assert "network_requires_sandbox" in str(e.value)


@pytest.mark.asyncio
async def test_unknown_sandbox_backend_fails_closed(workspace):
    pol = {
        "code_read": {"enabled": True, "workspace_path": str(workspace)},
        "code_exec": {"enabled": True},
        "execution": {"location": "local", "sandbox": "microvm"},
    }
    with pytest.raises(FatalToolError) as e:
        await CodeExecHandler().invoke(
            _req({"argv": [sys.executable, "ok.py"]}, tool_policy=pol)
        )
    assert "backend_unknown" in str(e.value)


# --------------------------------------------------------------------------- #
# LIVE seatbelt — real OS enforcement (macOS only)
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not _SEATBELT, reason="sandbox-exec unavailable")
@pytest.mark.asyncio
async def test_seatbelt_blocks_network_live(workspace):
    # A program that opens an outbound socket must FAIL under the seatbelt
    # sandbox (deny network*). It exits non-zero / errors; without the sandbox
    # the connect attempt would not be denied at the syscall layer.
    prog = (
        "import socket,sys\n"
        "try:\n"
        "    s=socket.create_connection(('1.1.1.1',53),timeout=3)\n"
        "    s.close(); print('CONNECTED'); sys.exit(0)\n"
        "except Exception as e:\n"
        "    print('BLOCKED', file=sys.stderr); sys.exit(7)\n"
    )
    (workspace / "net.py").write_text(prog)
    pol = {
        "code_read": {"enabled": True, "workspace_path": str(workspace)},
        "code_exec": {"enabled": True},
        "execution": {"location": "local", "sandbox": "seatbelt"},
    }
    res = await CodeExecHandler().invoke(
        _req({"argv": [sys.executable, "net.py"]}, tool_policy=pol, run_id="run-net")
    )
    payload = json.loads(res.content)
    assert payload["exit_code"] != 0
    assert "CONNECTED" not in (payload.get("stdout_preview") or "")


@pytest.mark.skipif(not _SEATBELT, reason="sandbox-exec unavailable")
@pytest.mark.asyncio
async def test_seatbelt_confines_writes_live(workspace, tmp_path):
    # Writing INSIDE the workspace succeeds; writing OUTSIDE it is denied.
    outside = tmp_path / "outside.txt"
    prog = (
        "import sys\n"
        "open('inside.txt','w').write('in')\n"
        f"open({str(outside)!r},'w').write('out')\n"
    )
    (workspace / "w.py").write_text(prog)
    pol = {
        "code_read": {"enabled": True, "workspace_path": str(workspace)},
        "code_exec": {"enabled": True},
        "execution": {"location": "local", "sandbox": "seatbelt"},
    }
    res = await CodeExecHandler().invoke(
        _req({"argv": [sys.executable, "w.py"]}, tool_policy=pol, run_id="run-write")
    )
    payload = json.loads(res.content)
    # The outside write raised -> non-zero exit; the outside file never appears.
    assert payload["exit_code"] != 0
    assert not outside.exists()
