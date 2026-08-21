# Live-Run Supervisor (Slice 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A wall-clock supervisor in the sidecar that launches an operator-declared profile (local + ssh steps + reverse tunnel), watches declared probes, tears down safely with logoff evidence on stall, survives sidecar restarts by tearing down (never resuming), and is driven/narrated through Slack.

**Architecture:** New package `python/errorta_liverun/` (sibling of `errorta_slack`, outside `errorta_council`) holds profile validation, step primitives, a persisted state machine on a daemon thread, and boot recovery. It reuses a real `RemoteToolRunner` (ssh, new) and reverse-forward support in `errorta_tunnels`. Slack reaches it in-process via new `ToolDeps` seams; the outbound poller reads its `events.jsonl` as a fifth item source.

**Tech Stack:** Python ≥3.10, PyYAML (already a dep), `subprocess`/`threading`, pytest (+ `pytest-asyncio`, `asyncio_mode=auto`), existing `errorta_tunnels`, `errorta_tools.runner`, `errorta_slack`, `errorta_policy`.

Spec: `docs/superpowers/specs/2026-08-21-live-run-supervisor-design.md`. Read it first.

## Global Constraints

- Profiles live ONLY under `ERRORTA_HOME/liverun/profiles/<name>.yaml`, `created_by: operator`, `version: 1`; loaded fail-closed; Slack may select a profile by name, never author one.
- No model-composed string may ever reach a `local` or `remote` argv. Only supervisor-generated `$SESSION_ID` / `$RUN_ID` are substituted, after validation.
- `local.argv[0]` must be an absolute path, or one of `osascript`, `pgrep`; `./gradlew` is allowed only with an absolute `cwd`.
- Banned argv tokens: `--ignore-risk-budget`, `--no-safety-plane`. A remote step whose argv contains both `senditai_ng.cli` and `run` must also contain `--max-session-seconds`, `--receipt-id`, `--require-live-feed`.
- Cap defaults (may only be lowered): `max_launches_per_hour: 2`, `min_launch_gap_s: 900`, `max_launches_per_day: 8`, `max_consecutive_failed_cycles: 2`.
- `teardown` must contain a sub-step with `evidence_literal: logoff_verified`; absence of the literal is reported as `logoff_verified: ABSENT`, never as success.
- `stop_live_run` is R-class and never waits on approval. `resume_live_run` is C-class and is NEVER auto-fired by autopilot. `start_live_run` is C-class.
- Runs are never resumed across a sidecar restart; boot recovery runs the full teardown and marks `lost_on_restart`.
- `errorta_council` must never import `errorta_liverun` or `errorta_tools.runner.remote` (existing import-lint tests must stay green).
- All text reaching the ledger/Slack passes `errorta_council.coding.runtime_process.redact_log_line`.
- Every new module gets tests before the OSRS profile is authored; run `python3 -m pytest -q tests/<area>` after each task and the full `python3 -m pytest -q` before the final commit.
- Commit after every task. Work from `python/` as the cwd for pytest (`cd /Users/wiggins/GitHub/errorta_app/python`).

## File Structure

| File | Responsibility |
|---|---|
| `python/errorta_tunnels/manager.py` (modify) | `TunnelSpec.reverse_forwards`, `-R` argv, `_Child.kill` → `os.killpg`, `TunnelManager.close(spec)` |
| `python/errorta_tools/runner/remote.py` (replace) | `RemoteToolRunner` over a fixed ssh argv; `build_remote_ssh_argv`; `run_sync` |
| `python/errorta_liverun/__init__.py` | package marker |
| `python/errorta_liverun/profile.py` | dataclasses + `load_profile(path)` + `ProfileError`; all validator rules |
| `python/errorta_liverun/steps.py` | step/probe primitives; `StepResult`; argv-identity guard; token substitution |
| `python/errorta_liverun/state.py` | `RunState`, atomic persistence, `events.jsonl`, `LaunchLedger` caps |
| `python/errorta_liverun/supervisor.py` | `Supervisor` state machine + thread; `LiveRunManager` registry (singleton) |
| `python/errorta_liverun/recovery.py` | boot reconcile of non-terminal runs |
| `python/errorta_slack/tools.py` (modify) | 5 verbs, `ToolDeps` seams, `HUMAN_ONLY_VERBS` |
| `python/errorta_slack/connection.py` (modify) | human-only carve-out in autopilot; cancel text → `stop_live_run` |
| `python/errorta_slack/outbound.py` (modify) | `_liverun_items` source; PNG upload on stop |
| `python/errorta_app/slack_lifecycle.py` (modify) | forward `interval_s`/`timeout_minutes`; `post_file` on posters |
| `python/errorta_app/server.py` (modify) | boot recovery + lifespan teardown |
| `python/tests/tunnels/test_tunnel_manager.py` (modify) | reverse argv, killpg, close |
| `python/tests/tools/test_remote_runner.py` | fake-ssh tests |
| `python/tests/liverun/test_profile.py`, `test_steps.py`, `test_state.py`, `test_supervisor.py`, `test_recovery.py` | unit tests |
| `python/tests/slack/test_tools.py`, `test_connection.py`, `test_outbound.py` (modify) | Slack surface |
| `python/tests/acceptance/test_liverun_fake_profile.py` | end-to-end with fake client/brain |
| `docs/liverun/README.md` + `docs/liverun/example-profile.yaml` | operator docs |

---

### Task 1: Reverse tunnels, group kill, and `close()` in `errorta_tunnels`

**Files:**
- Modify: `python/errorta_tunnels/manager.py:55-110` (TunnelSpec, build_ssh_argv), `:112-140` (_Child.kill), `:195-285` (TunnelManager)
- Test: `python/tests/tunnels/test_tunnel_manager.py`

**Interfaces:**
- Produces: `TunnelSpec(ssh_host, remote_port=0, remote_host="127.0.0.1", ..., reverse_forwards: tuple[tuple[int,int],...]=())` — `remote_port` may be `0` iff `reverse_forwards` is non-empty. `build_ssh_argv(spec, local_port)` emits `-R 127.0.0.1:<rp>:127.0.0.1:<lp>` per pair and **no** `-L` when the spec is reverse-only. `TunnelManager.close(spec) -> bool` stops one tunnel. `TunnelManager.status_for(spec)["state"]` is `"up"` for a reverse-only tunnel once its child has been alive for one watch interval.

- [ ] **Step 1: Write the failing tests**

Append to `python/tests/tunnels/test_tunnel_manager.py`:

```python
def test_reverse_forwards_emit_R_and_no_L() -> None:
    spec = TunnelSpec(ssh_host="box", remote_port=0,
                      reverse_forwards=((8081, 8081), (8082, 18082))).validated()
    argv = build_ssh_argv(spec, 0)
    assert "-L" not in argv
    assert argv[argv.index("-R") + 1] == "127.0.0.1:8081:127.0.0.1:8081"
    assert argv.count("-R") == 2
    assert "127.0.0.1:8082:127.0.0.1:18082" in argv


def test_reverse_only_spec_requires_forwards() -> None:
    with pytest.raises(TunnelValidationError):
        TunnelSpec(ssh_host="box", remote_port=0).validated()
    with pytest.raises(TunnelValidationError):
        TunnelSpec(ssh_host="box", remote_port=0, reverse_forwards=((0, 80),)).validated()


def test_child_kill_kills_process_group(tmp_path) -> None:
    # A shell that forks a sleeping grandchild; killing the leader alone
    # leaves the grandchild alive. killpg must take both.
    import os, signal, subprocess, time
    from errorta_tunnels.manager import _Child
    marker = tmp_path / "pid"
    child = _Child(["/bin/sh", "-c", f"sleep 30 & echo $! > {marker}; wait"])
    for _ in range(50):
        if marker.exists() and marker.read_text().strip():
            break
        time.sleep(0.05)
    grandchild = int(marker.read_text().strip())
    child.kill()
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild, 0)


def test_close_stops_one_tunnel() -> None:
    mgr, children = _manager()
    a = TunnelSpec(ssh_host="a", remote_port=1)
    b = TunnelSpec(ssh_host="b", remote_port=2)
    mgr.ensure(a, wait=False); mgr.ensure(b, wait=False)
    assert mgr.close(a) is True
    assert mgr.status_for(a) is None
    assert mgr.status_for(b) is not None
    assert mgr.close(a) is False
    mgr.teardown()
```

`_manager()` is the existing helper at `tests/tunnels/test_tunnel_manager.py:129` returning `(TunnelManager, children)` with a fake spawn; reuse it. Make sure `pytest` and `TunnelValidationError` are imported at the top of the file.

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest -q tests/tunnels/test_tunnel_manager.py -k "reverse or kill_kills or close_stops"`
Expected: FAIL (`TypeError: unexpected keyword 'reverse_forwards'`, `AttributeError: close`).

- [ ] **Step 3: Implement**

In `manager.py`, extend `TunnelSpec`:

```python
@dataclass(frozen=True)
class TunnelSpec:
    ssh_host: str
    remote_port: int
    remote_host: str = "127.0.0.1"
    ssh_port: Optional[int] = None
    ssh_username: Optional[str] = None
    ssh_key_path: Optional[str] = None
    # (remote_port, local_port) pairs for `ssh -R`; reverse-only specs set
    # remote_port=0. Loopback on both ends by construction.
    reverse_forwards: tuple[tuple[int, int], ...] = ()

    @property
    def reverse_only(self) -> bool:
        return bool(self.reverse_forwards) and not self.remote_port

    def validated(self) -> "TunnelSpec":
        ssh_host = _validate_token(self.ssh_host, what="ssh_host")
        remote_host = _validate_token(self.remote_host, what="remote_host")
        reverse = tuple(
            (_validate_port(rp, what="reverse_remote_port"),
             _validate_port(lp, what="reverse_local_port"))
            for rp, lp in self.reverse_forwards
        )
        if self.remote_port:
            remote_port = _validate_port(self.remote_port, what="remote_port")
        elif reverse:
            remote_port = 0
        else:
            raise TunnelValidationError("remote_port required unless reverse_forwards set")
        ssh_port = _validate_port(self.ssh_port, what="ssh_port") if self.ssh_port else None
        ssh_username = (
            _validate_token(self.ssh_username, what="ssh_username") if self.ssh_username else None)
        key = self.ssh_key_path
        if key is not None:
            key = os.path.expanduser(key)
            if not os.path.isfile(key):
                raise TunnelValidationError(f"ssh_key_path not a file: {self.ssh_key_path!r}")
        return TunnelSpec(
            ssh_host=ssh_host, remote_port=remote_port, remote_host=remote_host,
            ssh_port=ssh_port, ssh_username=ssh_username, ssh_key_path=key,
            reverse_forwards=reverse)
```

(Keep the existing username/key validation bodies exactly as they are today at `:65-82`; the snippet shows where the new lines go.) In `build_ssh_argv`, replace the `-L` line:

```python
    if spec.reverse_forwards:
        for rp, lp in spec.reverse_forwards:
            argv += ["-R", f"127.0.0.1:{rp}:127.0.0.1:{lp}"]
    if not spec.reverse_only:
        local_port = _validate_port(local_port, what="local_port")
        argv += ["-L", f"127.0.0.1:{local_port}:{spec.remote_host}:{spec.remote_port}"]
