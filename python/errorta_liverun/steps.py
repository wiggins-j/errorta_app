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
    request = _remote_request(ctx, host_id, argv, timeout_s)
    # Audit-only policy evaluation ahead of every remote egress: this call site
    # always passes a fixed allow policy (the launch cap / operator-authorship
    # gates already happened at profile-validation time), so the decision here
    # exists to produce an auditable record, not to change behaviour. Fail
    # closed anyway if a future policy configuration ever returns non-ALLOW.
    decision = evaluate_runner_launch(request, phase=PolicyPhase.REMOTE_EGRESS,
                                      policy={"action": "allow"})
    if decision.action != PolicyAction.ALLOW:
        return _blocked_remote_result(request, decision.reason_code)
    return ctx.remote.run_sync(request, stdin_path=stdin_path)


def _blocked_remote_result(request: ToolRunnerRequest, reason_code: str | None) -> ToolRunnerResult:
    return ToolRunnerResult.blocked(request=request, reason_code=reason_code or "policy_denied")


# --- actions ------------------------------------------------------------- #

def _run_local(params: dict[str, Any], ctx: Ctx, timeout_s: float) -> StepResult:
    argv = _guard(tuple(params["argv"]), ctx)
    started_at = now_iso()
    ctx.evidence_dir.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.Popen(  # noqa: S603 — validated, profile-declared argv
            list(argv), cwd=params.get("cwd") or None, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    except OSError as exc:
        return StepResult(False, started_at, now_iso(), detail=f"spawn failed: {exc}")
    # Recorded before awaiting the child: a supervisor/teardown reaper walks
    # this list to guarantee no local process group outlives its Ctx even if
    # the awaiting caller itself is interrupted. Left in place after a clean
    # exit too — killpg-ing an already-dead pgid is a harmless no-op (see
    # `_killpg`), and the alternative (removing it here, synchronously, the
    # instant `communicate()` returns) would make the accounting useless for
    # anything that inspects it right after `run_action` returns.
    ctx.owned_pgids.append(proc.pid)
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _killpg(proc.pid)
        out, err = proc.communicate()
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
        # Read stdin (the token file, if any) into a remote temp file BEFORE
        # backgrounding the process, then remove it AFTER the process has
        # started reading from its own fd — the fd stays open across the
        # `rm`, and this avoids handing a live ssh-session stdin (which
        # `eval` under the fake-ssh test harness would keep open) to the
        # detached child.
        # `setsid` (full session detach) is used when the remote box has it
        # (typical Linux target); it is util-linux-only and absent on macOS,
        # so fall back to plain `nohup` there — under a non-interactive ssh
        # command there is no controlling tty to detach from either way.
        redirected = f"{shlex.join(argv)} > {shlex.quote(log)} 2>&1 < \"$t\""
        wrapped = ("sh", "-c",
                   f"t=$(mktemp); cat > \"$t\"; "
                   f"if command -v setsid >/dev/null 2>&1; then setsid nohup {redirected} & "
                   f"else nohup {redirected} & fi; "
                   f"echo $! > {shlex.quote(pidfile)}; rm -f \"$t\"")
        res = _remote(ctx, host_id, wrapped, timeout_s, stdin_path=stdin_path)
        if res.status == "completed":
            ctx.owned_remote_pidfiles.append({"host": host_id, "pidfile": pidfile})
    else:
        res = _remote(ctx, host_id, argv, timeout_s, stdin_path=stdin_path)
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
    except urllib.error.HTTPError as exc:
        return exc.code
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
    res = _remote(ctx, host, ("sh", "-c", f"kill -0 $(cat {shlex.quote(pidfile)})"), 15)
    return res.status == "completed"


def run_check(check: Check, ctx: Ctx, *, step_start: float) -> bool:
    k, p = check.kind, check.params
    if k == "all":
        return all(run_check(c, ctx, step_start=step_start) for c in p)
    if k == "exit0":
        argv = _guard(tuple(p["argv"]), ctx)
        try:
            return subprocess.run(  # noqa: S603 — validated, profile-declared argv
                list(argv), stdin=subprocess.DEVNULL, capture_output=True, timeout=30).returncode == 0
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
