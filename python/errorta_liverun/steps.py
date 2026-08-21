"""Step / check / probe primitives (spec §3.3). Deterministic mechanism only."""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shlex
import signal
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from errorta_council.coding.runtime_process import redact_log_line
from errorta_policy import PolicyAction, PolicyPhase
from errorta_tools.runner.policy import evaluate_runner_launch
from errorta_tools.runner.preview import capture_app_window
from errorta_tools.runner.remote import RemoteToolRunner
from errorta_tools.runner.types import ToolRunnerRequest, ToolRunnerResult, now_iso
from errorta_tunnels.manager import TunnelManager, TunnelSpec

from .profile import Action, Check, Probe, Profile, TunnelDef

_TAIL = 2000
_LOG = logging.getLogger("errorta.liverun")


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
    pgid: int | None = None


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
    # Redact the FULL text first, then truncate — truncating first can slice a
    # secret-shaped token in half, leaving an un-redacted fragment behind.
    redacted = "\n".join(redact_log_line(line) for line in text.splitlines())
    return redacted[-_TAIL:]


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


def _remote_path_expr(path: str) -> str:
    """Shell-code fragment for a validated pidfile/log path, tilde-aware.

    ``shlex.quote`` single-quotes its argument, which suppresses shell tilde
    expansion — a literal ``'~/b.pid'`` never becomes the remote HOME. Since
    the remote HOME differs from wherever this process runs, we can't resolve
    ``~`` locally either; instead render it as a ``"$HOME"``-expansion for the
    REMOTE shell to resolve at run time.
    """
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        rest = path[2:]
        return f'"$HOME"/{shlex.quote(rest)}' if rest else '"$HOME"'
    return shlex.quote(path)


def _remote_request(ctx: Ctx, host_id: str, argv: tuple[str, ...], timeout_s: float) -> ToolRunnerRequest:
    return ToolRunnerRequest(
        request_id=secrets.token_hex(4), run_id=ctx.run_id, tool_call_id="liverun",
        argv=argv, workspace_root=str(ctx.evidence_dir), execution_location="remote_ssh",
        timeout_seconds=timeout_s, metadata=_host(ctx, host_id))


def _remote(ctx: Ctx, host_id: str, argv: tuple[str, ...], timeout_s: float = 20,
            *, stdin_path: str | None = None):
    request = _remote_request(ctx, host_id, argv, timeout_s)
    # Policy evaluation ahead of every remote egress. The policy passed here
    # is a fixed allow (launch-cap / operator-authorship gating already
    # happened at profile-validation time, upstream of this module), so today
    # this call never changes control flow — but it is a REAL enforcement
    # point, not a no-op audit trail: a future policy configuration change has
    # somewhere to bite, and every decision is logged for the record.
    decision = evaluate_runner_launch(request, phase=PolicyPhase.REMOTE_EGRESS,
                                      policy={"action": "allow"})
    _LOG.info("policy decision for remote egress host=%s action=%s reason=%s",
             host_id, decision.action, decision.reason_code)
    if decision.action != PolicyAction.ALLOW:
        return _blocked_remote_result(request, decision.reason_code)
    return ctx.remote.run_sync(request, stdin_path=stdin_path)


def _blocked_remote_result(request: ToolRunnerRequest, reason_code: str | None) -> ToolRunnerResult:
    return ToolRunnerResult.blocked(request=request, reason_code=reason_code or "policy_denied")


# --- actions ------------------------------------------------------------- #

@dataclass
class _SpawnOutcome:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    pgid: int | None
    spawn_error: str | None = None