```

and move the existing `local_port = _validate_port(...)` at the top of the function inside that `if`. In `_Child.kill`, replace `self._proc.kill()` with:

```python
        try:
            os.killpg(self._proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass
```

(`import signal` at top.) In `_watch`, the "is the forward accepting yet?" branch becomes:

```python
            if tun.spec.reverse_only or _port_accepts(tun.local_port):
```

Add to `TunnelManager`:

```python
    def close(self, spec: TunnelSpec) -> bool:
        """Stop one tunnel and forget it. Returns False if none existed."""
        spec = spec.validated()
        with self._lock:
            tun = self._tunnels.pop(spec, None)
        if tun is None:
            return False
        tun.stop.set()
        self._kill_child(tun)
        if tun.thread is not None:
            tun.thread.join(timeout=5)
        return True
```

- [ ] **Step 4: Run the tunnel tests**

Run: `python3 -m pytest -q tests/tunnels`
Expected: all PASS (existing `-L` tests included).

- [ ] **Step 5: Commit**

```bash
git add python/errorta_tunnels/manager.py python/tests/tunnels/test_tunnel_manager.py
git commit -m "feat(tunnels): reverse forwards, process-group kill, close(spec)"
```

---

### Task 2: Real `RemoteToolRunner` over ssh

**Files:**
- Replace: `python/errorta_tools/runner/remote.py`
- Test: `python/tests/tools/test_remote_runner.py` (create; create `python/tests/tools/__init__.py` if absent)

**Interfaces:**
- Produces:
  - `build_remote_ssh_argv(host: str, remote_argv: Sequence[str], *, ssh_port: int|None=None, ssh_username: str|None=None, ssh_key_path: str|None=None, ssh_bin: str="ssh") -> list[str]`
  - `class RemoteToolRunner(ssh_bin="ssh", source_env=None)` with `async run(request: ToolRunnerRequest) -> ToolRunnerResult` and `run_sync(request, *, stdin_path: str|None=None) -> ToolRunnerResult`. `request.metadata` may carry `{"ssh_host": str, "ssh_port": int, "ssh_username": str, "ssh_key_path": str}`; `execution_location` must be `"remote_ssh"`. `stdin_path` streams that file to the remote command's stdin.
  - Remote argv is `shlex.join`ed into a single trailing argument after `--`.

- [ ] **Step 1: Write the failing tests**

```python
# python/tests/tools/test_remote_runner.py
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from errorta_tools.runner.remote import RemoteToolRunner, build_remote_ssh_argv
from errorta_tools.runner.types import ToolRunnerRequest


def _req(**kw) -> ToolRunnerRequest:
    base = dict(request_id="r1", run_id="run1", tool_call_id="t1",
                argv=("echo", "hi there"), workspace_root="/tmp",
                execution_location="remote_ssh", timeout_seconds=5,
                metadata={"ssh_host": "box"})
    base.update(kw)
    return ToolRunnerRequest(**base)


@pytest.fixture
def fake_ssh(tmp_path: Path) -> Path:
    """A stand-in `ssh` that records argv + stdin and runs the remote command locally."""
    script = tmp_path / "ssh"
    script.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {tmp_path}/argv\n"
        f"cat > {tmp_path}/stdin\n"
        "while [ \"$1\" != \"--\" ]; do shift; done; shift\n"
        "eval \"$1\"\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_argv_is_hardened_and_uses_double_dash() -> None:
    argv = build_remote_ssh_argv("box", ["tail", "-n", "5", "a b.log"], ssh_port=2222,
                                 ssh_username="u", ssh_key_path=None)
    assert argv[0] == "ssh"
    for flag in ("BatchMode=yes", "StrictHostKeyChecking=yes", "ConnectTimeout=10",
                 "ServerAliveInterval=15", "ServerAliveCountMax=3"):
        assert flag in argv
    assert "-N" not in argv
    assert argv[argv.index("-p") + 1] == "2222"
    assert argv[-3:] == ["u@box", "--", "tail -n 5 'a b.log'"]


def test_rejects_flag_injection_in_host() -> None:
    with pytest.raises(ValueError):
        build_remote_ssh_argv("-oProxyCommand=evil", ["true"])


def test_run_sync_executes_and_caps_output(fake_ssh: Path, tmp_path: Path) -> None:
    runner = RemoteToolRunner(ssh_bin=str(fake_ssh))
    res = runner.run_sync(_req(argv=("echo", "hi there")))
    assert res.status == "completed" and res.exit_code == 0
    assert res.stdout_preview.strip() == "hi there"
    recorded = (tmp_path / "argv").read_text().splitlines()
    assert recorded[-2:] == ["--", "echo 'hi there'"]


def test_stdin_path_reaches_stdin_not_argv(fake_ssh: Path, tmp_path: Path) -> None:
    secret = tmp_path / "tok"; secret.write_text("s3cr3t")
    runner = RemoteToolRunner(ssh_bin=str(fake_ssh))
    runner.run_sync(_req(argv=("cat",)), stdin_path=str(secret))
    assert (tmp_path / "stdin").read_text() == "s3cr3t"
    assert "s3cr3t" not in (tmp_path / "argv").read_text()


def test_timeout_kills_group_and_reports(fake_ssh: Path) -> None:
    runner = RemoteToolRunner(ssh_bin=str(fake_ssh))
    res = runner.run_sync(_req(argv=("sh", "-c", "sleep 30 & wait"), timeout_seconds=0.5))
    assert res.status == "timed_out" and res.reason_code == "runner_timeout"


def test_wrong_location_is_blocked() -> None:
    res = RemoteToolRunner().run_sync(_req(execution_location="local"))
    assert res.status == "blocked" and res.reason_code == "runner_location_not_remote"


def test_missing_host_is_blocked() -> None:
    res = RemoteToolRunner().run_sync(_req(metadata={}))
    assert res.status == "blocked" and res.reason_code == "remote_host_missing"


async def test_async_run_delegates(fake_ssh: Path) -> None:
    res = await RemoteToolRunner(ssh_bin=str(fake_ssh)).run(_req())
    assert res.status == "completed"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest -q tests/tools/test_remote_runner.py`
Expected: FAIL with `ImportError: cannot import name 'build_remote_ssh_argv'`.

- [ ] **Step 3: Implement**

```python
# python/errorta_tools/runner/remote.py
"""Remote ToolRunner: one bounded command over a fixed ``ssh`` argv.

Supervisor egress primitive (live-run), NOT a member tool: it inherits the
real user environment (HOME, SSH_AUTH_SOCK) so ``~/.ssh/config`` and the
agent resolve normally. Never registered in the member tool gateway;
``errorta_council`` must not import it.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shlex
import signal
import subprocess
import time
from typing import Any, Mapping, Sequence

from .types import ToolRunnerRequest, ToolRunnerResult, now_iso

_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _token(value: Any, *, what: str) -> str:
    v = str(value or "").strip()
    if not _TOKEN_RE.match(v):
        raise ValueError(f"invalid {what}: {value!r}")
    return v


def build_remote_ssh_argv(
    host: str, remote_argv: Sequence[str], *, ssh_port: int | None = None,
    ssh_username: str | None = None, ssh_key_path: str | None = None,
    ssh_bin: str = "ssh",
) -> list[str]:
    host = _token(host, what="ssh_host")
    argv = [
        ssh_bin,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
    ]
    if ssh_port:
        port = int(ssh_port)
        if not 1 <= port <= 65535:
            raise ValueError("invalid ssh_port")
        argv += ["-p", str(port)]
    if ssh_key_path:
        argv += ["-i", os.path.expanduser(str(ssh_key_path))]
    target = f"{_token(ssh_username, what='ssh_username')}@{host}" if ssh_username else host
    if not remote_argv:
        raise ValueError("empty remote argv")
    argv += [target, "--", shlex.join(str(a) for a in remote_argv)]
    return argv


class RemoteToolRunner:
    def __init__(self, *, ssh_bin: str = "ssh",
                 source_env: Mapping[str, str] | None = None) -> None:
        self._ssh_bin = ssh_bin
        self._env = dict(source_env) if source_env is not None else None

    async def run(self, request: ToolRunnerRequest) -> ToolRunnerResult:
        return await asyncio.to_thread(self.run_sync, request)

    def run_sync(self, request: ToolRunnerRequest, *,
                 stdin_path: str | None = None) -> ToolRunnerResult:
        if request.execution_location != "remote_ssh":
            return ToolRunnerResult.blocked(
                request=request, reason_code="runner_location_not_remote",
                metadata={"execution_location": request.execution_location})
        host = request.metadata.get("ssh_host")
        if not host:
            return ToolRunnerResult.blocked(request=request, reason_code="remote_host_missing")
        try:
            argv = build_remote_ssh_argv(
                str(host), request.argv,
                ssh_port=request.metadata.get("ssh_port"),
                ssh_username=request.metadata.get("ssh_username"),
                ssh_key_path=request.metadata.get("ssh_key_path"),
                ssh_bin=self._ssh_bin)
        except ValueError as exc:
            return ToolRunnerResult.blocked(
                request=request, reason_code="remote_argv_invalid",
                metadata={"detail": str(exc)})

        started_at = now_iso()
        started = time.monotonic()
        stdin_fh = open(stdin_path, "rb") if stdin_path else subprocess.DEVNULL
        try:
            proc = subprocess.Popen(  # noqa: S603 — fixed, validated argv
                argv, stdin=stdin_fh, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=self._env, start_new_session=True)
        except FileNotFoundError:
            return _fail(request, "runner_executable_not_found", started_at, started)
        except OSError:
            return _fail(request, "runner_spawn_failed", started_at, started)
        finally:
            if stdin_path:
                stdin_fh.close()  # type: ignore[union-attr]

        timed_out = False
        try:
            out, err = proc.communicate(timeout=request.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            out, err = proc.communicate()

        cap = request.max_output_bytes
        out_c, err_c = out[:cap], err[:cap]
        status = "timed_out" if timed_out else ("completed" if proc.returncode == 0 else "failed")
        reason = "runner_timeout" if timed_out else (
            "runner_nonzero_exit" if proc.returncode != 0 else None)
        out_p = out_c.decode("utf-8", errors="replace")
        err_p = err_c.decode("utf-8", errors="replace")
        return ToolRunnerResult(
            request_id=request.request_id, run_id=request.run_id,
            tool_call_id=request.tool_call_id, status=status,
            exit_code=proc.returncode, duration_ms=int((time.monotonic() - started) * 1000),
            stdout_preview=out_p, stderr_preview=err_p,
            stdout_sha256=hashlib.sha256(out_c).hexdigest(),
            stderr_sha256=hashlib.sha256(err_c).hexdigest(),
            stdout_bytes=len(out_c), stderr_bytes=len(err_c),
            reason_code=reason, log_tail=(err_p or out_p)[-800:] if reason else None,
            started_at=started_at, finished_at=now_iso(),
            metadata={"stdout_truncated": len(out) > cap, "stderr_truncated": len(err) > cap,
                      "ssh_host": str(host)})


def _fail(request: ToolRunnerRequest, reason: str, started_at: str, started: float) -> ToolRunnerResult:
    empty = hashlib.sha256(b"").hexdigest()
    return ToolRunnerResult(
        request_id=request.request_id, run_id=request.run_id,
        tool_call_id=request.tool_call_id, status="failed", exit_code=None,
        duration_ms=int((time.monotonic() - started) * 1000), stdout_preview="",
        stderr_preview="", stdout_sha256=empty, stderr_sha256=empty, stdout_bytes=0,
        stderr_bytes=0, reason_code=reason, started_at=started_at, finished_at=now_iso())


__all__ = ["RemoteToolRunner", "build_remote_ssh_argv"]
```

- [ ] **Step 4: Run tests, including the council import-lint guards**

Run: `python3 -m pytest -q tests/tools/test_remote_runner.py tests/council/test_tool_runner_local.py tests/council/test_toolgateway_slice1.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/errorta_tools/runner/remote.py python/tests/tools/
git commit -m "feat(runner): real RemoteToolRunner over a fixed ssh argv"
```

---
### Task 3: Profile schema + fail-closed validator

**Files:**
- Create: `python/errorta_liverun/__init__.py` (empty docstring module), `python/errorta_liverun/profile.py`
- Test: `python/tests/liverun/__init__.py` (empty), `python/tests/liverun/test_profile.py`

**Interfaces:**
- Produces (all frozen dataclasses in `profile.py`):
  - `ProfileError(ValueError)` with `.code: str` (e.g. `"created_by_not_operator"`).
  - `Host(ssh_host: str, ssh_port: int|None, ssh_username: str|None, ssh_key_path: str|None)`
  - `TunnelDef(id: str, host: str, reverse: tuple[tuple[int,int],...])`
  - `Check(kind: str, params: dict)` — kinds: `exit0, http, http_json, file_exists, file_mtime_newer, pgrep, pgrep_absent, tunnel_up, remote_pid_alive, all`.
  - `Probe(kind: str, params: dict)` — kinds: `http, remote_pid_alive, remote_file_mtime_advancing, remote_stdout_advancing, remote_stdout_matches, elapsed_lt_s`.
  - `Action(kind: str, params: dict)` — kinds: `local, remote, remote_signal, tunnel, tunnel_close, window_shot, http`.
  - `Step(name: str, action: Action|None, check: Check|None, timeout_s: float, evidence_literal: str|None)`
  - `WatchProbe(id: str, every_s: float, stall_after_s: float, on_stall: str, probe: Probe)`
  - `Caps(max_launches_per_hour: int, min_launch_gap_s: int, max_launches_per_day: int, max_consecutive_failed_cycles: int)` + `DEFAULT_CAPS`.
  - `Profile(name, hosts: dict[str,Host], tunnels: dict[str,TunnelDef], launch: tuple[Step,...], watch: tuple[WatchProbe,...], evidence: tuple[Step,...], teardown: tuple[Step,...], caps: Caps, ban_signals: tuple[str,...])`
  - `load_profile(path: Path, *, known_hosts_fn=default_known_hosts_check) -> Profile`; `profiles_dir() -> Path` = `errorta_home()/"liverun"/"profiles"`; `list_profiles() -> list[dict]` with `{"name", "valid": bool, "error": str|None}`.
  - `LOCAL_ARGV0_ALLOWLIST = ("osascript", "pgrep")`, `BANNED_TOKENS = ("--ignore-risk-budget", "--no-safety-plane")`, `BRAIN_REQUIRED_FLAGS = ("--max-session-seconds", "--receipt-id", "--require-live-feed")`.

- [ ] **Step 1: Write the failing tests**

```python
# python/tests/liverun/test_profile.py
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from errorta_liverun import profile as P


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


def _ok_hosts(host: str) -> bool:
    return True


def _minimal(**over) -> dict:
    doc = {
        "version": 1,
        "created_by": "operator",
        "hosts": {"box": {"ssh_host": "box"}},
        "tunnels": {},
        "launch": [
            {"name": "start", "local": {"argv": ["/bin/true"]},
             "check": {"exit0": ["/bin/true"]}, "timeout_s": 5},
        ],
        "watch": [
            {"id": "alive", "every_s": 1, "stall_after_s": 5, "on_stall": "stop",
             "probe": {"http": {"url": "http://127.0.0.1:1/state"}}},
        ],
        "evidence": [],
        "teardown": [
            {"name": "logoff", "check": {"http_json": {"url": "http://127.0.0.1:1/state",
             "path": "gameState", "not_equals": "LOGGED_IN"}}, "timeout_s": 5,
             "evidence_literal": "logoff_verified"},
        ],
        "caps": {},
        "ban_signals": ["Account is banned"],
    }
    doc.update(over)
    return doc


def _write(tmp_path: Path, doc: dict, name: str = "p") -> Path:
    d = P.profiles_dir(); d.mkdir(parents=True, exist_ok=True)
    f = d / f"{name}.yaml"
    f.write_text(yaml.safe_dump(doc)); f.chmod(0o600)
    return f


def test_minimal_profile_loads(tmp_path: Path) -> None:
    prof = P.load_profile(_write(tmp_path, _minimal()), known_hosts_fn=_ok_hosts)
    assert prof.name == "p"
    assert prof.launch[0].action.kind == "local"
    assert prof.caps == P.DEFAULT_CAPS
    assert prof.teardown[0].evidence_literal == "logoff_verified"


@pytest.mark.parametrize("mutate,code", [
    (lambda d: d.update(created_by="slack"), "created_by_not_operator"),
    (lambda d: d.update(version=2), "unsupported_version"),
    (lambda d: d.update(bogus=1), "unknown_key"),
    (lambda d: d["launch"][0].update(local={"argv": ["./jagex-play"]}), "argv0_not_absolute"),
    (lambda d: d["launch"][0].update(local={"argv": ["/bin/sh", "-c", "echo $HOME"]}), "shell_token_in_argv"),
    (lambda d: d["launch"][0].update(local={"argv": ["/bin/true", "--ignore-risk-budget"]}), "banned_token"),
    (lambda d: d.update(caps={"max_launches_per_hour": 3}), "cap_above_default"),
    (lambda d: d.update(teardown=[{"name": "x", "local": {"argv": ["/bin/true"]}, "timeout_s": 1}]), "missing_logoff_literal"),
    (lambda d: d["watch"][0].update(on_stall="explode"), "bad_on_stall"),
    (lambda d: d["launch"][0].update(remote={"host": "nope", "argv": ["true"]}, local=None), "unknown_host"),
    (lambda d: d["launch"][0].update(remote={"host": "box", "argv": [
        "python", "-m", "senditai_ng.cli", "run", "--execute"]}, local=None), "brain_flags_missing"),
])
def test_validator_rejects(tmp_path: Path, mutate, code: str) -> None:
    doc = _minimal(); mutate(doc)
    if doc["launch"][0].get("local") is None:
        doc["launch"][0].pop("local", None)
    with pytest.raises(P.ProfileError) as ei:
        P.load_profile(_write(tmp_path, doc), known_hosts_fn=_ok_hosts)
    assert ei.value.code == code


def test_gradlew_needs_absolute_cwd(tmp_path: Path) -> None:
    doc = _minimal()
    doc["launch"][0]["local"] = {"argv": ["./gradlew", "build"]}
    with pytest.raises(P.ProfileError) as ei:
        P.load_profile(_write(tmp_path, doc), known_hosts_fn=_ok_hosts)
    assert ei.value.code == "argv0_not_absolute"
    doc["launch"][0]["local"] = {"argv": ["./gradlew", "build"], "cwd": "/abs/repo"}
    P.load_profile(_write(tmp_path, doc), known_hosts_fn=_ok_hosts)


def test_brain_run_with_required_flags_passes(tmp_path: Path) -> None:
    doc = _minimal()
    doc["launch"][0] = {"name": "brain", "remote": {"host": "box", "argv": [
        "python", "-m", "senditai_ng.cli", "run", "--max-session-seconds", "3600",
        "--receipt-id", "r", "--require-live-feed"], "detach": True,
        "pidfile": "~/x.pid"}, "check": {"remote_pid_alive": {"host": "box", "pidfile": "~/x.pid"}},
        "timeout_s": 5}
    P.load_profile(_write(tmp_path, doc), known_hosts_fn=_ok_hosts)


def test_unknown_known_hosts_rejected(tmp_path: Path) -> None:
    with pytest.raises(P.ProfileError) as ei:
        P.load_profile(_write(tmp_path, _minimal()), known_hosts_fn=lambda h: False)
    assert ei.value.code == "host_unknown"


def test_symlink_and_wrong_mode_rejected(tmp_path: Path) -> None:
    f = _write(tmp_path, _minimal())
    f.chmod(0o666)
    with pytest.raises(P.ProfileError) as ei:
        P.load_profile(f, known_hosts_fn=_ok_hosts)
    assert ei.value.code == "profile_mode_insecure"
    f.chmod(0o600)
    link = f.parent / "link.yaml"; link.symlink_to(f)
    with pytest.raises(P.ProfileError) as ei:
        P.load_profile(link, known_hosts_fn=_ok_hosts)
    assert ei.value.code == "profile_is_symlink"


def test_outside_profiles_dir_rejected(tmp_path: Path) -> None:
    f = tmp_path / "elsewhere.yaml"; f.write_text(yaml.safe_dump(_minimal())); f.chmod(0o600)
    with pytest.raises(P.ProfileError) as ei:
        P.load_profile(f, known_hosts_fn=_ok_hosts)
    assert ei.value.code == "profile_outside_dir"


def test_list_profiles_reports_validity(tmp_path: Path) -> None:
    _write(tmp_path, _minimal(), "good")
    _write(tmp_path, _minimal(created_by="slack"), "bad")
    rows = {r["name"]: r for r in P.list_profiles(known_hosts_fn=_ok_hosts)}
    assert rows["good"]["valid"] is True
    assert rows["bad"]["valid"] is False and rows["bad"]["error"] == "created_by_not_operator"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest -q tests/liverun/test_profile.py`
Expected: FAIL with `ModuleNotFoundError: errorta_liverun`.

- [ ] **Step 3: Implement**

`python/errorta_liverun/__init__.py`:

```python
"""Live-run supervisor: operator-declared profiles, wall-clock watch, safe teardown."""
```

`python/errorta_liverun/profile.py`:

```python
"""Profile schema + fail-closed validator (spec §3.2)."""
from __future__ import annotations

import os
import re
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from errorta_app.paths import errorta_home

LOCAL_ARGV0_ALLOWLIST = ("osascript", "pgrep")
BANNED_TOKENS = ("--ignore-risk-budget", "--no-safety-plane")
BRAIN_REQUIRED_FLAGS = ("--max-session-seconds", "--receipt-id", "--require-live-feed")
ALLOWED_TOKENS = ("$SESSION_ID", "$RUN_ID")
_SHELL_CHARS = re.compile(r"[$`|;&<>]")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

CHECK_KINDS = {"exit0", "http", "http_json", "file_exists", "file_mtime_newer", "pgrep",
               "pgrep_absent", "tunnel_up", "remote_pid_alive", "all"}
PROBE_KINDS = {"http", "remote_pid_alive", "remote_file_mtime_advancing",
               "remote_stdout_advancing", "remote_stdout_matches", "elapsed_lt_s"}
ACTION_KINDS = {"local", "remote", "remote_signal", "tunnel", "tunnel_close", "window_shot", "http"}
TOP_KEYS = {"version", "created_by", "hosts", "tunnels", "launch", "watch", "evidence",
            "teardown", "caps", "ban_signals"}
STEP_KEYS = {"name", "check", "timeout_s", "evidence_literal"} | ACTION_KINDS


class ProfileError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True)
class Host:
    ssh_host: str
    ssh_port: int | None = None
    ssh_username: str | None = None
    ssh_key_path: str | None = None


@dataclass(frozen=True)
class TunnelDef:
    id: str
    host: str
    reverse: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class Check:
    kind: str
    params: Any


@dataclass(frozen=True)
class Probe:
    kind: str
    params: Any


@dataclass(frozen=True)
class Action:
    kind: str
    params: dict[str, Any]


@dataclass(frozen=True)
class Step:
    name: str
    action: Action | None
    check: Check | None
    timeout_s: float
    evidence_literal: str | None = None


@dataclass(frozen=True)
class WatchProbe:
    id: str
    every_s: float
    stall_after_s: float
    on_stall: str
    probe: Probe


@dataclass(frozen=True)
class Caps:
    max_launches_per_hour: int = 2
    min_launch_gap_s: int = 900
    max_launches_per_day: int = 8
    max_consecutive_failed_cycles: int = 2


DEFAULT_CAPS = Caps()


@dataclass(frozen=True)
class Profile:
    name: str
    hosts: dict[str, Host]
    tunnels: dict[str, TunnelDef]
    launch: tuple[Step, ...]
    watch: tuple[WatchProbe, ...]
    evidence: tuple[Step, ...]
    teardown: tuple[Step, ...]
    caps: Caps = DEFAULT_CAPS
    ban_signals: tuple[str, ...] = ()


def profiles_dir() -> Path:
    return errorta_home() / "liverun" / "profiles"


def default_known_hosts_check(host: str) -> bool:
    try:
        rc = subprocess.run(["ssh-keygen", "-F", host], capture_output=True, timeout=5).returncode
    except (OSError, subprocess.TimeoutExpired):
        return False
    return rc == 0


# --- validation helpers -------------------------------------------------- #

def _argv(value: Any, *, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(a, str) for a in value):
        raise ProfileError("argv_not_list_of_str", where)
    for a in value:
        if a in BANNED_TOKENS:
            raise ProfileError("banned_token", f"{where}: {a}")
        stripped = a
        for tok in ALLOWED_TOKENS:
            stripped = stripped.replace(tok, "")
        if _SHELL_CHARS.search(stripped):
            raise ProfileError("shell_token_in_argv", f"{where}: {a!r}")
    return tuple(value)


def _local_action(raw: dict[str, Any], *, where: str) -> Action:
    argv = _argv(raw.get("argv"), where=where)
    cwd = raw.get("cwd")
    a0 = argv[0]
    if a0 == "./gradlew":
        if not (isinstance(cwd, str) and os.path.isabs(cwd)):
            raise ProfileError("argv0_not_absolute", f"{where}: ./gradlew needs absolute cwd")
    elif not (os.path.isabs(a0) or a0 in LOCAL_ARGV0_ALLOWLIST):
        raise ProfileError("argv0_not_absolute", f"{where}: {a0!r}")
    if cwd is not None and not (isinstance(cwd, str) and os.path.isabs(cwd)):
        raise ProfileError("cwd_not_absolute", where)
    return Action("local", {"argv": argv, "cwd": cwd})


def _remote_action(raw: dict[str, Any], hosts: dict[str, Host], *, where: str) -> Action:
    host = raw.get("host")
    if host not in hosts:
        raise ProfileError("unknown_host", f"{where}: {host!r}")
    argv = _argv(raw.get("argv"), where=where)
    if "senditai_ng.cli" in argv and "run" in argv:
        missing = [f for f in BRAIN_REQUIRED_FLAGS if f not in argv]
        if missing:
            raise ProfileError("brain_flags_missing", f"{where}: {missing}")
    detach = bool(raw.get("detach", False))
    pidfile = raw.get("pidfile")
    if detach and not isinstance(pidfile, str):
        raise ProfileError("detach_needs_pidfile", where)
    stdin_file = raw.get("stdin_file")
    if stdin_file is not None and not (isinstance(stdin_file, str) and os.path.isabs(os.path.expanduser(stdin_file))):
        raise ProfileError("stdin_file_not_absolute", where)
    return Action("remote", {"host": host, "argv": argv, "detach": detach,
                             "pidfile": pidfile, "stdin_file": stdin_file,
                             "log": raw.get("log")})


def _action(raw: dict[str, Any], hosts: dict[str, Host], tunnels: dict[str, TunnelDef],
            *, where: str) -> Action | None:
    kinds = [k for k in ACTION_KINDS if k in raw]
    if len(kinds) > 1:
        raise ProfileError("multiple_actions", where)
    if not kinds:
        return None
    kind = kinds[0]
    body = raw[kind]
    if kind == "local":
        return _local_action(body, where=where)
    if kind == "remote":
        return _remote_action(body, hosts, where=where)
    if kind == "remote_signal":
        if body.get("host") not in hosts or not isinstance(body.get("pidfile"), str):
            raise ProfileError("bad_remote_signal", where)
        return Action(kind, {"host": body["host"], "pidfile": body["pidfile"],
                             "signal": str(body.get("signal", "TERM")),
                             "grace_s": float(body.get("grace_s", 10)),
                             "then": str(body.get("then", "KILL"))})
    if kind in ("tunnel", "tunnel_close"):
        if body not in tunnels:
            raise ProfileError("unknown_tunnel", f"{where}: {body!r}")
        return Action(kind, {"id": body})
    if kind == "window_shot":
        if not isinstance(body.get("pgrep"), str):
            raise ProfileError("bad_window_shot", where)
        return Action(kind, {"pgrep": body["pgrep"]})
    if kind == "http":
        if not isinstance(body.get("url"), str) or not body["url"].startswith("http://127.0.0.1"):
            raise ProfileError("http_not_loopback", where)
        return Action(kind, {"url": body["url"]})
    raise ProfileError("bad_action", where)


def _check(raw: Any, hosts: dict[str, Host], tunnels: dict[str, TunnelDef], *, where: str) -> Check:
    if not isinstance(raw, dict) or len(raw) != 1:
        raise ProfileError("bad_check", where)
    kind, params = next(iter(raw.items()))
    if kind not in CHECK_KINDS:
        raise ProfileError("bad_check", f"{where}: {kind}")
    if kind == "all":
        return Check(kind, tuple(_check(c, hosts, tunnels, where=where) for c in params))
    if kind == "exit0":
        params = {"argv": _argv(params, where=where)}
        if not (os.path.isabs(params["argv"][0]) or params["argv"][0] in LOCAL_ARGV0_ALLOWLIST):
            raise ProfileError("argv0_not_absolute", where)
    if kind in ("http", "http_json") and not str(params.get("url", "")).startswith("http://127.0.0.1"):
        raise ProfileError("http_not_loopback", where)
    if kind == "tunnel_up" and params not in tunnels:
        raise ProfileError("unknown_tunnel", where)
    if kind == "remote_pid_alive" and params.get("host") not in hosts:
        raise ProfileError("unknown_host", where)
    return Check(kind, params)


def _probe(raw: Any, hosts: dict[str, Host], *, where: str) -> Probe:
    if not isinstance(raw, dict) or len(raw) != 1:
        raise ProfileError("bad_probe", where)
    kind, params = next(iter(raw.items()))
    if kind not in PROBE_KINDS:
        raise ProfileError("bad_probe", f"{where}: {kind}")
    if kind.startswith("remote_"):
        if params.get("host") not in hosts:
            raise ProfileError("unknown_host", where)
        if "argv" in params:
            params = dict(params, argv=_argv(params["argv"], where=where))
    if kind == "http" and not str(params.get("url", "")).startswith("http://127.0.0.1"):
        raise ProfileError("http_not_loopback", where)
    return Probe(kind, params)


def _step(raw: Any, hosts, tunnels, *, where: str) -> Step:
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
        raise ProfileError("bad_step", where)
    extra = set(raw) - STEP_KEYS
    if extra:
        raise ProfileError("unknown_key", f"{where}: {sorted(extra)}")
    action = _action(raw, hosts, tunnels, where=where)
    check = _check(raw["check"], hosts, tunnels, where=where) if "check" in raw else None
    if action is None and check is None:
        raise ProfileError("empty_step", where)
    timeout = float(raw.get("timeout_s", 30))
    if timeout <= 0:
        raise ProfileError("bad_timeout", where)
    lit = raw.get("evidence_literal")
    if lit is not None and not _TOKEN_RE.match(str(lit)):
        raise ProfileError("bad_evidence_literal", where)
    return Step(raw["name"], action, check, timeout, lit)


def _caps(raw: Any) -> Caps:
    if raw is None:
        return DEFAULT_CAPS
    if not isinstance(raw, dict):
        raise ProfileError("bad_caps")
    values = {}
    for k in ("max_launches_per_hour", "min_launch_gap_s", "max_launches_per_day",
              "max_consecutive_failed_cycles"):
        if k in raw:
            v = int(raw[k]); d = getattr(DEFAULT_CAPS, k)
            looser = v < d if k == "min_launch_gap_s" else v > d
            if looser:
                raise ProfileError("cap_above_default", f"{k}={v} (default {d})")
            values[k] = v
    extra = set(raw) - set(values) - {"max_launches_per_hour", "min_launch_gap_s",
                                      "max_launches_per_day", "max_consecutive_failed_cycles"}
    if extra:
        raise ProfileError("unknown_key", f"caps: {sorted(extra)}")
    return Caps(**values)


def _file_guard(path: Path) -> None:
    if path.is_symlink():
        raise ProfileError("profile_is_symlink", str(path))
    try:
        path.resolve().relative_to(profiles_dir().resolve())
    except ValueError:
        raise ProfileError("profile_outside_dir", str(path)) from None
    st = path.stat()
    if st.st_uid != os.getuid():
        raise ProfileError("profile_not_owned", str(path))
    if stat.S_IMODE(st.st_mode) not in (0o600, 0o644, 0o640):
        raise ProfileError("profile_mode_insecure", oct(stat.S_IMODE(st.st_mode)))


def load_profile(path: Path, *, known_hosts_fn: Callable[[str], bool] = default_known_hosts_check) -> Profile:
    path = Path(path)
    _file_guard(path)
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ProfileError("yaml_invalid", str(exc)) from None
    if not isinstance(doc, dict):
        raise ProfileError("not_a_mapping")
    extra = set(doc) - TOP_KEYS
    if extra:
        raise ProfileError("unknown_key", str(sorted(extra)))
    if doc.get("version") != 1:
        raise ProfileError("unsupported_version", repr(doc.get("version")))
    if doc.get("created_by") != "operator":
        raise ProfileError("created_by_not_operator", repr(doc.get("created_by")))

    hosts: dict[str, Host] = {}
    for hid, h in (doc.get("hosts") or {}).items():
        if not isinstance(h, dict) or not _TOKEN_RE.match(str(h.get("ssh_host", ""))):
            raise ProfileError("bad_host", hid)
        if not known_hosts_fn(str(h["ssh_host"])):
            raise ProfileError("host_unknown", f"{hid}: {h['ssh_host']} not in known_hosts")
        hosts[str(hid)] = Host(str(h["ssh_host"]), h.get("ssh_port"), h.get("ssh_username"),
                               h.get("ssh_key_path"))

    tunnels: dict[str, TunnelDef] = {}
    for t in (doc.get("tunnels") or []):
        if not isinstance(t, dict) or t.get("host") not in hosts or not t.get("reverse"):
            raise ProfileError("bad_tunnel", str(t))
        pairs = tuple((int(p["remote_port"]), int(p["local_port"])) for p in t["reverse"])
        tunnels[str(t["id"])] = TunnelDef(str(t["id"]), str(t["host"]), pairs)

    launch = tuple(_step(s, hosts, tunnels, where=f"launch[{i}]") for i, s in enumerate(doc.get("launch") or []))
    evidence = tuple(_step(s, hosts, tunnels, where=f"evidence[{i}]") for i, s in enumerate(doc.get("evidence") or []))
    teardown = tuple(_step(s, hosts, tunnels, where=f"teardown[{i}]") for i, s in enumerate(doc.get("teardown") or []))
    if not any(s.evidence_literal == "logoff_verified" for s in teardown):
        raise ProfileError("missing_logoff_literal")

    watch: list[WatchProbe] = []
    for i, w in enumerate(doc.get("watch") or []):
        if not isinstance(w, dict) or not _TOKEN_RE.match(str(w.get("id", ""))):
            raise ProfileError("bad_watch", f"watch[{i}]")
        on_stall = str(w.get("on_stall", "stop"))
        if on_stall not in ("stop", "warn"):
            raise ProfileError("bad_on_stall", f"watch[{i}]: {on_stall}")
        watch.append(WatchProbe(str(w["id"]), float(w.get("every_s", 30)),
                                float(w.get("stall_after_s", 0)), on_stall,
                                _probe(w.get("probe"), hosts, where=f"watch[{i}]")))

    bans = tuple(str(b) for b in (doc.get("ban_signals") or []))
    for b in bans:
        try:
            re.compile(b)
        except re.error:
            raise ProfileError("bad_ban_regex", b) from None

    return Profile(name=path.stem, hosts=hosts, tunnels=tunnels, launch=launch,
                   watch=tuple(watch), evidence=evidence, teardown=teardown,
                   caps=_caps(doc.get("caps")), ban_signals=bans)


def list_profiles(*, known_hosts_fn: Callable[[str], bool] = default_known_hosts_check) -> list[dict[str, Any]]:
    d = profiles_dir()
    if not d.is_dir():
        return []
    rows = []
    for f in sorted(d.glob("*.yaml")):
        try:
            load_profile(f, known_hosts_fn=known_hosts_fn)
            rows.append({"name": f.stem, "valid": True, "error": None})
        except ProfileError as exc:
            rows.append({"name": f.stem, "valid": False, "error": exc.code})
    return rows


__all__ = ["Profile", "ProfileError", "Step", "WatchProbe", "Caps", "DEFAULT_CAPS", "Host",
           "TunnelDef", "Check", "Probe", "Action", "load_profile", "list_profiles",
           "profiles_dir", "BANNED_TOKENS", "LOCAL_ARGV0_ALLOWLIST", "BRAIN_REQUIRED_FLAGS"]
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest -q tests/liverun/test_profile.py`
Expected: PASS. If the `shell_token_in_argv` case fires before `argv0_not_absolute` for the `./jagex-play` row, that is fine only if the test's expected code matches — the `./jagex-play` row has no shell chars, so it must report `argv0_not_absolute`.

- [ ] **Step 5: Commit**

```bash
git add python/errorta_liverun/ python/tests/liverun/
git commit -m "feat(liverun): profile schema and fail-closed validator"
```

---
### Task 4: Run state persistence, event log, and launch-cap ledger

**Files:**
- Create: `python/errorta_liverun/state.py`
- Test: `python/tests/liverun/test_state.py`

**Interfaces:**
- Produces:
  - `PHASES = ("idle","launching","watching","stopping","stopped","failed","paused_awaiting_human","lost_on_restart")`, `TERMINAL_PHASES = {"stopped","failed","paused_awaiting_human","lost_on_restart"}`.
  - `@dataclass RunState(run_id, profile_name, project_id: str|None, phase: str, reason: str|None, session_id: str, step_index: int, started_at: str, launched_at: str|None, ended_at: str|None, owned_pgids: list[int], owned_remote_pidfiles: list[dict], owned_tunnels: list[str], probe_last_ok: dict[str,str], probe_last_value: dict[str,str], literals: dict[str,bool], evidence_dir: str)` with `to_dict()/from_dict()`.
  - `class RunStore(root: Path|None=None)` — `root` defaults to `errorta_home()/"liverun"/"runs"`; `new_run_id() -> str`; `save(state)` atomic (tmp+rename); `load(run_id) -> RunState|None`; `list_non_terminal() -> list[RunState]`; `append_event(run_id, kind: str, detail: dict) -> int` (returns `seq`, writes `events.jsonl` lines `{"seq","at","kind","detail"}`); `events(run_id, after_seq=0) -> list[dict]`; `evidence_dir(run_id) -> Path`.
  - `class LaunchLedger(path: Path|None=None)` — `path` defaults to `errorta_home()/"liverun"/"launches.jsonl"`; `record(profile_name, run_id, at: float)`; `record_outcome(run_id, failed: bool)`; `check(profile_name, caps: Caps, now: float) -> str|None` returning `None` (ok) or a code: `cap_hourly`, `cap_daily`, `cap_gap`, `cap_consecutive_failures`.

- [ ] **Step 1: Write the failing tests**

```python
# python/tests/liverun/test_state.py
from __future__ import annotations

import json
from pathlib import Path

import pytest

from errorta_liverun.profile import Caps
from errorta_liverun.state import LaunchLedger, RunState, RunStore


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


def _state(store: RunStore, phase: str = "launching") -> RunState:
    rid = store.new_run_id()
    return RunState(run_id=rid, profile_name="p", project_id=None, phase=phase, reason=None,
                    session_id="s-" + rid, step_index=0, started_at="2026-08-21T00:00:00Z",
                    launched_at=None, ended_at=None, owned_pgids=[], owned_remote_pidfiles=[],
                    owned_tunnels=[], probe_last_ok={}, probe_last_value={}, literals={},
                    evidence_dir=str(store.evidence_dir(rid)))


def test_save_load_roundtrip_is_atomic(tmp_path: Path) -> None:
    store = RunStore()
    st = _state(store)
    st.owned_pgids.append(4242)
    store.save(st)
    assert store.load(st.run_id) == st
    assert not list((tmp_path / "liverun" / "runs" / st.run_id).glob("*.tmp"))


def test_list_non_terminal_filters(tmp_path: Path) -> None:
    store = RunStore()
    a = _state(store, "watching"); b = _state(store, "stopped"); c = _state(store, "lost_on_restart")
    for s in (a, b, c):
        store.save(s)
    assert [s.run_id for s in store.list_non_terminal()] == [a.run_id]


def test_events_are_monotonic_and_readable(tmp_path: Path) -> None:
    store = RunStore(); st = _state(store); store.save(st)
    assert store.append_event(st.run_id, "phase", {"to": "launching"}) == 1
    assert store.append_event(st.run_id, "step", {"name": "x", "ok": True}) == 2
    evs = store.events(st.run_id, after_seq=1)
    assert [e["seq"] for e in evs] == [2] and evs[0]["kind"] == "step"
    raw = (tmp_path / "liverun" / "runs" / st.run_id / "events.jsonl").read_text().splitlines()
    assert json.loads(raw[0])["seq"] == 1


def test_launch_ledger_caps(tmp_path: Path) -> None:
    led = LaunchLedger()
    caps = Caps(max_launches_per_hour=2, min_launch_gap_s=900, max_launches_per_day=3,
                max_consecutive_failed_cycles=2)
    t0 = 1_000_000.0
    assert led.check("p", caps, t0) is None
    led.record("p", "r1", t0)
    assert led.check("p", caps, t0 + 10) == "cap_gap"
    led.record("p", "r2", t0 + 1000)
    assert led.check("p", caps, t0 + 2000) == "cap_hourly"
    assert led.check("p", caps, t0 + 3700) is None
    led.record("p", "r3", t0 + 3700)
    assert led.check("p", caps, t0 + 7300) == "cap_daily"
    # consecutive failures
    led2 = LaunchLedger(tmp_path / "other.jsonl")
    led2.record("p", "a", t0); led2.record_outcome("a", failed=True)
    led2.record("p", "b", t0 + 4000); led2.record_outcome("b", failed=True)
    assert led2.check("p", caps, t0 + 90_000) == "cap_consecutive_failures"
    led2.record("p", "c", t0 + 90_000); led2.record_outcome("c", failed=False)
    assert led2.check("p", caps, t0 + 200_000) is None


def test_launch_ledger_is_per_profile(tmp_path: Path) -> None:
    led = LaunchLedger(); caps = Caps()
    led.record("p", "r1", 0.0)
    assert led.check("q", caps, 1.0) is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest -q tests/liverun/test_state.py`
Expected: FAIL with `ModuleNotFoundError: errorta_liverun.state`.

- [ ] **Step 3: Implement**

```python
# python/errorta_liverun/state.py
"""Persisted live-run state: atomic state.json, append-only events.jsonl,
and the launch ledger that enforces caps across restarts (spec §3.6)."""
from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from errorta_app.paths import errorta_home

from .profile import Caps

PHASES = ("idle", "launching", "watching", "stopping", "stopped", "failed",
          "paused_awaiting_human", "lost_on_restart")
TERMINAL_PHASES = {"stopped", "failed", "paused_awaiting_human", "lost_on_restart"}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class RunState:
    run_id: str
    profile_name: str
    project_id: str | None
    phase: str
    reason: str | None
    session_id: str
    step_index: int
    started_at: str
    launched_at: str | None
    ended_at: str | None
    owned_pgids: list[int] = field(default_factory=list)
    owned_remote_pidfiles: list[dict[str, str]] = field(default_factory=list)  # {"host","pidfile"}
    owned_tunnels: list[str] = field(default_factory=list)
    probe_last_ok: dict[str, str] = field(default_factory=dict)      # probe id -> iso
    probe_last_value: dict[str, str] = field(default_factory=dict)   # probe id -> last observed
    literals: dict[str, bool] = field(default_factory=dict)          # e.g. logoff_verified
    evidence_dir: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RunState":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


class RunStore:
    def __init__(self, root: Path | None = None) -> None:
        self._root = Path(root) if root else errorta_home() / "liverun" / "runs"

    def new_run_id(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)

    def _dir(self, run_id: str) -> Path:
        return self._root / run_id

    def evidence_dir(self, run_id: str) -> Path:
        return self._dir(run_id) / "evidence"

    def save(self, state: RunState) -> None:
        _atomic_write(self._dir(state.run_id) / "state.json",
                      json.dumps(state.to_dict(), indent=1, sort_keys=True))

    def load(self, run_id: str) -> RunState | None:
        p = self._dir(run_id) / "state.json"
        if not p.is_file():
            return None
        try:
            return RunState.from_dict(json.loads(p.read_text()))
        except (ValueError, TypeError):
            return None

    def list_non_terminal(self) -> list[RunState]:
        if not self._root.is_dir():
            return []
        out = []
        for d in sorted(self._root.iterdir()):
            st = self.load(d.name)
            if st is not None and st.phase not in TERMINAL_PHASES:
                out.append(st)
        return out

    def append_event(self, run_id: str, kind: str, detail: dict[str, Any]) -> int:
        p = self._dir(run_id) / "events.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        seq = len(self.events(run_id)) + 1
        with p.open("a") as fh:
            fh.write(json.dumps({"seq": seq, "at": now_iso(), "kind": kind, "detail": detail}) + "\n")
        return seq

    def events(self, run_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        p = self._dir(run_id) / "events.jsonl"
        if not p.is_file():
            return []
        out = []
        for line in p.read_text().splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if int(ev.get("seq", 0)) > after_seq:
                out.append(ev)
        return out


class LaunchLedger:
    """Append-only record of launches + outcomes; caps are computed over it so a
    burst survives a sidecar restart (the brain's own budget reasoning)."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path else errorta_home() / "liverun" / "launches.jsonl"

    def _rows(self) -> list[dict[str, Any]]:
        if not self._path.is_file():
            return []
        rows = []
        for line in self._path.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows

    def _append(self, row: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")

    def record(self, profile_name: str, run_id: str, at: float) -> None:
        self._append({"kind": "launch", "profile": profile_name, "run_id": run_id, "at": at})

    def record_outcome(self, run_id: str, *, failed: bool) -> None:
        self._append({"kind": "outcome", "run_id": run_id, "failed": bool(failed)})

    def check(self, profile_name: str, caps: Caps, now: float) -> str | None:
        rows = self._rows()
        launches = [r for r in rows if r.get("kind") == "launch" and r.get("profile") == profile_name]
        if not launches:
            return None
        last = max(float(r["at"]) for r in launches)
        if now - last < caps.min_launch_gap_s:
            return "cap_gap"
        if sum(1 for r in launches if now - float(r["at"]) < 3600) >= caps.max_launches_per_hour:
            return "cap_hourly"
        if sum(1 for r in launches if now - float(r["at"]) < 86400) >= caps.max_launches_per_day:
            return "cap_daily"
        outcomes = {r["run_id"]: bool(r["failed"]) for r in rows if r.get("kind") == "outcome"}
        streak = 0
        for r in sorted(launches, key=lambda r: float(r["at"]), reverse=True):
            failed = outcomes.get(r["run_id"])
            if failed is None:
                continue
            if not failed:
                break
            streak += 1
        if streak >= caps.max_consecutive_failed_cycles:
            return "cap_consecutive_failures"
        return None


__all__ = ["PHASES", "TERMINAL_PHASES", "RunState", "RunStore", "LaunchLedger", "now_iso"]
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest -q tests/liverun/test_state.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/errorta_liverun/state.py python/tests/liverun/test_state.py
git commit -m "feat(liverun): persisted run state, event log, launch-cap ledger"
```

---

### Task 5: Step and probe primitives

**Files:**
- Create: `python/errorta_liverun/steps.py`
- Test: `python/tests/liverun/test_steps.py`

**Interfaces:**
- Consumes: `Profile`, `Step`, `Action`, `Check`, `Probe`, `Host`, `TunnelDef` (Task 3); `RemoteToolRunner`, `build_remote_ssh_argv` (Task 2); `TunnelManager`, `TunnelSpec` (Task 1); `capture_app_window` (`errorta_tools/runner/preview.py:159`); `redact_log_line` (`errorta_council/coding/runtime_process.py:295`).
- Produces:
  - `@dataclass StepResult(ok: bool, started_at: str, ended_at: str, exit_code: int|None, stdout_tail: str, stderr_tail: str, evidence_refs: list[str], timed_out: bool, detail: str)`
  - `@dataclass Ctx(profile: Profile, run_id: str, session_id: str, evidence_dir: Path, tunnels: TunnelManager, remote: RemoteToolRunner, owned_pgids: list[int], owned_remote_pidfiles: list[dict], owned_tunnels: list[str], last_values: dict[str,str], launched_monotonic: float|None, clock=time.monotonic)`
  - `substitute(argv: tuple[str,...], ctx) -> tuple[str,...]` (only `$SESSION_ID`, `$RUN_ID`)
  - `run_action(action: Action, ctx, *, timeout_s: float) -> StepResult`
  - `run_check(check: Check, ctx, *, step_start: float) -> bool`
  - `run_probe(probe: Probe, ctx) -> bool` (updates `ctx.last_values` for "advancing" probes)
  - `tunnel_spec_for(tdef: TunnelDef, ctx) -> TunnelSpec`
  - `ArgvIdentityError(RuntimeError)` raised when an argv is not one of the profile's validated argvs (after substitution) — the spec §3.3 invariant.

- [ ] **Step 1: Write the failing tests**

```python
# python/tests/liverun/test_steps.py
from __future__ import annotations

import http.server
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from errorta_liverun import profile as P
from errorta_liverun import steps as S
from errorta_tools.runner.remote import RemoteToolRunner
from errorta_tunnels.manager import TunnelManager


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_ssh(tmp_path: Path) -> Path:
    script = tmp_path / "ssh"
    script.write_text("#!/bin/sh\nwhile [ \"$1\" != \"--\" ]; do shift; done; shift\neval \"$1\"\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


@pytest.fixture
def http_state():
    body = {"value": b'{"gameState":"LOGGED_IN","seq":1}'}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.end_headers(); self.wfile.write(body["value"])
        def log_message(self, *a): pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    yield f"http://127.0.0.1:{srv.server_port}/state", body
    srv.shutdown()


def _profile(tmp_path: Path, local_argvs=(), remote_argvs=()) -> P.Profile:
    hosts = {"box": P.Host("localhost")}
    launch = [P.Step("l", P.Action("local", {"argv": tuple(a), "cwd": None}), None, 5) for a in local_argvs]
    launch += [P.Step("r", P.Action("remote", {"host": "box", "argv": tuple(a), "detach": False,
                                                "pidfile": None, "stdin_file": None, "log": None}), None, 5)
               for a in remote_argvs]
    return P.Profile("p", hosts, {}, tuple(launch), (), (), (), P.DEFAULT_CAPS, ())


def _ctx(tmp_path: Path, prof: P.Profile, ssh: Path | None = None) -> S.Ctx:
    return S.Ctx(profile=prof, run_id="RUN1", session_id="SESS1", evidence_dir=tmp_path / "ev",
                 tunnels=TunnelManager(), remote=RemoteToolRunner(ssh_bin=str(ssh) if ssh else "ssh"),
                 owned_pgids=[], owned_remote_pidfiles=[], owned_tunnels=[], last_values={},
                 launched_monotonic=None)


def test_substitute_only_known_tokens() -> None:
    prof = _profile(Path("/"), local_argvs=[["/bin/echo", "$SESSION_ID", "$RUN_ID", "$HOME"]])
    ctx = _ctx(Path("/tmp"), prof)
    assert S.substitute(("/bin/echo", "$SESSION_ID", "$RUN_ID", "$HOME"), ctx) == \
        ("/bin/echo", "SESS1", "RUN1", "$HOME")


def test_local_action_runs_and_records_pgid(tmp_path: Path) -> None:
    prof = _profile(tmp_path, local_argvs=[["/bin/echo", "hello $RUN_ID"]])
    ctx = _ctx(tmp_path, prof)
    res = S.run_action(prof.launch[0].action, ctx, timeout_s=5)
    assert res.ok and "hello RUN1" in res.stdout_tail
    assert ctx.owned_pgids  # recorded before/at spawn


def test_local_action_rejects_foreign_argv(tmp_path: Path) -> None:
    prof = _profile(tmp_path, local_argvs=[["/bin/echo", "ok"]])
    ctx = _ctx(tmp_path, prof)
    with pytest.raises(S.ArgvIdentityError):
        S.run_action(P.Action("local", {"argv": ("/bin/echo", "pwned"), "cwd": None}), ctx, timeout_s=5)


def test_local_timeout_kills_group(tmp_path: Path) -> None:
    marker = tmp_path / "gc"
    argv = ["/bin/sh", "-c", f"sleep 30 & echo $! > {marker}; wait"]
    prof = _profile(tmp_path, local_argvs=[argv])
    ctx = _ctx(tmp_path, prof)
    res = S.run_action(prof.launch[0].action, ctx, timeout_s=0.5)
    assert res.timed_out and not res.ok
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


def test_remote_action_via_fake_ssh(tmp_path: Path, fake_ssh: Path) -> None:
    prof = _profile(tmp_path, remote_argvs=[["echo", "remote $SESSION_ID"]])
    ctx = _ctx(tmp_path, prof, fake_ssh)
    res = S.run_action(prof.launch[0].action, ctx, timeout_s=5)
    assert res.ok and "remote SESS1" in res.stdout_tail


def test_remote_detach_records_pidfile(tmp_path: Path, fake_ssh: Path) -> None:
    pidfile = str(tmp_path / "b.pid"); log = str(tmp_path / "b.log")
    action = P.Action("remote", {"host": "box", "argv": ("sleep", "5"), "detach": True,
                                 "pidfile": pidfile, "stdin_file": None, "log": log})
    prof = P.Profile("p", {"box": P.Host("localhost")}, {}, (P.Step("b", action, None, 5),), (), (), (), P.DEFAULT_CAPS, ())
    ctx = _ctx(tmp_path, prof, fake_ssh)
    res = S.run_action(action, ctx, timeout_s=5)
    assert res.ok
    assert ctx.owned_remote_pidfiles == [{"host": "box", "pidfile": pidfile}]
    time.sleep(0.3)
    pid = int(Path(pidfile).read_text().strip())
    os.kill(pid, 0)  # alive
    assert S.run_check(P.Check("remote_pid_alive", {"host": "box", "pidfile": pidfile}), ctx, step_start=0.0)
    sig = P.Action("remote_signal", {"host": "box", "pidfile": pidfile, "signal": "TERM", "grace_s": 1, "then": "KILL"})
    assert S.run_action(sig, ctx, timeout_s=5).ok
    time.sleep(0.3)
    assert not S.run_check(P.Check("remote_pid_alive", {"host": "box", "pidfile": pidfile}), ctx, step_start=0.0)


def test_http_checks_and_probe(tmp_path: Path, http_state) -> None:
    url, body = http_state
    ctx = _ctx(tmp_path, _profile(tmp_path))
    assert S.run_check(P.Check("http", {"url": url, "expect_status": 200}), ctx, step_start=0.0)
    assert not S.run_check(P.Check("http_json", {"url": url, "path": "gameState", "not_equals": "LOGGED_IN"}), ctx, step_start=0.0)
    body["value"] = b'{"gameState":"LOGIN_SCREEN"}'
    assert S.run_check(P.Check("http_json", {"url": url, "path": "gameState", "not_equals": "LOGGED_IN"}), ctx, step_start=0.0)
    assert S.run_probe(P.Probe("http", {"url": url}), ctx)
    assert not S.run_probe(P.Probe("http", {"url": "http://127.0.0.1:1/x"}), ctx)


def test_file_checks(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _profile(tmp_path))
    f = tmp_path / "jar"; f.write_text("x")
    assert S.run_check(P.Check("file_exists", str(f)), ctx, step_start=0.0)
    assert not S.run_check(P.Check("file_exists", str(tmp_path / "nope")), ctx, step_start=0.0)
    assert S.run_check(P.Check("file_mtime_newer", {"path": str(f), "than": "step_start"}), ctx, step_start=f.stat().st_mtime - 1)
    assert not S.run_check(P.Check("file_mtime_newer", {"path": str(f), "than": "step_start"}), ctx, step_start=f.stat().st_mtime + 1)


def test_advancing_probes(tmp_path: Path, fake_ssh: Path) -> None:
    ctx = _ctx(tmp_path, _profile(tmp_path, remote_argvs=[["cat", str(tmp_path / "seq")]]), fake_ssh)
    seqf = tmp_path / "seq"; seqf.write_text("1")
    probe = P.Probe("remote_stdout_advancing", {"host": "box", "argv": ("cat", str(seqf))})
    assert S.run_probe(probe, ctx) is True      # first observation counts as ok
    assert S.run_probe(probe, ctx) is False     # unchanged
    seqf.write_text("2")
    assert S.run_probe(probe, ctx) is True
    m = P.Probe("remote_stdout_matches", {"host": "box", "argv": ("cat", str(seqf)), "regex": r"^2$"})
    assert S.run_probe(m, ctx) is True


def test_elapsed_probe(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _profile(tmp_path))
    ctx.launched_monotonic = ctx.clock() - 100
    assert S.run_probe(P.Probe("elapsed_lt_s", 200), ctx)
    assert not S.run_probe(P.Probe("elapsed_lt_s", 50), ctx)


def test_all_check_and_pgrep(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _profile(tmp_path))
    assert S.run_check(P.Check("pgrep_absent", "definitely-not-a-real-process-name-xyz"), ctx, step_start=0.0)
    assert S.run_check(P.Check("all", (P.Check("pgrep_absent", "nope-xyz"), P.Check("file_exists", "/"))), ctx, step_start=0.0)
    assert not S.run_check(P.Check("all", (P.Check("file_exists", "/"), P.Check("file_exists", "/nope"))), ctx, step_start=0.0)


def test_evidence_redacted(tmp_path: Path) -> None:
    prof = _profile(tmp_path, local_argvs=[["/bin/echo", "token=sk-abcdefghijklmnopqrstuvwxyz0123456789"]])
    ctx = _ctx(tmp_path, prof)
    res = S.run_action(prof.launch[0].action, ctx, timeout_s=5)
    assert "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in res.stdout_tail
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest -q tests/liverun/test_steps.py`
Expected: FAIL with `ModuleNotFoundError: errorta_liverun.steps`.

- [ ] **Step 3: Implement**

```python
# python/errorta_liverun/steps.py
"""Step / check / probe primitives (spec §3.3). Deterministic mechanism only."""
from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from errorta_council.coding.runtime_process import redact_log_line
from errorta_tools.runner.preview import capture_app_window
from errorta_tools.runner.remote import RemoteToolRunner
from errorta_tools.runner.types import ToolRunnerRequest, now_iso
from errorta_tunnels.manager import TunnelManager, TunnelSpec

from .profile import Action, Check, Probe, Profile, TunnelDef

_TAIL = 2000


class ArgvIdentityError(RuntimeError):
    """An argv that is not byte-identical to one the validated profile declared."""


@dataclass
class StepResult:
    ok: bool
    started_at: str
    ended_at: str
    exit_code: int | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    timed_out: bool = False
    detail: str = ""


@dataclass
class Ctx:
    profile: Profile
    run_id: str
    session_id: str
    evidence_dir: Path
    tunnels: TunnelManager
    remote: RemoteToolRunner
    owned_pgids: list[int]
    owned_remote_pidfiles: list[dict[str, str]]
    owned_tunnels: list[str]
    last_values: dict[str, str]
    launched_monotonic: float | None
    clock: Callable[[], float] = time.monotonic


def _redact(text: str) -> str:
    return "\n".join(redact_log_line(line) for line in text[-_TAIL:].splitlines())


def substitute(argv: tuple[str, ...], ctx: Ctx) -> tuple[str, ...]:
    return tuple(a.replace("$SESSION_ID", ctx.session_id).replace("$RUN_ID", ctx.run_id) for a in argv)


def _declared_argvs(profile: Profile) -> set[tuple[str, ...]]:
    out: set[tuple[str, ...]] = set()
    for step in (*profile.launch, *profile.evidence, *profile.teardown):
        if step.action and "argv" in step.action.params:
            out.add(tuple(step.action.params["argv"]))
        if step.check:
            out |= _check_argvs(step.check)
    for w in profile.watch:
        if "argv" in (w.probe.params if isinstance(w.probe.params, dict) else {}):
            out.add(tuple(w.probe.params["argv"]))
    return out


def _check_argvs(check: Check) -> set[tuple[str, ...]]:
    if check.kind == "all":
        s: set[tuple[str, ...]] = set()
        for c in check.params:
            s |= _check_argvs(c)
        return s
    if check.kind == "exit0":
        return {tuple(check.params["argv"])}
    return set()


def _guard(argv: tuple[str, ...], ctx: Ctx) -> tuple[str, ...]:
    if tuple(argv) not in _declared_argvs(ctx.profile):
        raise ArgvIdentityError(f"argv not declared by profile: {argv[:1]}")
    return substitute(tuple(argv), ctx)


def _host(ctx: Ctx, host_id: str) -> dict[str, Any]:
    h = ctx.profile.hosts[host_id]
    return {"ssh_host": h.ssh_host, "ssh_port": h.ssh_port, "ssh_username": h.ssh_username,
            "ssh_key_path": h.ssh_key_path}


def _remote_request(ctx: Ctx, host_id: str, argv: tuple[str, ...], timeout_s: float) -> ToolRunnerRequest:
    return ToolRunnerRequest(
        request_id=secrets.token_hex(4), run_id=ctx.run_id, tool_call_id="liverun",
        argv=argv, workspace_root=str(ctx.evidence_dir), execution_location="remote_ssh",
        timeout_seconds=timeout_s, metadata=_host(ctx, host_id))


def _remote(ctx: Ctx, host_id: str, argv: tuple[str, ...], timeout_s: float = 20,
            *, stdin_path: str | None = None):
    return ctx.remote.run_sync(_remote_request(ctx, host_id, argv, timeout_s), stdin_path=stdin_path)


# --- actions ------------------------------------------------------------- #

def _run_local(params: dict[str, Any], ctx: Ctx, timeout_s: float) -> StepResult:
    argv = _guard(tuple(params["argv"]), ctx)
    started_at, t0 = now_iso(), time.monotonic()
    ctx.evidence_dir.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.Popen(  # noqa: S603 — validated, profile-declared argv
            list(argv), cwd=params.get("cwd") or None, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    except OSError as exc:
        return StepResult(False, started_at, now_iso(), detail=f"spawn failed: {exc}")
    ctx.owned_pgids.append(proc.pid)
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _killpg(proc.pid)
        out, err = proc.communicate()
    finally:
        if proc.pid in ctx.owned_pgids and proc.poll() is not None:
            ctx.owned_pgids.remove(proc.pid)
    return StepResult(ok=(proc.returncode == 0 and not timed_out), started_at=started_at,
                      ended_at=now_iso(), exit_code=proc.returncode,
                      stdout_tail=_redact(out.decode("utf-8", "replace")),
                      stderr_tail=_redact(err.decode("utf-8", "replace")), timed_out=timed_out)


def _killpg(pid: int, sig: int = signal.SIGKILL) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def _run_remote(params: dict[str, Any], ctx: Ctx, timeout_s: float) -> StepResult:
    argv = _guard(tuple(params["argv"]), ctx)
    host_id = params["host"]
    started_at = now_iso()
    stdin_path = os.path.expanduser(params["stdin_file"]) if params.get("stdin_file") else None
    if params.get("detach"):
        pidfile = params["pidfile"]
        log = params.get("log") or (pidfile + ".log")
        # Built server-side from the validated argv; never from a string.
        wrapped = ("sh", "-c",
                   f"setsid nohup {shlex.join(argv)} > {shlex.quote(log)} 2>&1 < /dev/stdin & "
                   f"echo $! > {shlex.quote(pidfile)}")
        res = ctx.remote.run_sync(_remote_request(ctx, host_id, wrapped, timeout_s), stdin_path=stdin_path)
        if res.status == "completed":
            ctx.owned_remote_pidfiles.append({"host": host_id, "pidfile": pidfile})
    else:
        res = ctx.remote.run_sync(_remote_request(ctx, host_id, argv, timeout_s), stdin_path=stdin_path)
    return StepResult(ok=res.status == "completed", started_at=started_at, ended_at=now_iso(),
                      exit_code=res.exit_code, stdout_tail=_redact(res.stdout_preview),
                      stderr_tail=_redact(res.stderr_preview), timed_out=res.status == "timed_out",
                      detail=res.reason_code or "")


def _run_remote_signal(params: dict[str, Any], ctx: Ctx, timeout_s: float) -> StepResult:
    started_at = now_iso()
    host, pidfile = params["host"], params["pidfile"]
    sig, then, grace = params["signal"], params["then"], float(params["grace_s"])
    script = (f"p=$(cat {shlex.quote(pidfile)} 2>/dev/null) || exit 0; "
              f"kill -{sig} $p 2>/dev/null || exit 0; "
              f"for i in $(seq 1 {int(grace * 10)}); do kill -0 $p 2>/dev/null || exit 0; sleep 0.1; done; "
              f"kill -{then} $p 2>/dev/null; exit 0")
    res = _remote(ctx, host, ("sh", "-c", script), timeout_s=grace + timeout_s)
    return StepResult(ok=res.status == "completed", started_at=started_at, ended_at=now_iso(),
                      exit_code=res.exit_code, stderr_tail=_redact(res.stderr_preview))


def tunnel_spec_for(tdef: TunnelDef, ctx: Ctx) -> TunnelSpec:
    h = ctx.profile.hosts[tdef.host]
    return TunnelSpec(ssh_host=h.ssh_host, remote_port=0, ssh_port=h.ssh_port,
                      ssh_username=h.ssh_username, ssh_key_path=h.ssh_key_path,
                      reverse_forwards=tdef.reverse)


def _run_tunnel(params: dict[str, Any], ctx: Ctx, timeout_s: float, *, close: bool) -> StepResult:
    started_at = now_iso()
    tdef = ctx.profile.tunnels[params["id"]]
    spec = tunnel_spec_for(tdef, ctx)
    if close:
        ctx.tunnels.close(spec)
        if params["id"] in ctx.owned_tunnels:
            ctx.owned_tunnels.remove(params["id"])
        return StepResult(True, started_at, now_iso())
    ctx.tunnels.ensure(spec, wait=False)
    ctx.owned_tunnels.append(params["id"])
    return StepResult(True, started_at, now_iso())


def _run_window_shot(params: dict[str, Any], ctx: Ctx, timeout_s: float) -> StepResult:
    started_at = now_iso()
    pids = _pgrep(params["pgrep"])
    ctx.evidence_dir.mkdir(parents=True, exist_ok=True)
    out = ctx.evidence_dir / f"window-{int(time.time())}.png"
    ok = bool(pids) and capture_app_window(pids=set(pids), out_path=out)
    return StepResult(ok, started_at, now_iso(), evidence_refs=[str(out)] if ok else [],
                      detail="" if ok else "no window captured")


def _run_http_action(params: dict[str, Any], ctx: Ctx, timeout_s: float) -> StepResult:
    started_at = now_iso()
    body = _http_get(params["url"], timeout_s)
    ctx.evidence_dir.mkdir(parents=True, exist_ok=True)
    out = ctx.evidence_dir / f"http-{int(time.time())}.txt"
    out.write_text(_redact(body or ""))
    return StepResult(body is not None, started_at, now_iso(), evidence_refs=[str(out)])


def run_action(action: Action, ctx: Ctx, *, timeout_s: float) -> StepResult:
    p = action.params
    if action.kind == "local":
        return _run_local(p, ctx, timeout_s)
    if action.kind == "remote":
        return _run_remote(p, ctx, timeout_s)
    if action.kind == "remote_signal":
        return _run_remote_signal(p, ctx, timeout_s)
    if action.kind == "tunnel":
        return _run_tunnel(p, ctx, timeout_s, close=False)
    if action.kind == "tunnel_close":
        return _run_tunnel(p, ctx, timeout_s, close=True)
    if action.kind == "window_shot":
        return _run_window_shot(p, ctx, timeout_s)
    if action.kind == "http":
        return _run_http_action(p, ctx, timeout_s)
    raise ValueError(action.kind)


# --- checks -------------------------------------------------------------- #

def _http_get(url: str, timeout_s: float = 5) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as r:  # noqa: S310 — loopback only (validator)
            return r.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None


def _http_status(url: str, timeout_s: float = 5) -> int | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as r:  # noqa: S310
            return r.status
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        return e.code
    except Exception:  # noqa: BLE001
        return None


def _pgrep(pattern: str) -> list[int]:
    try:
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [int(x) for x in out.split() if x.isdigit() and int(x) != os.getpid()]


def _remote_pid_alive(ctx: Ctx, host: str, pidfile: str) -> bool:
    res = _remote(ctx, host, ("sh", "-c", f"kill -0 $(cat {shlex.quote(pidfile)})"), 15)
    return res.status == "completed"


def run_check(check: Check, ctx: Ctx, *, step_start: float) -> bool:
    k, p = check.kind, check.params
    if k == "all":
        return all(run_check(c, ctx, step_start=step_start) for c in p)
    if k == "exit0":
        argv = _guard(tuple(p["argv"]), ctx)
        try:
            return subprocess.run(list(argv), capture_output=True, timeout=30).returncode == 0  # noqa: S603
        except (OSError, subprocess.TimeoutExpired):
            return False
    if k == "http":
        return _http_status(p["url"]) == int(p.get("expect_status", 200))
    if k == "http_json":
        body = _http_get(p["url"])
        if body is None:
            return False
        try:
            val: Any = json.loads(body)
            for part in str(p["path"]).split("."):
                val = val[part]
        except (ValueError, KeyError, TypeError):
            return False
        if "equals" in p:
            return val == p["equals"]
        return val != p.get("not_equals")
    if k == "file_exists":
        return Path(os.path.expanduser(str(p))).exists()
    if k == "file_mtime_newer":
        path = Path(os.path.expanduser(p["path"]))
        return path.exists() and path.stat().st_mtime > step_start
    if k == "pgrep":
        return bool(_pgrep(str(p)))
    if k == "pgrep_absent":
        return not _pgrep(str(p))
    if k == "tunnel_up":
        st = ctx.tunnels.status_for(tunnel_spec_for(ctx.profile.tunnels[p], ctx))
        return bool(st) and st["state"] == "up"
    if k == "remote_pid_alive":
        return _remote_pid_alive(ctx, p["host"], p["pidfile"])
    raise ValueError(k)


# --- probes -------------------------------------------------------------- #

def run_probe(probe: Probe, ctx: Ctx) -> bool:
    k, p = probe.kind, probe.params
    if k == "http":
        return _http_status(p["url"]) == 200
    if k == "remote_pid_alive":
        return _remote_pid_alive(ctx, p["host"], p["pidfile"])
    if k == "elapsed_lt_s":
        if ctx.launched_monotonic is None:
            return True
        return (ctx.clock() - ctx.launched_monotonic) < float(p)
    if k == "remote_file_mtime_advancing":
        res = _remote(ctx, p["host"], ("sh", "-c", f"stat -c %Y {shlex.quote(p['path'])} 2>/dev/null || stat -f %m {shlex.quote(p['path'])}"), 15)
        return _advancing(ctx, f"mtime:{p['path']}", res.stdout_preview.strip() if res.status == "completed" else None)
    if k == "remote_stdout_advancing":
        argv = _guard(tuple(p["argv"]), ctx)
        res = _remote(ctx, p["host"], argv, 15)
        return _advancing(ctx, f"stdout:{argv}", res.stdout_preview.strip() if res.status == "completed" else None)
    if k == "remote_stdout_matches":
        argv = _guard(tuple(p["argv"]), ctx)
        res = _remote(ctx, p["host"], argv, 15)
        return res.status == "completed" and re.search(p["regex"], res.stdout_preview, re.M) is not None
    raise ValueError(k)


def _advancing(ctx: Ctx, key: str, value: str | None) -> bool:
    if value is None or value == "":
        return False
    prev = ctx.last_values.get(key)
    ctx.last_values[key] = value
    return prev is None or prev != value


__all__ = ["ArgvIdentityError", "Ctx", "StepResult", "run_action", "run_check", "run_probe",
           "substitute", "tunnel_spec_for"]
```

Note on `_run_remote` detach + `stdin`: `< /dev/stdin` hands the ssh session's stdin (the token file) to the detached process. If the fake-ssh test for detach hangs because `eval` keeps stdin open, change the wrapper to read the token into a remote temp file first: `t=$(mktemp); cat > "$t"; setsid nohup ... < "$t" &; rm -f "$t"` — and keep that form, it is safer.

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest -q tests/liverun/test_steps.py`
Expected: PASS. `test_evidence_redacted` depends on `redact_log_line` recognising `sk-…`; if it does not, assert on a `KEY=VALUE` secret shape it does redact (read `errorta_diagnostics/redact.py` and pick a pattern it covers) — do not weaken the redaction.

- [ ] **Step 5: Commit**

```bash
git add python/errorta_liverun/steps.py python/tests/liverun/test_steps.py
git commit -m "feat(liverun): step, check, and probe primitives with argv identity guard"
```

---
### Task 6: Supervisor state machine and manager

**Files:**
- Create: `python/errorta_liverun/supervisor.py`
- Test: `python/tests/liverun/test_supervisor.py`

**Interfaces:**
- Consumes: Tasks 3–5 (`Profile`, `RunStore`, `RunState`, `LaunchLedger`, `Ctx`, `run_action`, `run_check`, `run_probe`, `TERMINAL_PHASES`).
- Produces:
  - `class Supervisor(profile, *, store: RunStore, ledger: LaunchLedger, tunnels, remote, project_id: str|None=None, clock=time.monotonic, sleep=None, run_action=steps.run_action, run_check=steps.run_check, run_probe=steps.run_probe, wall=time.time)` — `sleep(seconds)` defaults to `self._stop.wait`; tests inject a fake clock and a no-op sleep.
    - `.state: RunState` (live), `.start() -> RunState` (spawns the thread; raises `LiveRunRefused(code)` when caps say no), `.run_once_blocking()` (the whole loop synchronously — for tests), `.stop(reason: str="operator_stop")`, `.join(timeout)`.
  - `class LiveRunRefused(RuntimeError)` with `.code`.
  - `class LiveRunManager(store=None, ledger=None, tunnels=None, remote=None)` — `start(profile_name: str, *, project_id: str|None) -> dict` (returns `{"status": "started", "run_id"}` or `{"status": "refused", "reason"}` — loads the profile via `profile.load_profile`, refuses if a run is active for the profile or project), `stop(profile_name=None, project_id=None, reason="operator_stop") -> dict`, `status(project_id=None) -> dict` (phase, run_id, elapsed_s, probes: `{id: {"last_ok_age_s": float|None}}`, caps headroom, literals), `resume(profile_name) -> dict` (clears a `paused_awaiting_human` marker file `ERRORTA_HOME/liverun/paused/<profile>`), `teardown_all()`.
  - Module singleton `live_run_manager = LiveRunManager()`.
  - Event kinds written via `store.append_event`: `phase`, `launch_step`, `probe_warn`, `stall`, `evidence`, `teardown_step`, `literals`, `caps`, `ban_signal`, `refused`.

- [ ] **Step 1: Write the failing tests**

```python
# python/tests/liverun/test_supervisor.py
from __future__ import annotations

from pathlib import Path

import pytest

from errorta_liverun import profile as P
from errorta_liverun.state import LaunchLedger, RunStore
from errorta_liverun.steps import StepResult
from errorta_liverun.supervisor import LiveRunRefused, Supervisor


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0
    def __call__(self) -> float:
        return self.t
    def sleep(self, s: float) -> None:
        self.t += s


def _ok(*a, **k) -> StepResult:
    return StepResult(True, "t", "t")


def _profile(*, launch_ok=True, warn=False) -> P.Profile:
    act = P.Action("local", {"argv": ("/bin/true",), "cwd": None})
    launch = (P.Step("one", act, P.Check("file_exists", "/"), 5),)
    watch = (P.WatchProbe("alive", 10, 30, "warn" if warn else "stop", P.Probe("http", {"url": "http://127.0.0.1:1/"})),
             P.WatchProbe("clock", 10, 0, "stop", P.Probe("elapsed_lt_s", 10_000)))
    teardown = (P.Step("logoff", None, P.Check("file_exists", "/"), 5, "logoff_verified"),
                P.Step("quit", act, None, 5))
    evidence = (P.Step("ev", act, None, 5),)
    return P.Profile("p", {}, {}, launch, watch, evidence, teardown, P.DEFAULT_CAPS, ("Account is banned",))


def _sup(prof, clock, *, probe, check=None, action=None) -> Supervisor:
    return Supervisor(prof, store=RunStore(), ledger=LaunchLedger(), tunnels=None, remote=None,
                      clock=clock, sleep=clock.sleep, wall=clock,
                      run_action=action or _ok, run_check=check or (lambda c, ctx, step_start: True),
                      run_probe=probe)


def test_happy_launch_then_stall_tears_down_with_literal() -> None:
    clock = FakeClock()
    probe_ok = {"v": True}
    sup = _sup(_profile(), clock, probe=lambda p, ctx: probe_ok["v"] if p.kind == "http" else True)
    sup.start(blocking=False)
    # drive synchronously: a few healthy ticks, then the probe dies
    sup._tick(); clock.sleep(5); sup._tick()
    assert sup.state.phase == "watching"
    probe_ok["v"] = False
    for _ in range(5):
        clock.sleep(10); sup._tick()
    assert sup.state.phase == "stopped"
    assert sup.state.reason == "stall:alive"
    assert sup.state.literals == {"logoff_verified": True}
    kinds = [e["kind"] for e in sup.store.events(sup.state.run_id)]
    assert "evidence" in kinds and "teardown_step" in kinds and kinds[-1] == "phase"
    assert clock.t - 1000.0 >= 30  # stall_after honoured, not iteration-counted


def test_warn_probe_posts_once_and_keeps_watching() -> None:
    clock = FakeClock()
    sup = _sup(_profile(warn=True), clock, probe=lambda p, ctx: p.kind != "http")
    sup.start(blocking=False)
    for _ in range(8):
        clock.sleep(10); sup._tick()
    assert sup.state.phase == "watching"
    warns = [e for e in sup.store.events(sup.state.run_id) if e["kind"] == "probe_warn"]
    assert len(warns) == 1


def test_launch_step_failure_tears_down() -> None:
    clock = FakeClock()
    sup = _sup(_profile(), clock, probe=lambda p, ctx: True,
               check=lambda c, ctx, step_start: c.kind != "file_exists" or ctx.run_id == "never")
    sup.start(blocking=False)
    for _ in range(5):
        sup._tick()
    assert sup.state.phase == "failed"
    assert sup.state.reason.startswith("launch_step_failed:one")
    assert sup.state.literals.get("logoff_verified") is False


def test_missing_logoff_literal_is_reported_absent() -> None:
    clock = FakeClock()
    def check(c, ctx, step_start):
        return False  # logoff never verified
    sup = _sup(_profile(), clock, probe=lambda p, ctx: True, check=check)
    sup.start(blocking=False)
    sup._tick()  # launch step check fails -> failed path
    for _ in range(3):
        sup._tick()
    lits = [e for e in sup.store.events(sup.state.run_id) if e["kind"] == "literals"][-1]
    assert lits["detail"]["logoff_verified"] == "ABSENT"


def test_ban_signal_in_evidence_pauses() -> None:
    clock = FakeClock()
    def action(a, ctx, timeout_s):
        return StepResult(True, "t", "t", stdout_tail="Login failed: Account is banned")
    sup = _sup(_profile(), clock, probe=lambda p, ctx: p.kind != "http", action=action)
    sup.start(blocking=False)
    for _ in range(8):
        clock.sleep(10); sup._tick()
    assert sup.state.phase == "paused_awaiting_human"
    assert any(e["kind"] == "ban_signal" for e in sup.store.events(sup.state.run_id))


def test_caps_refuse_second_launch_inside_gap() -> None:
    clock = FakeClock()
    a = _sup(_profile(), clock, probe=lambda p, ctx: True)
    a.start(blocking=False); a.stop("operator_stop"); a._tick(); a._tick()
    assert a.state.phase == "stopped"
    b = _sup(_profile(), clock, probe=lambda p, ctx: True)
    with pytest.raises(LiveRunRefused) as ei:
        b.start(blocking=False)
    assert ei.value.code == "cap_gap"


def test_stop_during_launch_interrupts_and_tears_down() -> None:
    clock = FakeClock()
    calls = []
    def action(a, ctx, timeout_s):
        calls.append(ctx.run_id)
        return _ok()
    prof = _profile()
    prof = P.Profile(prof.name, prof.hosts, prof.tunnels, prof.launch * 3, prof.watch, prof.evidence,
                     prof.teardown, prof.caps, prof.ban_signals)
    sup = _sup(prof, clock, probe=lambda p, ctx: True, action=action)
    sup.start(blocking=False)
    sup._tick()           # step 0
    sup.stop("operator_stop")
    for _ in range(4):
        sup._tick()
    assert sup.state.phase == "stopped" and sup.state.reason == "operator_stop"
    assert sup.state.step_index < 3


def test_brain_refusal_rc3_counts_failed_cycle() -> None:
    clock = FakeClock()
    def action(a, ctx, timeout_s):
        return StepResult(False, "t", "t", exit_code=3, stderr_tail="REFUSED: risk budget")
    sup = _sup(_profile(), clock, probe=lambda p, ctx: True, action=action)
    sup.start(blocking=False)
    for _ in range(4):
        sup._tick()
    assert sup.state.phase == "failed" and "refused" in sup.state.reason
    rows = sup.ledger._rows()
    assert any(r.get("kind") == "outcome" and r["failed"] for r in rows)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest -q tests/liverun/test_supervisor.py`
Expected: FAIL with `ModuleNotFoundError: errorta_liverun.supervisor`.

- [ ] **Step 3: Implement**

```python
# python/errorta_liverun/supervisor.py
"""The live-run state machine (spec §3.6). One daemon thread per run.

idle -> launching(step i) -> watching -> stopping(reason) -> stopped
                                                          -> paused_awaiting_human
step failed ----------------------------------------------> failed
"""
from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from errorta_app.paths import errorta_home

from . import profile as _profile
from . import steps as _steps
from .state import TERMINAL_PHASES, LaunchLedger, RunState, RunStore, now_iso

_LOG = logging.getLogger("errorta.liverun")
_CHECK_POLL_S = 2.0
_TICK_S = 1.0


class LiveRunRefused(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def paused_marker(profile_name: str) -> Path:
    return errorta_home() / "liverun" / "paused" / profile_name


class Supervisor:
    def __init__(self, profile: _profile.Profile, *, store: RunStore, ledger: LaunchLedger,
                 tunnels: Any, remote: Any, project_id: str | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Any] | None = None,
                 wall: Callable[[], float] = time.time,
                 run_action=_steps.run_action, run_check=_steps.run_check,
                 run_probe=_steps.run_probe) -> None:
        self.profile = profile
        self.store = store
        self.ledger = ledger
        self._clock = clock
        self._wall = wall
        self._stop = threading.Event()
        self._sleep = sleep or (lambda s: self._stop.wait(s))
        self._run_action, self._run_check, self._run_probe = run_action, run_check, run_probe
        self._thread: threading.Thread | None = None
        self._stop_reason: str | None = None
        self._warned: set[str] = set()
        self._probe_next: dict[str, float] = {}
        self._probe_last_ok: dict[str, float] = {}
        self._step_started: float | None = None
        rid = store.new_run_id()
        self.state = RunState(
            run_id=rid, profile_name=profile.name, project_id=project_id, phase="idle",
            reason=None, session_id=f"lr-{rid}", step_index=0, started_at=now_iso(),
            launched_at=None, ended_at=None, evidence_dir=str(store.evidence_dir(rid)))
        self.ctx = _steps.Ctx(
            profile=profile, run_id=rid, session_id=self.state.session_id,
            evidence_dir=Path(self.state.evidence_dir), tunnels=tunnels, remote=remote,
            owned_pgids=self.state.owned_pgids, owned_remote_pidfiles=self.state.owned_remote_pidfiles,
            owned_tunnels=self.state.owned_tunnels, last_values=self.state.probe_last_value,
            launched_monotonic=None, clock=clock)

    # -- lifecycle --------------------------------------------------------- #
    def start(self, *, blocking: bool = False) -> RunState:
        if paused_marker(self.profile.name).exists():
            raise LiveRunRefused("paused_awaiting_human", "resume_live_run required")
        code = self.ledger.check(self.profile.name, self.profile.caps, self._wall())
        if code:
            self._event("refused", {"code": code})
            raise LiveRunRefused(code)
        self.ledger.record(self.profile.name, self.state.run_id, self._wall())
        self._set_phase("launching")
        if blocking:
            self.run_once_blocking()
        elif self._thread is None:
            self._thread = threading.Thread(target=self.run_once_blocking, daemon=True,
                                            name=f"liverun-{self.state.run_id}")
            self._thread.start()
        return self.state

    def run_once_blocking(self) -> None:
        try:
            while self.state.phase not in TERMINAL_PHASES:
                self._tick()
                if self.state.phase in ("launching", "watching"):
                    self._sleep(_TICK_S)
        except Exception as exc:  # noqa: BLE001 — the supervisor must never die silently
            _LOG.exception("liverun %s crashed", self.state.run_id)
            self._stop_reason = f"supervisor_error:{type(exc).__name__}"
            try:
                self._do_stopping()
            except Exception:  # noqa: BLE001
                _LOG.exception("liverun %s teardown after crash failed", self.state.run_id)
                self._finish("failed", self._stop_reason)

    def stop(self, reason: str = "operator_stop") -> None:
        self._stop_reason = self._stop_reason or reason
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # -- one tick of the machine (public for tests) ------------------------ #
    def _tick(self) -> None:
        ph = self.state.phase
        if ph in TERMINAL_PHASES:
            return
        if self._stop.is_set() and ph in ("launching", "watching"):
            self._do_stopping()
            return
        if ph == "launching":
            self._tick_launch()
        elif ph == "watching":
            self._tick_watch()
        elif ph == "stopping":
            self._do_stopping()

    def _tick_launch(self) -> None:
        steps = self.profile.launch
        i = self.state.step_index
        if i >= len(steps):
            self.state.launched_at = now_iso()
            self.ctx.launched_monotonic = self._clock()
            now = self._clock()
            for w in self.profile.watch:
                self._probe_last_ok[w.id] = now
                self._probe_next[w.id] = now
            self._set_phase("watching")
            return
        step = steps[i]
        if self._step_started is None:
            self._step_started = self._clock()
            if step.action is not None:
                res = self._run_action(step.action, self.ctx, timeout_s=step.timeout_s)
                self._event("launch_step", {"name": step.name, "ok": res.ok, "exit_code": res.exit_code,
                                            "stdout": res.stdout_tail[-400:], "stderr": res.stderr_tail[-400:]})
                if self._scan_ban(res.stdout_tail + "\n" + res.stderr_tail):
                    return
                if not res.ok:
                    reason = f"launch_step_failed:{step.name}"
                    if res.exit_code == 3:
                        reason += ":refused"
                    self._stop_reason = reason
                    self._do_stopping(final="failed")
                    return
        # poll the check
        if step.check is None or self._run_check(step.check, self.ctx, step_start=self._step_started):
            if step.check is not None:
                self._event("launch_step", {"name": step.name, "check": "passed"})
            self.state.step_index += 1
            self._step_started = None
            self._save()
            return
        if self._clock() - self._step_started > step.timeout_s:
            self._event("launch_step", {"name": step.name, "check": "timeout"})
            self._stop_reason = f"launch_step_failed:{step.name}:check_timeout"
            self._do_stopping(final="failed")
            return
        self._sleep(_CHECK_POLL_S)

    def _tick_watch(self) -> None:
        now = self._clock()
        for w in self.profile.watch:
            if now < self._probe_next.get(w.id, 0):
                continue
            self._probe_next[w.id] = now + w.every_s
            ok = False
            try:
                ok = bool(self._run_probe(w.probe, self.ctx))
            except Exception as exc:  # noqa: BLE001
                self._event("probe_error", {"id": w.id, "error": type(exc).__name__})
            if ok:
                self._probe_last_ok[w.id] = now
                self.state.probe_last_ok[w.id] = now_iso()
                self._warned.discard(w.id)
                continue
            stalled_for = now - self._probe_last_ok.get(w.id, now)
            if stalled_for < w.stall_after_s:
                continue
            if w.on_stall == "warn":
                if w.id not in self._warned:
                    self._warned.add(w.id)
                    self._event("probe_warn", {"id": w.id, "stalled_s": stalled_for})
                continue
            self._event("stall", {"id": w.id, "stalled_s": stalled_for})
            self._stop_reason = f"stall:{w.id}"
            self._do_stopping()
            return
        self._save()

    # -- stopping: evidence, teardown, literals ---------------------------- #
    def _do_stopping(self, *, final: str = "stopped") -> None:
        reason = self._stop_reason or "unknown"
        self._set_phase("stopping", reason)
        texts: list[str] = []
        for step in self.profile.evidence:
            try:
                res = self._run_action(step.action, self.ctx, timeout_s=step.timeout_s) if step.action else None
            except Exception as exc:  # noqa: BLE001
                res = _steps.StepResult(False, now_iso(), now_iso(), detail=type(exc).__name__)
            if res is not None:
                texts.append(res.stdout_tail + "\n" + res.stderr_tail)
                self._event("evidence", {"id": step.name, "ok": res.ok, "refs": res.evidence_refs,
                                         "detail": res.detail})
        for step in self.profile.teardown:
            ok = True
            started = self._clock()
            if step.action is not None:
                try:
                    res = self._run_action(step.action, self.ctx, timeout_s=step.timeout_s)
                    ok = res.ok
                    texts.append(res.stdout_tail + "\n" + res.stderr_tail)
                except Exception as exc:  # noqa: BLE001
                    ok = False
                    self._event("teardown_step", {"name": step.name, "error": type(exc).__name__})
            if step.check is not None:
                ok = False
                while self._clock() - started <= step.timeout_s:
                    try:
                        if self._run_check(step.check, self.ctx, step_start=started):
                            ok = True
                            break
                    except Exception:  # noqa: BLE001
                        pass
                    self._sleep(_CHECK_POLL_S)
            if step.evidence_literal:
                self.state.literals[step.evidence_literal] = bool(ok)
            self._event("teardown_step", {"name": step.name, "ok": ok,
                                          "literal": step.evidence_literal})
        self._kill_owned()
        literals = {"logoff_verified": self.state.literals.get("logoff_verified", False)}
        self._event("literals", {k: ("PRESENT" if v else "ABSENT") for k, v in literals.items()})
        banned = self._scan_ban("\n".join(texts), finish=False)
        failed = final == "failed" or reason.startswith("stall") or reason.startswith("supervisor_error")
        self.ledger.record_outcome(self.state.run_id, failed=failed)
        if banned:
            self._pause("ban_signal")
        elif self.ledger.check(self.profile.name, self.profile.caps, self._wall()) == "cap_consecutive_failures":
            self._event("caps", {"code": "cap_consecutive_failures"})
            self._pause("cap_consecutive_failures")
        else:
            self._finish(final, reason)

    def _kill_owned(self) -> None:
        for pgid in list(self.state.owned_pgids):
            _steps._killpg(pgid)
            self.state.owned_pgids.remove(pgid)
        for tid in list(self.state.owned_tunnels):
            try:
                self.ctx.tunnels.close(_steps.tunnel_spec_for(self.profile.tunnels[tid], self.ctx))
            except Exception:  # noqa: BLE001
                pass
            self.state.owned_tunnels.remove(tid)

    def _scan_ban(self, text: str, *, finish: bool = True) -> bool:
        for pat in self.profile.ban_signals:
            m = re.search(pat, text or "")
            if m:
                self._event("ban_signal", {"pattern": pat})
                if finish:
                    self._stop_reason = self._stop_reason or "ban_signal"
                    self._do_stopping()
                return True
        return False

    def _pause(self, why: str) -> None:
        marker = paused_marker(self.profile.name)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{why} {now_iso()} {self.state.run_id}\n")
        self._finish("paused_awaiting_human", why)

    # -- bookkeeping ------------------------------------------------------- #
    def _set_phase(self, phase: str, reason: str | None = None) -> None:
        self.state.phase = phase
        if reason is not None:
            self.state.reason = reason
        self._save()
        self._event("phase", {"to": phase, "reason": reason})

    def _finish(self, phase: str, reason: str | None) -> None:
        self.state.ended_at = now_iso()
        self._set_phase(phase, reason)

    def _save(self) -> None:
        self.store.save(self.state)

    def _event(self, kind: str, detail: dict[str, Any]) -> int:
        return self.store.append_event(self.state.run_id, kind, detail)


class LiveRunManager:
    """Registry of supervisors; the seam Slack and the server reach through."""

    def __init__(self, *, store: RunStore | None = None, ledger: LaunchLedger | None = None,
                 tunnels: Any = None, remote: Any = None,
                 load_profile=None) -> None:
        self._store, self._ledger = store, ledger
        self._tunnels, self._remote = tunnels, remote
        self._load_profile = load_profile or _profile.load_profile
        self._lock = threading.Lock()
        self._runs: dict[str, Supervisor] = {}  # profile name -> supervisor

    def _deps(self) -> tuple[RunStore, LaunchLedger, Any, Any]:
        if self._store is None:
            self._store = RunStore()
        if self._ledger is None:
            self._ledger = LaunchLedger()
        if self._tunnels is None:
            from errorta_tunnels import tunnel_manager
            self._tunnels = tunnel_manager
        if self._remote is None:
            from errorta_tools.runner.remote import RemoteToolRunner
            self._remote = RemoteToolRunner()
        return self._store, self._ledger, self._tunnels, self._remote

    def _active(self) -> dict[str, Supervisor]:
        return {k: s for k, s in self._runs.items() if s.state.phase not in TERMINAL_PHASES}

    def start(self, profile_name: str, *, project_id: str | None = None) -> dict[str, Any]:
        store, ledger, tunnels, remote = self._deps()
        try:
            prof = self._load_profile(_profile.profiles_dir() / f"{profile_name}.yaml")
        except (_profile.ProfileError, OSError) as exc:
            return {"status": "refused", "reason": f"profile_invalid:{getattr(exc, 'code', exc)}"}
        with self._lock:
            active = self._active()
            if profile_name in active:
                return {"status": "refused", "reason": "already_running", "run_id": active[profile_name].state.run_id}
            if project_id and any(s.state.project_id == project_id for s in active.values()):
                return {"status": "refused", "reason": "project_has_live_run"}
            sup = Supervisor(prof, store=store, ledger=ledger, tunnels=tunnels, remote=remote,
                             project_id=project_id)
            try:
                sup.start()
            except LiveRunRefused as exc:
                return {"status": "refused", "reason": exc.code}
            self._runs[profile_name] = sup
        return {"status": "started", "run_id": sup.state.run_id}

    def _find(self, profile_name: str | None, project_id: str | None) -> Supervisor | None:
        for name, s in self._active().items():
            if profile_name and name == profile_name:
                return s
            if project_id and s.state.project_id == project_id:
                return s
        return None

    def stop(self, *, profile_name: str | None = None, project_id: str | None = None,
             reason: str = "operator_stop") -> dict[str, Any]:
        sup = self._find(profile_name, project_id)
        if sup is None:
            return {"status": "empty"}
        sup.stop(reason)
        return {"status": "stopping", "run_id": sup.state.run_id}

    def status(self, *, profile_name: str | None = None, project_id: str | None = None) -> dict[str, Any]:
        sup = self._find(profile_name, project_id)
        if sup is None:
            last = None
            if self._runs:
                last = max(self._runs.values(), key=lambda s: s.state.started_at).state
            return {"status": "empty", "last": last.to_dict() if last else None}
        st = sup.state
        now = sup._clock()
        probes = {w.id: {"last_ok_age_s": (now - sup._probe_last_ok[w.id]) if w.id in sup._probe_last_ok else None,
                         "on_stall": w.on_stall, "stall_after_s": w.stall_after_s}
                  for w in sup.profile.watch}
        return {"status": "live", "run_id": st.run_id, "profile": st.profile_name, "phase": st.phase,
                "reason": st.reason, "step_index": st.step_index,
                "elapsed_s": (now - sup.ctx.launched_monotonic) if sup.ctx.launched_monotonic else None,
                "probes": probes, "literals": dict(st.literals)}

    def resume(self, profile_name: str) -> dict[str, Any]:
        marker = paused_marker(profile_name)
        if not marker.exists():
            return {"status": "empty"}
        marker.unlink()
        return {"status": "resumed"}

    def teardown_all(self) -> None:
        for sup in list(self._active().values()):
            sup.stop("sidecar_shutdown")
        for sup in list(self._active().values()):
            sup.join(timeout=60)


live_run_manager = LiveRunManager()

__all__ = ["Supervisor", "LiveRunManager", "LiveRunRefused", "live_run_manager", "paused_marker"]
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest -q tests/liverun/test_supervisor.py`
Expected: PASS. If `test_happy_launch_then_stall_tears_down_with_literal` needs more ticks than 5 to cross `stall_after_s=30` (each tick advances the fake clock only through `_sleep` calls), raise the loop count rather than lowering `stall_after_s` — the assertion on elapsed wall-clock is the point of the test.

- [ ] **Step 5: Commit**

```bash
git add python/errorta_liverun/supervisor.py python/tests/liverun/test_supervisor.py
git commit -m "feat(liverun): wall-clock supervisor state machine and manager"
```

---

### Task 7: Boot recovery and sidecar wiring

**Files:**
- Create: `python/errorta_liverun/recovery.py`
- Modify: `python/errorta_app/server.py` (after the F157 boot reap block, ~`:296-325`; and the lifespan `finally` next to `_tunnels.teardown()`, ~`:484-497`)
- Test: `python/tests/liverun/test_recovery.py`

**Interfaces:**
- Consumes: `RunStore`, `RunState`, `TERMINAL_PHASES` (Task 4); `Supervisor` internals `_kill_owned` semantics (Task 6); `load_profile` (Task 3).
- Produces: `recover_on_boot(*, store: RunStore|None=None, tunnels=None, remote=None, load_profile=None, run_action=steps.run_action, run_check=steps.run_check) -> list[str]` (run ids marked `lost_on_restart`). For each non-terminal run it: loads the profile (if the profile is now invalid it still kills owned pgids/pidfiles/tunnels), runs the profile's `teardown` steps via a throwaway `Supervisor` bound to the persisted state, kills every owned pgid, signals every owned remote pidfile (`TERM` then `KILL`), closes owned tunnels, records literals, appends events, and saves `phase="lost_on_restart"`.

- [ ] **Step 1: Write the failing test**

```python
# python/tests/liverun/test_recovery.py
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from errorta_liverun import profile as P
from errorta_liverun.recovery import recover_on_boot
from errorta_liverun.state import RunState, RunStore
from errorta_liverun.steps import StepResult


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


def _prof() -> P.Profile:
    td = (P.Step("logoff", None, P.Check("file_exists", "/"), 1, "logoff_verified"),)
    return P.Profile("p", {}, {}, (), (), (), td, P.DEFAULT_CAPS, ())


def test_recovery_tears_down_and_marks_lost(tmp_path: Path) -> None:
    store = RunStore()
    child = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    rid = store.new_run_id()
    st = RunState(run_id=rid, profile_name="p", project_id="proj", phase="watching", reason=None,
                  session_id="s", step_index=1, started_at="t", launched_at="t", ended_at=None,
                  owned_pgids=[child.pid], evidence_dir=str(store.evidence_dir(rid)))
    store.save(st)
    done = store.new_run_id()
    store.save(RunState(run_id=done, profile_name="p", project_id=None, phase="stopped", reason="x",
                        session_id="s", step_index=0, started_at="t", launched_at=None, ended_at="t"))

    lost = recover_on_boot(store=store, load_profile=lambda path: _prof(),
                           run_action=lambda a, ctx, timeout_s: StepResult(True, "t", "t"),
                           run_check=lambda c, ctx, step_start: True)
    assert lost == [rid]
    time.sleep(0.3)
    assert child.poll() is not None
    after = store.load(rid)
    assert after.phase == "lost_on_restart" and after.literals == {"logoff_verified": True}
    assert after.owned_pgids == []
    kinds = [e["kind"] for e in store.events(rid)]
    assert "teardown_step" in kinds and "literals" in kinds
    assert store.load(done).phase == "stopped"


def test_recovery_with_invalid_profile_still_kills(tmp_path: Path) -> None:
    store = RunStore()
    child = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    rid = store.new_run_id()
    store.save(RunState(run_id=rid, profile_name="gone", project_id=None, phase="launching", reason=None,
                        session_id="s", step_index=0, started_at="t", launched_at=None, ended_at=None,
                        owned_pgids=[child.pid]))
    def bad(path):
        raise P.ProfileError("profile_outside_dir")
    assert recover_on_boot(store=store, load_profile=bad) == [rid]
    time.sleep(0.3)
    assert child.poll() is not None
    after = store.load(rid)
    assert after.phase == "lost_on_restart"
    lits = [e for e in store.events(rid) if e["kind"] == "literals"][-1]
    assert lits["detail"]["logoff_verified"] == "ABSENT"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest -q tests/liverun/test_recovery.py`
Expected: FAIL with `ModuleNotFoundError: errorta_liverun.recovery`.

- [ ] **Step 3: Implement**

```python
# python/errorta_liverun/recovery.py
"""Boot reconcile: any non-terminal live run is torn down, never resumed (spec §3.6, F-H)."""
from __future__ import annotations

import logging
import shlex
from pathlib import Path
from typing import Any, Callable

from . import profile as _profile
from . import steps as _steps
from .state import RunStore, now_iso
from .supervisor import Supervisor

_LOG = logging.getLogger("errorta.liverun")


def _signal_remote_pidfiles(ctx: _steps.Ctx, state) -> None:
    for ref in list(state.owned_remote_pidfiles):
        try:
            _steps.run_action(_profile.Action("remote_signal", {
                "host": ref["host"], "pidfile": ref["pidfile"], "signal": "TERM",
                "grace_s": 10, "then": "KILL"}), ctx, timeout_s=20)
        except Exception:  # noqa: BLE001
            _LOG.warning("recovery: remote signal failed for %s", ref, exc_info=True)
        state.owned_remote_pidfiles.remove(ref)


def recover_on_boot(*, store: RunStore | None = None, tunnels: Any = None, remote: Any = None,
                    load_profile: Callable[[Path], _profile.Profile] | None = None,
                    run_action=_steps.run_action, run_check=_steps.run_check) -> list[str]:
    store = store or RunStore()
    load = load_profile or _profile.load_profile
    if tunnels is None:
        from errorta_tunnels import tunnel_manager
        tunnels = tunnel_manager
    if remote is None:
        from errorta_tools.runner.remote import RemoteToolRunner
        remote = RemoteToolRunner()
    lost: list[str] = []
    for state in store.list_non_terminal():
        store.append_event(state.run_id, "phase", {"to": "recovering", "reason": "sidecar_restart"})
        prof: _profile.Profile | None
        try:
            prof = load(_profile.profiles_dir() / f"{state.profile_name}.yaml")
        except Exception as exc:  # noqa: BLE001
            prof = None
            store.append_event(state.run_id, "recovery", {"profile": "unavailable", "error": str(exc)[:200]})
        if prof is None:
            prof = _profile.Profile(state.profile_name, {}, {}, (), (), (), (), _profile.DEFAULT_CAPS, ())
        sup = Supervisor(prof, store=store, ledger=_NullLedger(), tunnels=tunnels, remote=remote,
                         project_id=state.project_id, run_action=run_action, run_check=run_check)
        # Bind the throwaway supervisor to the PERSISTED state so owned resources are reachable.
        sup.state = state
        sup.ctx = _steps.Ctx(
            profile=prof, run_id=state.run_id, session_id=state.session_id,
            evidence_dir=Path(state.evidence_dir or store.evidence_dir(state.run_id)),
            tunnels=tunnels, remote=remote, owned_pgids=state.owned_pgids,
            owned_remote_pidfiles=state.owned_remote_pidfiles, owned_tunnels=state.owned_tunnels,
            last_values=state.probe_last_value, launched_monotonic=None)
        sup._stop_reason = "sidecar_restart"
        try:
            sup._do_stopping(final="lost_on_restart")
        except Exception:  # noqa: BLE001
            _LOG.exception("recovery: teardown failed for %s", state.run_id)
        _signal_remote_pidfiles(sup.ctx, state)
        sup._kill_owned()
        if state.phase != "lost_on_restart":
            state.ended_at = now_iso()
            state.phase = "lost_on_restart"
            state.reason = "sidecar_restart"
            store.save(state)
            store.append_event(state.run_id, "phase", {"to": "lost_on_restart", "reason": "sidecar_restart"})
        lost.append(state.run_id)
    return lost


class _NullLedger:
    """Recovery never counts as a launch; outcomes are recorded against the real ledger
    by the original run's caps check on the next start."""
    def record_outcome(self, run_id: str, *, failed: bool) -> None:
        from .state import LaunchLedger
        LaunchLedger().record_outcome(run_id, failed=failed)
    def check(self, *a, **k):
        return None


__all__ = ["recover_on_boot"]
```

Note: `_do_stopping(final="lost_on_restart")` relies on `Supervisor._finish` accepting any phase string; `_pause` may override to `paused_awaiting_human` if evidence contains a ban signal — that is intended. Also `_do_stopping` calls `self.ledger.check(...)` for the consecutive-failures cap; `_NullLedger.check` returns `None` so recovery never pauses for caps.

In `server.py`, add after the F157 boot reap block:

```python
    # Live-run boot recovery: tear down every non-terminal live run from a prior
    # sidecar (never resume — spec 2026-08-21 live-run-supervisor §3.6).
    try:
        from errorta_liverun.recovery import recover_on_boot as _liverun_recover

        def _liverun_boot() -> None:
            try:
                lost = _liverun_recover()
                if lost:
                    logging.getLogger("errorta.liverun").info(
                        "liverun recovery: %d run(s) torn down as lost_on_restart", len(lost))
            except Exception as exc:  # pragma: no cover - defensive only
                logging.getLogger("errorta.liverun").warning("liverun recovery failed: %s", exc)

        app.state.liverun_recovery_thread = _threading.Thread(
            target=_liverun_boot, name="liverun-boot-recovery", daemon=True)
        app.state.liverun_recovery_thread.start()
    except Exception as exc:  # pragma: no cover - defensive only
        logging.getLogger("errorta.liverun").debug("liverun recovery not started: %s", exc)
```

and in the lifespan `finally`, **before** `_tunnels.teardown()`:

```python
        # Live runs: stop + full teardown (logoff) before the tunnels they use go away.
        try:
            from errorta_liverun.supervisor import live_run_manager as _liverun

            _liverun.teardown_all()
        except Exception:  # pragma: no cover - defensive
            pass
```

- [ ] **Step 4: Run tests + server import smoke**

Run: `python3 -m pytest -q tests/liverun tests/test_server*.py 2>/dev/null || python3 -m pytest -q tests/liverun && python3 -c "import errorta_app.server"`
Expected: PASS; server imports cleanly.

- [ ] **Step 5: Commit**

```bash
git add python/errorta_liverun/recovery.py python/tests/liverun/test_recovery.py python/errorta_app/server.py
git commit -m "feat(liverun): boot recovery tears down lost runs; sidecar lifespan wiring"
```

---
### Task 8: Slack verbs, `ToolDeps` seams, human-only carve-out, cancel text

**Files:**
- Modify: `python/errorta_slack/tools.py` (`TOOL_CATALOG` ~`:76-200`, `ToolDeps` ~`:307-340`, `_VERB_IMPLS` at `:935`)
- Modify: `python/errorta_slack/connection.py` (`_handle_staged_confirmations` at `:741-756`, `_default_cancel_hook` at `:398-413`, `_CONFIRMATION_COPY` at `:629`)
- Test: `python/tests/slack/test_tools.py`, `python/tests/slack/test_connection.py`

**Interfaces:**
- Consumes: `live_run_manager` (Task 6) — `start(profile_name, project_id=)`, `stop(project_id=)`, `status(project_id=)`, `resume(profile_name)`; `profile.list_profiles()` (Task 3).
- Produces:
  - Catalog entries: `list_live_profiles` [R], `start_live_run` [C, arg `profile` required], `stop_live_run` [R], `live_status` [R], `resume_live_run` [C, arg `profile` required].
  - `tools.HUMAN_ONLY_VERBS = frozenset({"resume_live_run"})`.
  - `ToolDeps` seams: `liverun_list_fn: Callable[[], list[dict]]`, `liverun_start_fn: Callable[[str, str|None], dict]`, `liverun_stop_fn: Callable[[str|None], dict]`, `liverun_status_fn: Callable[[str|None], dict]`, `liverun_resume_fn: Callable[[str], dict]` — all `None` by default and resolved lazily to `live_run_manager` / `list_profiles` so importing `tools` never imports `errorta_liverun`.
  - Verb results: `start_live_run` → `{"status": "started", "run_id"}` or `{"status": "refused", "reason"}`; `stop_live_run` → `{"status": "stopping"|"empty"}`; `live_status` → the manager's dict; `resume_live_run` → `{"status": "resumed"|"empty"}`; `list_live_profiles` → `{"status": "ok", "profiles": [...]}`.
- The concierge system prompt renders straight from `TOOL_CATALOG` (`tests/slack/test_catalog_canary.py`), so no prompt edit is needed; the module-level `assert set(_VERB_IMPLS) == set(TOOL_CATALOG)` must keep passing.

- [ ] **Step 1: Write the failing tests**

Append to `python/tests/slack/test_tools.py`:

```python
def _liverun_deps(tmp_path: Path, calls: list) -> tools.ToolDeps:
    store.bind_channel("C9", "proj-lr")
    return tools.ToolDeps(
        liverun_list_fn=lambda: [{"name": "osrs", "valid": True, "error": None}],
        liverun_start_fn=lambda profile, project_id: (calls.append(("start", profile, project_id)) or {"status": "started", "run_id": "r1"}),
        liverun_stop_fn=lambda project_id: (calls.append(("stop", project_id)) or {"status": "stopping"}),
        liverun_status_fn=lambda project_id: {"status": "live", "phase": "watching", "probes": {}},
        liverun_resume_fn=lambda profile: (calls.append(("resume", profile)) or {"status": "resumed"}),
    )


def test_liverun_catalog_trust_classes() -> None:
    assert tools.TOOL_CATALOG["start_live_run"]["trust"] == "C"
    assert tools.TOOL_CATALOG["resume_live_run"]["trust"] == "C"
    for v in ("stop_live_run", "live_status", "list_live_profiles"):
        assert tools.TOOL_CATALOG[v]["trust"] == "R"
    assert "resume_live_run" in tools.HUMAN_ONLY_VERBS


def test_start_live_run_stages_then_fires_with_block_actions(tmp_path: Path) -> None:
    calls: list = []
    deps = _liverun_deps(tmp_path, calls)
    staged = tools.dispatch("start_live_run", {"profile": "osrs"}, channel_id="C9", thread_ts="1", deps=deps)
    assert staged["status"] == "needs_confirmation" and calls == []
    fired = tools.dispatch("start_live_run", {"profile": "osrs"}, channel_id="C9", thread_ts="1",
                           confirmed_via="block_actions", deps=deps)
    assert fired == {"status": "started", "run_id": "r1"}
    assert calls == [("start", "osrs", "proj-lr")]


def test_start_live_run_requires_profile_name(tmp_path: Path) -> None:
    deps = _liverun_deps(tmp_path, [])
    with pytest.raises(tools.ToolError):
        tools.dispatch("start_live_run", {}, channel_id="C9", thread_ts="1", confirmed_via="block_actions", deps=deps)
    with pytest.raises(tools.ToolError):
        tools.dispatch("start_live_run", {"profile": "../x"}, channel_id="C9", thread_ts="1", confirmed_via="block_actions", deps=deps)


def test_stop_live_run_is_R_and_immediate(tmp_path: Path) -> None:
    calls: list = []
    deps = _liverun_deps(tmp_path, calls)
    out = tools.dispatch("stop_live_run", {}, channel_id="C9", thread_ts="1", deps=deps)
    assert out == {"status": "stopping"} and calls == [("stop", "proj-lr")]


def test_live_status_and_list(tmp_path: Path) -> None:
    deps = _liverun_deps(tmp_path, [])
    assert tools.dispatch("live_status", {}, channel_id="C9", thread_ts="1", deps=deps)["phase"] == "watching"
    out = tools.dispatch("list_live_profiles", {}, channel_id="C9", thread_ts="1", deps=deps)
    assert out["profiles"][0]["name"] == "osrs"


def test_resume_live_run_is_C(tmp_path: Path) -> None:
    calls: list = []
    deps = _liverun_deps(tmp_path, calls)
    assert tools.dispatch("resume_live_run", {"profile": "osrs"}, channel_id="C9", thread_ts="1", deps=deps)["status"] == "needs_confirmation"
    assert tools.dispatch("resume_live_run", {"profile": "osrs"}, channel_id="C9", thread_ts="1",
                          confirmed_via="block_actions", deps=deps) == {"status": "resumed"}
```

Append to `python/tests/slack/test_connection.py` (mirror `test_autopilot_on_auto_fires_project_confirmation_via_block_actions` at `:1557` for the helpers `_bridge`, `_message_envelope`, `_staged_turn`, `_approve_button_values`):

```python
async def test_autopilot_never_auto_fires_resume_live_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store.bind_channel("C1", "proj-a")
    config.save({"autopilot": True})
    cid = store.stage_confirmation("resume_live_run", {"profile": "osrs"}, "601.1", channel_id="C1")
    monkeypatch.setattr(concierge, "run_turn", lambda *a, **k: _staged_turn("resume_live_run", {"profile": "osrs"}, cid))
    dispatch_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(tools, "dispatch",
                        lambda verb, args, *, channel_id, thread_ts, confirmed_via=None, deps: (
                            dispatch_calls.append({"verb": verb}) or {"status": "resumed"}))
    bridge, sdk, poster = _bridge(tmp_path)
    await bridge.handle_event(_message_envelope(event_id="Ev9", channel="C1", ts="601.1", thread_ts="601.1", text="resume the live run"))
    await bridge.wait_idle("601.1")
    assert dispatch_calls == []
    assert store.get_confirmation(cid)["state"] == "pending"
    assert _approve_button_values(poster) == [cid]


async def test_bare_stop_also_stops_live_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store.bind_channel("C1", "proj-a")
    seen: list[str] = []
    monkeypatch.setattr(tools, "dispatch",
                        lambda verb, args, *, channel_id, thread_ts, confirmed_via=None, deps: (
                            seen.append(verb) or {"status": "stopping"}))
    bridge, sdk, poster = _bridge(tmp_path)
    bridge._default_cancel_hook("700.1", {"channel_id": "C1", "project_id": "proj-a"})
    assert set(seen) == {"stop_runtime", "stop_live_run"}
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest -q tests/slack/test_tools.py tests/slack/test_connection.py -k "liverun or live_run or live_status or bare_stop"`
Expected: FAIL (`KeyError: 'start_live_run'`, `TypeError: unexpected keyword 'liverun_list_fn'`).

- [ ] **Step 3: Implement**

In `tools.py`, add to `TOOL_CATALOG` (after `stop_runtime`):

```python
    "list_live_profiles": {
        "trust": "R",
        "summary": "List the operator-authored live-run profiles and whether each validates.",
    },
    "start_live_run": {
        "trust": "C",
        "args": (("profile", True, "the live-run profile name (operator-authored, on disk)"),),
        "summary": "Launch the bound project's live run from a profile and supervise it by wall-clock.",
    },
    "stop_live_run": {
        "trust": "R",
        "summary": "Stop the live run now: collect evidence, log off, tear down. Never waits for approval.",
    },
    "live_status": {
        "trust": "R",
        "summary": "Phase, elapsed time, per-probe health ages, and teardown literals of the live run.",
    },
    "resume_live_run": {
        "trust": "C",
        "args": (("profile", True, "the paused profile to clear for launching again"),),
        "summary": "Clear a paused-awaiting-human hold on a profile (human approval only; autopilot never fires this).",
    },
```

Add after the catalog:

```python
# Verbs autopilot must NEVER auto-approve (spec §3.7): a human taps these.
HUMAN_ONLY_VERBS: frozenset[str] = frozenset({"resume_live_run"})
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
```

(`import re` at top if missing.) Add seams to `ToolDeps`:

```python
    # Live-run supervisor seams (spec 2026-08-21 §3.7). `None` = resolve lazily
    # to errorta_liverun so importing tools never imports the supervisor.
    liverun_list_fn: Callable[[], list[dict[str, Any]]] | None = None
    liverun_start_fn: Callable[[str, str | None], dict[str, Any]] | None = None
    liverun_stop_fn: Callable[[str | None], dict[str, Any]] | None = None
    liverun_status_fn: Callable[[str | None], dict[str, Any]] | None = None
    liverun_resume_fn: Callable[[str], dict[str, Any]] | None = None
```

Verb implementations (place near `stop_runtime`):

```python
def _profile_arg(args: dict[str, Any]) -> str:
    name = str(args.get("profile") or "").strip()
    if not _PROFILE_NAME_RE.match(name):
        raise ToolError("bad_profile_name", "profile must be a plain name (letters, digits, . _ -)")
    return name


def _liverun():
    from errorta_liverun.supervisor import live_run_manager
    return live_run_manager


def list_live_profiles(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                       deps: "ToolDeps") -> dict[str, Any]:
    if deps.liverun_list_fn is not None:
        rows = deps.liverun_list_fn()
    else:
        from errorta_liverun.profile import list_profiles
        rows = list_profiles()
    return {"status": "ok", "profiles": rows}


def start_live_run(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                   deps: "ToolDeps") -> dict[str, Any]:
    project_id = _bound_project_id(deps, channel_id)
    name = _profile_arg(args)
    fn = deps.liverun_start_fn or (lambda p, pid: _liverun().start(p, project_id=pid))
    return fn(name, project_id)


def stop_live_run(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                  deps: "ToolDeps") -> dict[str, Any]:
    project_id = _bound_project_id(deps, channel_id)
    fn = deps.liverun_stop_fn or (lambda pid: _liverun().stop(project_id=pid))
    return fn(project_id)


def live_status(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                deps: "ToolDeps") -> dict[str, Any]:
    project_id = _bound_project_id(deps, channel_id)
    fn = deps.liverun_status_fn or (lambda pid: _liverun().status(project_id=pid))
    return fn(project_id)


def resume_live_run(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                    deps: "ToolDeps") -> dict[str, Any]:
    _bound_project_id(deps, channel_id)
    name = _profile_arg(args)
    fn = deps.liverun_resume_fn or (lambda p: _liverun().resume(p))
    return fn(name)
```

Register all five in `_VERB_IMPLS` and add `"HUMAN_ONLY_VERBS"` to `__all__`.

In `connection.py` `_handle_staged_confirmations`, replace the loop body:

```python
        for item in staged:
            cid = item["cid"]
            record = self._deps.store.get_confirmation(cid)
            verb = str((record or {}).get("verb") or "")
            if autopilot and verb not in tools.HUMAN_ONLY_VERBS:
                await self._autopilot_fire(channel_id, thread_ts, cid)
            else:
                await self._post_decision_button(channel_id, thread_ts, cid)
```

Add copy to `_CONFIRMATION_COPY`:

```python
        "start_live_run": ("Start live run", "Launch and supervise the live run from this profile:"),
        "resume_live_run": ("Resume live run (human only)", "Clear the paused-awaiting-human hold on this profile:"),
```

and make `_confirmation_title` include `args.get("profile")` in the label lookup chain (`args.get("title") or args.get("profile") or args.get("project_id")`).

In `_default_cancel_hook`, after the `stop_runtime` dispatch add a second best-effort dispatch:

```python
        try:
            tools.dispatch(
                "stop_live_run", {},
                channel_id=channel_id, thread_ts=thread_ts,
                confirmed_via=None, deps=self._deps,
            )
        except tools.ToolError:
            pass
```

- [ ] **Step 4: Run the Slack suite**

Run: `python3 -m pytest -q tests/slack`
Expected: PASS, including `test_catalog_canary.py` and `test_studio_catalog_canary.py`.

- [ ] **Step 5: Commit**

```bash
git add python/errorta_slack/tools.py python/errorta_slack/connection.py python/tests/slack/test_tools.py python/tests/slack/test_connection.py
git commit -m "feat(slack): live-run verbs, human-only resume carve-out, bare stop tears down live run"
```

---

### Task 9: Outbound progress source, PNG attachment, and the `interval_s` fix

**Files:**
- Modify: `python/errorta_slack/outbound.py` (`OutboundDeps` `:127-137`, after `_run_state_items` `:234-270`, `_current_items` `:272-283`, `poll_once` fyi branch ~`:386-389`)
- Modify: `python/errorta_app/slack_lifecycle.py` (`_SyncWebClientPoster` `:91-102`, `_start_outbound` `:105-144`)
- Test: `python/tests/slack/test_outbound.py`, `python/tests/slack/test_slack_lifecycle_outbound.py`

**Interfaces:**
- Consumes: `RunStore.events/list`, run state files (Task 4); `live_run_manager` (Task 6).
- Produces:
  - `OutboundDeps.liverun_events_fn: Callable[[str], list[tuple[RunState, list[dict]]]]` — given a `project_id`, returns every run for that project (any phase) with its events; default reads `RunStore` directly (`_default_liverun_events_fn`), scanning `runs/*/state.json` for `project_id` matches.
  - `_liverun_items(deps, project_id) -> list[_Item]` with markers `liverun:<run_id>:<seq>`; `_Item.file_path: str|None` (new optional field) for the PNG.
  - Mandatory items: `phase` events to `stopped|failed|paused_awaiting_human|lost_on_restart`, `stall`, `ban_signal`, `caps`, `literals`. Non-mandatory: `launch_step`, `probe_warn`, `teardown_step`, `evidence`, `refused`, `phase` to `launching|watching|stopping`.
  - Poster duck-type: optional `post_file(channel_id, thread_ts, path: str, title: str) -> dict`. `poll_once` calls it when `item.file_path` is set and the poster has the attribute; otherwise the text still posts.
  - `_start_outbound(poster, *, run_loop_fn=None, interval_s: float|None=None, timeout_minutes: float|None=None)` forwards both from `config.load()` when not given.

- [ ] **Step 1: Write the failing tests**

Append to `python/tests/slack/test_outbound.py` (reuse `SyncFakePoster` at `:33` and the `_isolated_errorta_home` fixture):

```python
def _liverun_fixture(tmp_path: Path, project_id: str = "proj-lr"):
    from errorta_liverun.state import RunState, RunStore
    rs = RunStore()
    rid = rs.new_run_id()
    st = RunState(run_id=rid, profile_name="osrs", project_id=project_id, phase="watching", reason=None,
                  session_id="s", step_index=2, started_at="2026-08-21T00:00:00Z", launched_at="t",
                  ended_at=None, evidence_dir=str(rs.evidence_dir(rid)))
    rs.save(st)
    rs.append_event(rid, "phase", {"to": "launching", "reason": None})
    rs.append_event(rid, "launch_step", {"name": "rebuild-jar", "ok": True, "exit_code": 0, "stdout": "", "stderr": ""})
    rs.append_event(rid, "probe_warn", {"id": "xp", "stalled_s": 900})
    return rs, st


def test_liverun_items_have_stable_markers_and_mandatory_flags(tmp_path: Path) -> None:
    rs, st = _liverun_fixture(tmp_path)
    rs.append_event(st.run_id, "stall", {"id": "journal-seq", "stalled_s": 181})
    rs.append_event(st.run_id, "literals", {"logoff_verified": "ABSENT"})
    rs.append_event(st.run_id, "phase", {"to": "stopped", "reason": "stall:journal-seq"})
    items = outbound._liverun_items(outbound.OutboundDeps(), "proj-lr")
    markers = [it.marker for it in items]
    assert markers == [f"liverun:{st.run_id}:{i}" for i in range(1, 7)]
    by_kind = {m.split(":")[-1]: it for m, it in zip(markers, items)}
    assert items[0].mandatory is False            # phase -> launching
    assert items[3].mandatory is True             # stall
    assert items[4].mandatory is True and "ABSENT" in items[4].detail
    assert items[5].mandatory is True and "stopped" in items[5].detail
    assert all(it.kind == "fyi" for it in items)
    # nothing from another project
    assert outbound._liverun_items(outbound.OutboundDeps(), "other") == []


def test_liverun_items_flow_through_poll_once_and_dedupe(tmp_path: Path) -> None:
    rs, st = _liverun_fixture(tmp_path)
    store.bind_channel("C-lr", "proj-lr")
    poster = SyncFakePoster()
    first = outbound.poll_once("C-lr", "proj-lr", deps=outbound.OutboundDeps(), poster=poster)
    assert len(first) == 3
    assert outbound.poll_once("C-lr", "proj-lr", deps=outbound.OutboundDeps(), poster=poster) == []


def test_muted_channel_still_gets_stop_and_stall(tmp_path: Path) -> None:
    rs, st = _liverun_fixture(tmp_path)
    rs.append_event(st.run_id, "stall", {"id": "brain-alive", "stalled_s": 46})
    store.bind_channel("C-lr", "proj-lr")
    store.set_updates_muted("C-lr", True)
    poster = SyncFakePoster()
    posted = outbound.poll_once("C-lr", "proj-lr", deps=outbound.OutboundDeps(), poster=poster)
    assert posted == [f"liverun:{st.run_id}:4"]


def test_evidence_png_is_uploaded_when_poster_supports_files(tmp_path: Path) -> None:
    rs, st = _liverun_fixture(tmp_path)
    png = Path(st.evidence_dir); png.mkdir(parents=True, exist_ok=True)
    shot = png / "window-1.png"; shot.write_bytes(b"\x89PNG")
    rs.append_event(st.run_id, "evidence", {"id": "client-window", "ok": True, "refs": [str(shot)], "detail": ""})
    store.bind_channel("C-lr", "proj-lr")

    class FilePoster(SyncFakePoster):
        def __init__(self) -> None:
            super().__init__(); self.files: list[tuple] = []
        def post_file(self, channel_id, thread_ts, path, title):
            self.files.append((channel_id, path, title)); return {"ok": True}

    poster = FilePoster()
    outbound.poll_once("C-lr", "proj-lr", deps=outbound.OutboundDeps(), poster=poster)
    assert poster.files == [("C-lr", str(shot), "client-window")]
    # A poster without post_file still posts the text and never raises.
    store.advance_cursor("C-lr", "[]")
    outbound.poll_once("C-lr", "proj-lr", deps=outbound.OutboundDeps(), poster=SyncFakePoster())
```

Check `store.set_updates_muted` is the real mute setter name (`grep -n "def .*mute" errorta_slack/store.py`) and use that name.

Append to `python/tests/slack/test_slack_lifecycle_outbound.py`:

```python
def test_start_outbound_forwards_interval_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    from errorta_app import slack_lifecycle
    from errorta_slack import config
    config.save({"interval_s": 7, "timeout_minutes": 11})
    seen: dict = {}
    async def fake_run_loop(**kw):
        seen.update(kw)
    slack_lifecycle._start_outbound(poster=object(), run_loop_fn=fake_run_loop)
    slack_lifecycle._outbound_thread.join(timeout=2)
    assert seen["interval_s"] == 7 and seen["timeout_minutes"] == 11
    slack_lifecycle._stop_outbound()
```

(If `run_loop`'s parameters are named differently — read `outbound.run_loop` at `:507` — use its real names in both the test and the forwarding code.)

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest -q tests/slack/test_outbound.py tests/slack/test_slack_lifecycle_outbound.py -k "liverun or png or forwards"`
Expected: FAIL (`AttributeError: _liverun_items`, missing kwargs).

- [ ] **Step 3: Implement**

In `outbound.py`: add `file_path: str | None = None` to `_Item`; add to `OutboundDeps`:

```python
    liverun_events_fn: Callable[[str], list[tuple[Any, list[dict[str, Any]]]]] = _default_liverun_events_fn
```

with, above the class:

```python
def _default_liverun_events_fn(project_id: str) -> list[tuple[Any, list[dict[str, Any]]]]:
    try:
        from errorta_liverun.state import RunStore
    except ImportError:  # pragma: no cover
        return []
    rs = RunStore()
    root = rs._root
    out = []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        st = rs.load(d.name)
        if st is not None and st.project_id == project_id:
            out.append((st, rs.events(st.run_id)))
    return out
```

Items builder:

```python
_LIVERUN_MANDATORY_KINDS = {"stall", "ban_signal", "caps", "literals"}
_LIVERUN_TERMINAL = {"stopped", "failed", "paused_awaiting_human", "lost_on_restart"}


def _liverun_detail(kind: str, d: dict[str, Any], profile: str) -> tuple[str, bool, str | None]:
    esc = render.escape_mrkdwn
    if kind == "phase":
        to = str(d.get("to")); reason = str(d.get("reason") or "")
        text = f"Live run *{esc(profile)}* → {esc(to)}" + (f" — {esc(reason)[:200]}" if reason else "")
        return text, to in _LIVERUN_TERMINAL, None
    if kind == "launch_step":
        if "check" in d:
            return f"Launch step *{esc(str(d.get('name')))}*: check {esc(str(d['check']))}", False, None
        ok = "ok" if d.get("ok") else f"FAILED (rc={d.get('exit_code')})"
        tail = esc(str(d.get("stderr") or d.get("stdout") or ""))[:300]
        return f"Launch step *{esc(str(d.get('name')))}*: {ok}" + (f"\n```{tail}```" if tail and not d.get("ok") else ""), False, None
    if kind == "probe_warn":
        return f"⚠️ Probe *{esc(str(d.get('id')))}* stalled {int(d.get('stalled_s', 0))}s — watching", False, None
    if kind == "stall":
        return f"🛑 Stall on *{esc(str(d.get('id')))}* after {int(d.get('stalled_s', 0))}s — stopping", True, None
    if kind == "evidence":
        refs = [r for r in (d.get("refs") or []) if str(r).endswith(".png")]
        return f"Evidence *{esc(str(d.get('id')))}*: {'ok' if d.get('ok') else esc(str(d.get('detail') or 'failed'))}", False, (refs[0] if refs else None)
    if kind == "teardown_step":
        lit = d.get("literal")
        ok = "ok" if d.get("ok") else "FAILED"
        return f"Teardown *{esc(str(d.get('name')))}*: {ok}" + (f" ({esc(str(lit))}={'PRESENT' if d.get('ok') else 'ABSENT'})" if lit else ""), False, None
    if kind == "literals":
        parts = ", ".join(f"{esc(k)}: {esc(str(v))}" for k, v in d.items())
        return f"Teardown literals — {parts}", True, None
    if kind == "ban_signal":
        return f"🚫 Ban-class signal matched (`{esc(str(d.get('pattern')))}`) — paused awaiting human. Use resume_live_run only after you have looked.", True, None
    if kind == "caps":
        return f"⛔ Launch cap hit: {esc(str(d.get('code')))} — paused awaiting human", True, None
    if kind == "refused":
        return f"Live run refused: {esc(str(d.get('code')))}", False, None
    return f"{esc(kind)}: {esc(json.dumps(d))[:300]}", False, None


def _liverun_items(deps: "OutboundDeps", project_id: str) -> list[_Item]:
    items: list[_Item] = []
    try:
        runs = deps.liverun_events_fn(project_id)
    except Exception:  # noqa: BLE001
        _LOGGER.warning("outbound: could not read live-run events", exc_info=True)
        return []
    for state, events in runs:
        profile = str(_get(state, "profile_name", ""))
        run_id = str(_get(state, "run_id", ""))
        for ev in events:
            kind = str(ev.get("kind", ""))
            detail, mandatory, png = _liverun_detail(kind, dict(ev.get("detail") or {}), profile)
            items.append(_Item(marker=f"liverun:{run_id}:{ev['seq']}", sort_key=str(ev.get("at", "")),
                               kind="fyi", title="", detail=detail,
                               mandatory=mandatory or kind in _LIVERUN_MANDATORY_KINDS, file_path=png))
    return items
```

Add `+ _liverun_items(deps, project_id)` to `_current_items`. In `poll_once`'s fyi branch, after `poster.post_message(...)`:

```python
            if item.file_path and hasattr(poster, "post_file"):
                try:
                    poster.post_file(channel_id, "", item.file_path, Path(item.file_path).stem.split("-")[0] if False else item.detail.split("*")[1] if "*" in item.detail else "evidence")
                except Exception:  # noqa: BLE001 — an upload failure must not block the stream
                    _LOGGER.warning("outbound: evidence upload failed", exc_info=True)
```

Simplify that title expression to: `title = item.detail.split("*")[1] if item.detail.count("*") >= 2 else "evidence"` computed on the line above. `import json` and `from pathlib import Path` as needed.

In `slack_lifecycle.py`, add to `_SyncWebClientPoster`:

```python
        def post_file(self, channel_id: Any, thread_ts: Any, path: str, title: str) -> dict[str, Any]:
            kwargs: dict[str, Any] = {"channel": channel_id, "file": path, "title": title}
            if thread_ts:
                kwargs["thread_ts"] = thread_ts
            return dict(web_client.files_upload_v2(**kwargs).data)
```

and change `_start_outbound` to:

```python
def _start_outbound(poster: Any, *, run_loop_fn: Any = None,
                    interval_s: float | None = None, timeout_minutes: float | None = None) -> None:
    ...
    from errorta_slack import config as slack_config
    cfg = slack_config.load()
    interval = float(interval_s if interval_s is not None else cfg.get("interval_s", 15))
    timeout = float(timeout_minutes if timeout_minutes is not None else cfg.get("timeout_minutes", 30))
    ...
            asyncio.run(run_loop(
                bindings_provider=slack_store.list_bindings,
                deps=slack_outbound.OutboundDeps(),
                poster=poster,
                stop_event=stop_event,
                interval_s=interval,
                timeout_minutes=timeout,
            ))
```

Match the kwarg names to `outbound.run_loop`'s real signature (`:507`). If `interval_s` is not yet a config key, add `"interval_s": 15` to `config.DEFAULT_CONFIG` with float normalisation in `config.load()`.

- [ ] **Step 4: Run the Slack suite**

Run: `python3 -m pytest -q tests/slack`
Expected: PASS. `test_outbound_module_does_not_import_slack_sdk` must still pass — `_default_liverun_events_fn` imports `errorta_liverun.state` lazily and that module imports no Slack SDK.

- [ ] **Step 5: Commit**

```bash
git add python/errorta_slack/outbound.py python/errorta_slack/config.py python/errorta_app/slack_lifecycle.py python/tests/slack/
git commit -m "feat(slack): live-run progress stream with evidence PNGs; forward outbound interval/timeout"
```

---
### Task 10: Acceptance test — fake client + fake brain, real supervisor

**Files:**
- Create: `python/tests/acceptance/test_liverun_fake_profile.py`
- Create: `python/tests/acceptance/liverun_fixtures/fake_client.py`, `fake_brain.py`

**Interfaces:**
- Consumes: everything from Tasks 1–7 unmocked except `ssh` (a fake binary on `PATH` that runs the remote argv locally) and `known_hosts_fn`.
- Produces: the spec §4 acceptance assertions. No network beyond loopback; no real Slack.

- [ ] **Step 1: Write the fixtures**

`python/tests/acceptance/liverun_fixtures/fake_client.py` — a loopback HTTP server that serves `/state` with `gameState` driven by a control file:

```python
"""Fake game client: GET /state -> {"gameState": <contents of CTRL file or LOGGED_IN>}.
Usage: python fake_client.py <port> <ctrl_file>"""
import http.server, json, sys
from pathlib import Path

PORT, CTRL = int(sys.argv[1]), Path(sys.argv[2])


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        state = CTRL.read_text().strip() if CTRL.exists() else "LOGGED_IN"
        body = json.dumps({"gameState": state}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass


http.server.HTTPServer(("127.0.0.1", PORT), H).serve_forever()
```

`python/tests/acceptance/liverun_fixtures/fake_brain.py` — appends a line to a log every 0.2 s for N seconds, then goes silent but stays alive; on SIGTERM writes `LOGIN_SCREEN` to the client's ctrl file (the "safe logout"):

```python
"""Fake brain: python fake_brain.py <log> <active_seconds> <ctrl_file>"""
import signal, sys, time
from pathlib import Path

LOG, ACTIVE, CTRL = Path(sys.argv[1]), float(sys.argv[2]), Path(sys.argv[3])


def _logoff(*_):
    CTRL.write_text("LOGIN_SCREEN")
    sys.exit(0)


signal.signal(signal.SIGTERM, _logoff)
start = time.time(); i = 0
while True:
    if time.time() - start < ACTIVE:
        i += 1
        with LOG.open("a") as fh:
            fh.write(f"seq={i}\n")
    time.sleep(0.2)
```

- [ ] **Step 2: Write the acceptance test**

```python
# python/tests/acceptance/test_liverun_fake_profile.py
from __future__ import annotations

import os
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from errorta_liverun import profile as P
from errorta_liverun.recovery import recover_on_boot
from errorta_liverun.state import LaunchLedger, RunStore
from errorta_liverun.supervisor import LiveRunManager
from errorta_tools.runner.remote import RemoteToolRunner
from errorta_tunnels.manager import TunnelManager

FIX = Path(__file__).parent / "liverun_fixtures"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    ssh = tmp_path / "ssh"
    ssh.write_text("#!/bin/sh\nwhile [ \"$1\" != \"--\" ]; do shift; done; shift\neval \"$1\"\n")
    ssh.chmod(ssh.stat().st_mode | stat.S_IEXEC)
    port = _free_port()
    ctrl = tmp_path / "ctrl"; ctrl.write_text("LOGGED_IN")
    log = tmp_path / "brain.log"; pidfile = tmp_path / "brain.pid"
    py = sys.executable
    doc = {
        "version": 1, "created_by": "operator",
        "hosts": {"box": {"ssh_host": "localhost"}},
        "tunnels": [],
        "launch": [
            {"name": "client", "local": {"argv": [py, str(FIX / "fake_client.py"), str(port), str(ctrl)]},
             "check": {"http": {"url": f"http://127.0.0.1:{port}/state", "expect_status": 200}}, "timeout_s": 10},
            {"name": "brain", "remote": {"host": "box", "argv": [py, str(FIX / "fake_brain.py"), str(log), "2", str(ctrl)],
             "detach": True, "pidfile": str(pidfile), "log": str(tmp_path / "brain.out")},
             "check": {"remote_pid_alive": {"host": "box", "pidfile": str(pidfile)}}, "timeout_s": 10},
        ],
        "watch": [
            {"id": "client", "every_s": 0.5, "stall_after_s": 2, "on_stall": "stop",
             "probe": {"http": {"url": f"http://127.0.0.1:{port}/state"}}},
            {"id": "brain-alive", "every_s": 0.5, "stall_after_s": 2, "on_stall": "stop",
             "probe": {"remote_pid_alive": {"host": "box", "pidfile": str(pidfile)}}},
            {"id": "brain-log", "every_s": 0.5, "stall_after_s": 2, "on_stall": "stop",
             "probe": {"remote_stdout_advancing": {"host": "box", "argv": ["tail", "-n", "1", str(log)]}}},
        ],
        "evidence": [
            {"name": "log-tail", "remote": {"host": "box", "argv": ["tail", "-n", "5", str(log)]}},
            {"name": "state", "http": {"url": f"http://127.0.0.1:{port}/state"}},
        ],
        "teardown": [
            {"name": "brain-stop", "remote_signal": {"host": "box", "pidfile": str(pidfile), "signal": "TERM", "grace_s": 2, "then": "KILL"}},
            {"name": "logoff-wait", "check": {"http_json": {"url": f"http://127.0.0.1:{port}/state", "path": "gameState", "not_equals": "LOGGED_IN"}},
             "timeout_s": 5, "evidence_literal": "logoff_verified"},
        ],
        "caps": {"min_launch_gap_s": 900},
        "ban_signals": ["Account is banned"],
    }
    # The `local` client step needs an absolute argv0: sys.executable is absolute.
    d = P.profiles_dir(); d.mkdir(parents=True)
    pf = d / "fake.yaml"; pf.write_text(yaml.safe_dump(doc)); pf.chmod(0o600)
    mgr = LiveRunManager(store=RunStore(), ledger=LaunchLedger(), tunnels=TunnelManager(),
                         remote=RemoteToolRunner(ssh_bin=str(ssh)),
                         load_profile=lambda p: P.load_profile(p, known_hosts_fn=lambda h: True))
    yield mgr, tmp_path, ctrl, pidfile
    mgr.teardown_all()
    if pidfile.exists():
        try:
            os.kill(int(pidfile.read_text()), 9)
        except (ProcessLookupError, ValueError):
            pass


def _wait(mgr, project_id, phases, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = mgr.status(project_id=project_id)
        if st["status"] == "empty" and st["last"] and st["last"]["phase"] in phases:
            return st["last"]
        time.sleep(0.2)
    raise AssertionError(f"never reached {phases}: {mgr.status(project_id=project_id)}")


def test_stall_is_detected_and_torn_down_with_logoff(env) -> None:
    mgr, home, ctrl, pidfile = env
    t0 = time.time()
    out = mgr.start("fake", project_id="proj")
    assert out["status"] == "started", out
    final = _wait(mgr, "proj", {"stopped", "failed", "paused_awaiting_human"})
    assert final["phase"] == "stopped"
    assert final["reason"] == "stall:brain-log"
    assert final["literals"] == {"logoff_verified": True}
    # stall_after (2s) + every (0.5s) + active (2s) + slack — well under 30s
    assert time.time() - t0 < 30
    store = RunStore()
    kinds = [e["kind"] for e in store.events(final["run_id"])]
    assert kinds.count("stall") == 1
    assert kinds.index("evidence") < kinds.index("teardown_step")
    assert [e for e in store.events(final["run_id"]) if e["kind"] == "literals"][-1]["detail"] == {"logoff_verified": "PRESENT"}
    # no orphans: brain pid is gone, client pgid killed
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)
    assert final["owned_pgids"] == []


def test_second_start_inside_gap_is_refused(env) -> None:
    mgr, *_ = env
    assert mgr.start("fake", project_id="proj")["status"] == "started"
    _wait(mgr, "proj", {"stopped"})
    assert mgr.start("fake", project_id="proj") == {"status": "refused", "reason": "cap_gap"}


def test_sidecar_restart_mid_run_is_lost_on_restart(env) -> None:
    mgr, home, ctrl, pidfile = env
    assert mgr.start("fake", project_id="proj")["status"] == "started"
    # wait until watching, then simulate a crash: drop the manager without teardown
    for _ in range(100):
        if mgr.status(project_id="proj").get("phase") == "watching":
            break
        time.sleep(0.1)
    sup = next(iter(mgr._runs.values()))
    sup._stop.set()  # freeze the thread's next tick into a stop we will NOT process
    sup._tick = lambda: None  # type: ignore[assignment]
    brain_pid = int(pidfile.read_text())
    lost = recover_on_boot(store=RunStore(), tunnels=TunnelManager(),
                           remote=RemoteToolRunner(ssh_bin=str(home / "ssh")),
                           load_profile=lambda p: P.load_profile(p, known_hosts_fn=lambda h: True))
    assert lost == [sup.state.run_id]
    after = RunStore().load(sup.state.run_id)
    assert after.phase == "lost_on_restart"
    assert after.literals.get("logoff_verified") is True
    time.sleep(0.5)
    with pytest.raises(ProcessLookupError):
        os.kill(brain_pid, 0)
    mgr._runs.clear()
```

Mark the module `pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="posix only")`.

- [ ] **Step 3: Run it**

Run: `python3 -m pytest -q tests/acceptance/test_liverun_fake_profile.py -x`
Expected: PASS in under ~60 s. If `test_sidecar_restart_mid_run_is_lost_on_restart` races (the live thread finishes its own teardown first), replace the `_tick` monkeypatch by killing the thread's work earlier: start the run with `Supervisor(..., sleep=lambda s: time.sleep(60))` via `LiveRunManager` injection — add an optional `supervisor_kwargs: dict` to `LiveRunManager.__init__` forwarded to `Supervisor(...)` in `start()` if needed (document it in Task 6's interface when you do).

- [ ] **Step 4: Run the whole suite**

Run: `python3 -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/tests/acceptance/
git commit -m "test(liverun): acceptance — fake client/brain stall, caps, lost_on_restart"
```

---

### Task 11: Operator docs and the OSRS profile skeleton

**Files:**
- Create: `docs/liverun/README.md`, `docs/liverun/example-profile.yaml`
- Modify: `README.md` (one bullet under the Slack bridge section pointing at `docs/liverun/README.md`)

**Interfaces:** none (documentation).

- [ ] **Step 1: Write `docs/liverun/README.md`**

Contents (write it fully, not as bullets-to-expand):

```markdown
# Live-run supervisor

Errorta can launch, watch, and safely stop a long-running program from Slack.
It runs what **you** declare in a profile; it never composes commands itself.

## Where profiles live

`$ERRORTA_HOME/liverun/profiles/<name>.yaml` (default `~/.errorta/liverun/profiles/`).
File must be owned by you, mode 0600/0644, not a symlink, and contain
`created_by: operator` and `version: 1`. Invalid profiles are listed by
`list_live_profiles` with the reason; they never run.

## Slack verbs

| Verb | Approval | What it does |
|---|---|---|
| `list_live_profiles` | none | names + validation status |
| `start_live_run <profile>` | yes (autopilot may approve) | launch steps → watch |
| `stop_live_run` | none, immediate | evidence → teardown → logoff literals |
| `live_status` | none | phase, elapsed, per-probe last-ok age, literals |
| `resume_live_run <profile>` | **human tap only** | clears a paused-awaiting-human hold |

A bare "stop" in the channel also stops the live run.

## What you will see in Slack

Every launch step, every stall warning, the stop reason, each teardown step
with its literal, and a final `Teardown literals — logoff_verified: PRESENT`
(or `ABSENT`). Stops, stalls, ban signals and cap hits post even when the
channel is muted. The client window PNG is attached when captured.

## Safety rules (enforced by the validator)

- local commands: absolute path argv0 (`osascript`/`pgrep` allowed); `./gradlew` needs absolute `cwd`
- no shell: argv lists only; `$SESSION_ID`/`$RUN_ID` are the only substitutions
- banned: `--ignore-risk-budget`, `--no-safety-plane`
- a `senditai_ng.cli run` step must carry `--max-session-seconds`, `--receipt-id`, `--require-live-feed`
- caps (lower only): 2 launches/hour, 900 s gap, 8/day, 2 consecutive failed cycles
- `teardown` must include a step with `evidence_literal: logoff_verified`
- any `ban_signals` regex match in evidence → `paused_awaiting_human`

## Restart behaviour

A live run is never resumed across an Errorta restart. On boot, any
non-terminal run gets the full teardown and is marked `lost_on_restart`.
The sidecar exits with the desktop app, so an overnight run needs Errorta open.

## Authoring the OSRS profile — verify these first

1. `osascript`/`cliclick` from the sidecar: Accessibility is per-app. The
   example routes `jagex-play` through Terminal.app by absolute path.
2. The `/state` field on `:8081` that proves logout (`gameState` assumed).
3. Cost of the journal `seq` probe at 30 s (direct read-only `sqlite3` vs `osrs-watcher --json`).
4. `senditai_ng.cli kill --session` resolves the same `--base-dir` the run used.
```

- [ ] **Step 2: Write `docs/liverun/example-profile.yaml`**

Copy the profile block from the spec §3.2 verbatim (rename the evidence entries' `id:` keys to `name:` — the schema uses `name` for every step), replace every `[...]` with a `# FILL: <what>` comment line so the file is invalid-by-construction until authored, and add a top comment: `# Copy to $ERRORTA_HOME/liverun/profiles/osrs.yaml, chmod 600, fill every FILL, run list_live_profiles.`

- [ ] **Step 3: README pointer**

Under the Slack bridge section of `README.md` add: `- Live-run supervisor (launch/watch/stop a long-running program from Slack): see docs/liverun/README.md`.

- [ ] **Step 4: Commit**

```bash
git add docs/liverun/ README.md
git commit -m "docs(liverun): operator guide and OSRS profile skeleton"
```

---

## Self-review against the spec

- §3.2 profile + every validator rule → Task 3. §3.3 primitives + argv-identity invariant → Task 5. §3.4 RemoteToolRunner → Task 2. §3.5 reverse tunnels + killpg → Task 1. §3.6 state machine, caps ledger, persistence, recovery, lifespan → Tasks 4, 6, 7. §3.7 verbs, human-only carve-out, bare stop, progress source, mandatory posting, PNG, F-I fix → Tasks 8, 9. §3.8 never-autonomous list → Tasks 3 (profiles on disk only), 6 (`paused_marker`), 8 (`HUMAN_ONLY_VERBS`), 5 (`_guard`). §4 acceptance → Task 10. §7 unverified items → Task 11 docs.
- Policy audit (§3.4 last bullet, `PolicyEngine` with `REMOTE_EGRESS`) is **not** wired in Task 5 — add it inside `_remote_request` callers only if `errorta_policy.PolicyEngine().evaluate` can be called without the Council run context; otherwise log the decision to the run's `events.jsonl` as `policy` and leave the engine call for Slice 2. The implementer must write one line in the Task 5 commit message saying which.
- Type consistency: `Check.params` is a dict for `http/http_json/file_mtime_newer/remote_pid_alive`, a str for `file_exists/pgrep/pgrep_absent/tunnel_up`, a tuple of `Check` for `all`, and `{"argv": tuple}` for `exit0` — Tasks 3, 5, 6, 10 all follow that. `Probe.params` is a dict except `elapsed_lt_s` (number). `Step.action` may be `None` (check-only teardown step) — handled in Tasks 6, 7, 10.