def _spawn_tracked(argv: tuple[str, ...], cwd: str | None, timeout_s: float, ctx: Ctx) -> _SpawnOutcome:
    """Spawn ``argv`` in its own process group and track it in
    ``ctx.owned_pgids`` for exactly the duration it is alive: appended right
    after spawn, pruned right after ``communicate()`` returns on EVERY path
    (normal exit and timeout-kill alike). ``owned_pgids`` must only ever hold
    LIVE pgids — a stale entry left behind after the child exits is a future
    ``SIGKILL`` aimed at whatever unrelated process the OS hands that pid out
    to next (pid reuse), and ``killpg`` does not raise on a reused pgid, so
    nothing would catch it.
    """
    try:
        proc = subprocess.Popen(  # noqa: S603 — validated, profile-declared argv
            list(argv), cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    except OSError as exc:
        return _SpawnOutcome(None, b"", b"", False, None, spawn_error=str(exc))
    ctx.owned_pgids.append(proc.pid)
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _killpg(proc.pid)
        out, err = proc.communicate()
    finally:
        if proc.pid in ctx.owned_pgids:
            ctx.owned_pgids.remove(proc.pid)
    return _SpawnOutcome(proc.returncode, out, err, timed_out, proc.pid)


def _run_local(params: dict[str, Any], ctx: Ctx, timeout_s: float) -> StepResult:
    argv = _guard(tuple(params["argv"]), ctx)
    started_at = now_iso()
    ctx.evidence_dir.mkdir(parents=True, exist_ok=True)
    outcome = _spawn_tracked(argv, params.get("cwd") or None, timeout_s, ctx)
    if outcome.spawn_error is not None:
        return StepResult(False, started_at, now_iso(), detail=f"spawn failed: {outcome.spawn_error}")
    return StepResult(ok=(outcome.returncode == 0 and not outcome.timed_out), started_at=started_at,
                      ended_at=now_iso(), exit_code=outcome.returncode, pgid=outcome.pgid,
                      stdout_tail=_redact(outcome.stdout.decode("utf-8", "replace")),
                      stderr_tail=_redact(outcome.stderr.decode("utf-8", "replace")), timed_out=outcome.timed_out)


def _killpg(pid: int, sig: int = signal.SIGKILL) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        # EPERM means the pgid EXISTS but isn't ours (classic pid-reuse
        # signature) — log it and stop. Escalating to `os.kill` here would
        # aim a signal at a process we never spawned.
        _LOG.warning("killpg(%s, %s) denied (EPERM): pgid exists but is not ours; not escalating", pid, sig)


def _run_remote(params: dict[str, Any], ctx: Ctx, timeout_s: float) -> StepResult:
    argv = _guard(tuple(params["argv"]), ctx)
    host_id = params["host"]
    started_at = now_iso()
    stdin_path = os.path.expanduser(params["stdin_file"]) if params.get("stdin_file") else None
    if params.get("detach"):
        pidfile = params["pidfile"]
        log = params.get("log") or (pidfile + ".log")
        pidfile_expr = _remote_path_expr(pidfile)
        log_expr = _remote_path_expr(log)
        cmd = shlex.join(argv)
        # Built server-side from the validated argv; never from a string.
        #
        # `t=$(mktemp) || exit 90` / `cat > "$t" || exit 91`: fail loudly if
        # the remote can't even stage the stdin token.
        #
        # `exec 3<"$t"; rm -f "$t"`: open the temp file on fd 3 for the
        # detached child to read, then remove it immediately — the fd stays
        # open across the `rm` (POSIX guarantee), so the token file never
        # outlives this wrapper and there's no window where a second reader
        # could see it on disk.
        #
        # `setsid` (full session detach) is used when the remote box has it
        # (typical Linux target); it's util-linux-only and absent on macOS,
        # so fall back to plain `nohup` there — under a non-interactive ssh
        # command there's no controlling tty to detach from either way.
        #
        # `echo "$pid" > PIDFILE || exit 92`: fail loudly if the pidfile
        # can't be written (e.g. its directory doesn't exist) — previously
        # this wrapper's exit status was just `rm -f`'s, so a launch that
        # never even started could still report `ok=True` and let teardown
        # emit `logoff_verified` for a process it never signalled.
        #
        # `sleep 0.2; kill -0 "$pid" 2>/dev/null || exit 93`: confirm the
        # backgrounded process is actually alive (catches an immediate exec
        # failure, e.g. `nohup: exec: no such file`, that would otherwise
        # leave a dead pid quietly sitting in the pidfile).
        script = (
            f't=$(mktemp) || exit 90; '
            f'cat > "$t" || exit 91; '
            f'exec 3<"$t"; '
            f'rm -f "$t"; '
            f'if command -v setsid >/dev/null 2>&1; then setsid nohup {cmd} <&3 > {log_expr} 2>&1 & '
            f'else nohup {cmd} <&3 > {log_expr} 2>&1 & fi; '
            f'pid=$!; '
            f'echo "$pid" > {pidfile_expr} || exit 92; '
            f'sleep 0.2; '
            f'kill -0 "$pid" 2>/dev/null || exit 93'
        )
        wrapped = ("sh", "-c", script)
        res = _remote(ctx, host_id, wrapped, timeout_s, stdin_path=stdin_path)
        if res.status == "completed":
            ctx.owned_remote_pidfiles.append({"host": host_id, "pidfile": pidfile})
    else:
        res = _remote(ctx, host_id, argv, timeout_s, stdin_path=stdin_path)
    return StepResult(ok=res.status == "completed", started_at=started_at, ended_at=now_iso(),
                      exit_code=res.exit_code, stdout_tail=_redact(res.stdout_preview),
                      stderr_tail=_redact(res.stderr_preview), timed_out=res.status == "timed_out",
                      detail=res.reason_code or "")


def _pid_guard(var: str = "p") -> str:
    """Shell fragment: reject anything that isn't a bare positive-integer pid
    read from a pidfile before it ever reaches a `kill`. Guards against a
    corrupt or hostile pidfile (e.g. containing `-1`, which `kill -TERM -1`
    would interpret as "signal every process this user can reach" — a
    broadcast, not a targeted signal)."""
    return f'case "${var}" in \'\'|*[!0-9]*) exit {{fail_exit}};; esac'


def _run_remote_signal(params: dict[str, Any], ctx: Ctx, timeout_s: float) -> StepResult:
    started_at = now_iso()
    host, pidfile = params["host"], params["pidfile"]
    sig, then, grace = params["signal"], params["then"], float(params["grace_s"])
    pidfile_expr = _remote_path_expr(pidfile)
    guard = _pid_guard().format(fail_exit=0)  # no valid pid to signal: a clean no-op, not a failure
    script = (f'p=$(cat {pidfile_expr} 2>/dev/null); {guard}; '
              f'kill -{sig} "$p" 2>/dev/null || exit 0; '
              f'for i in $(seq 1 {int(grace * 10)}); do kill -0 "$p" 2>/dev/null || exit 0; sleep 0.1; done; '
              f'kill -{then} "$p" 2>/dev/null; exit 0')
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
    out = ctx.evidence_dir / f"window-{time.time_ns()}.png"
    ok = bool(pids) and capture_app_window(pids=set(pids), out_path=out)
    return StepResult(ok, started_at, now_iso(), evidence_refs=[str(out)] if ok else [],
                      detail="" if ok else "no window captured")


def _run_http_action(params: dict[str, Any], ctx: Ctx, timeout_s: float) -> StepResult:
    started_at = now_iso()
    body = _http_get(params["url"], timeout_s)
    ok = body is not None
    evidence_refs: list[str] = []
    if ok:
        ctx.evidence_dir.mkdir(parents=True, exist_ok=True)
        out = ctx.evidence_dir / f"http-{time.time_ns()}.txt"
        out.write_text(_redact(body))
        evidence_refs = [str(out)]
    return StepResult(ok, started_at, now_iso(), evidence_refs=evidence_refs)


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

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every HTTP redirect. Checks/probes validate a loopback URL, but
    that loopback service could itself be tricked (or misconfigured) into
    issuing a 3xx to an off-box target; silently following it would turn a
    "read this local port" primitive into unbounded outbound egress."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _http_get(url: str, timeout_s: float = 5) -> str | None:
    try:
        with _OPENER.open(url, timeout=timeout_s) as r:  # noqa: S310 — loopback only (validator); no-redirect opener
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        exc.close()  # HTTPError wraps an open fp; close it or it leaks (ResourceWarning)
        return None
    except Exception:  # noqa: BLE001
        return None


def _http_status(url: str, timeout_s: float = 5) -> int | None:
    try:
        with _OPENER.open(url, timeout=timeout_s) as r:  # noqa: S310
            return r.status
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()  # HTTPError wraps an open fp; close it or it leaks (ResourceWarning)
        return code
    except Exception:  # noqa: BLE001
        return None


def _pgrep(pattern: str) -> list[int]:
    try:
        out = subprocess.run(["pgrep", "-f", pattern], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [int(x) for x in out.split() if x.isdigit() and int(x) != os.getpid()]


def _remote_pid_alive(ctx: Ctx, host: str, pidfile: str) -> bool:
    pidfile_expr = _remote_path_expr(pidfile)
    guard = _pid_guard().format(fail_exit=1)  # no valid pid on file: definitely not "alive"
    script = f'p=$(cat {pidfile_expr} 2>/dev/null); {guard}; kill -0 "$p"'
    res = _remote(ctx, host, ("sh", "-c", script), 15)
    return res.status == "completed"


def run_check(check: Check, ctx: Ctx, *, step_start: float) -> bool:
    k, p = check.kind, check.params
    if k == "all":
        return all(run_check(c, ctx, step_start=step_start) for c in p)
    if k == "exit0":
        argv = _guard(tuple(p["argv"]), ctx)
        outcome = _spawn_tracked(argv, None, 30, ctx)
        return outcome.spawn_error is None and outcome.returncode == 0 and not outcome.timed_out
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
        except (ValueError, KeyError, TypeError, IndexError):
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
        res = _remote(ctx, p["host"],
                      ("sh", "-c", f"stat -c %Y {shlex.quote(p['path'])} 2>/dev/null || stat -f %m {shlex.quote(p['path'])}"),
                      15)
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
