"""F087-10 — real test runs: turn command_ids into grounded pass/fail.

The tester role chooses WHICH allow-listed commands to run (by id, from the
project's test-command registry); this module runs them for real in the
project's isolated worktree via the F039 ``LocalToolRunner`` and derives the
verdict from the actual exit code. A model never asserts a pass — ``passed`` is
computed here from ``ToolRunnerResult``.

Fail-closed by construction: an unknown id, an empty plan, a non-zero exit, a
timeout, or a blocked launch all yield ``passed=False``. Execution is argv-only
(no shell) and confined to the worktree (the user's real tree is never run).

Import surface is stdlib + ``errorta_tools.runner`` only (the sanctioned
execution primitive) — no gateway / HTTP / aiar (Council invariant 3).
"""
from __future__ import annotations

import asyncio
import hashlib
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

_MAX_OUTPUT_BYTES = 2_000_000
_PREVIEW_CHARS = 4000


@dataclass(frozen=True)
class TestRunResult:
    command_id: str
    argv_sha256: str
    status: str                # completed | failed | timed_out | blocked
    exit_code: Optional[int]
    passed: bool               # status == "completed" AND exit_code == 0
    duration_ms: int
    stdout_sha256: str
    stdout_preview: str
    stderr_preview: str
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id, "argv_sha256": self.argv_sha256,
            "status": self.status, "exit_code": self.exit_code,
            "passed": self.passed, "duration_ms": self.duration_ms,
            "stdout_sha256": self.stdout_sha256,
            "stdout_preview": self.stdout_preview,
            "stderr_preview": self.stderr_preview, "reason": self.reason,
        }


@dataclass(frozen=True)
class TestRunSession:
    command_ids: list[str]
    results: list[TestRunResult]
    unknown_ids: list[str]
    passed: bool               # non-empty, no unknown ids, every result passed
    sandbox: str = ""          # F087-15 M4: the actual backend used (audit/UI)


def resolve_commands(
    registry: dict[str, Any], command_ids: list[str]
) -> tuple[list[tuple[str, dict[str, Any]]], list[str]]:
    """Split requested ids into (resolved, unknown). Unknown ids are the
    ``invalid_test_command`` fail-closed case — they are never run."""
    resolved: list[tuple[str, dict[str, Any]]] = []
    unknown: list[str] = []
    for cid in command_ids:
        spec = registry.get(cid)
        if isinstance(spec, dict):
            resolved.append((cid, spec))
        else:
            unknown.append(cid)
    return resolved, unknown


def _default_sandbox() -> str:
    """The best available native OS sandbox, else the constrained-subprocess
    tier. A requested-but-unavailable hardened sandbox would fail closed inside
    the runner, so we only ever ASK for one we know is available here."""
    from errorta_tools.runner.sandbox import (
        SANDBOX_BWRAP,
        SANDBOX_NONE,
        SANDBOX_SEATBELT,
        is_available,
    )
    for backend in (SANDBOX_SEATBELT, SANDBOX_BWRAP):
        if is_available(backend):
            return backend
    return SANDBOX_NONE


async def _run_one(
    cmd_id: str, spec: dict[str, Any], *, workspace_root: Path,
    runner_root: Path, sandbox: str,
) -> TestRunResult:
    from errorta_tools.runner.artifacts import RunnerArtifactStore
    from errorta_tools.runner.local import LocalToolRunner
    from errorta_tools.runner.types import ToolRunnerRequest

    argv = [str(a) for a in spec.get("argv", [])]
    cwd = str(spec.get("cwd", "."))
    timeout = float(spec.get("timeout_seconds", 120) or 120)

    runner_home = runner_root / "home"
    runner_tmp = runner_root / "tmp"
    runner_home.mkdir(parents=True, exist_ok=True)
    runner_tmp.mkdir(parents=True, exist_ok=True)

    call_id = f"test-{cmd_id}-{hashlib.sha256(repr(argv).encode()).hexdigest()[:8]}"
    request = ToolRunnerRequest(
        request_id=call_id, run_id=f"test-{cmd_id}", tool_call_id=call_id,
        argv=tuple(argv), workspace_root=str(workspace_root), relative_cwd=cwd,
        execution_location="local", timeout_seconds=timeout,
        max_output_bytes=_MAX_OUTPUT_BYTES, network_allowed=False,
        sandbox=sandbox,
        sandbox_writable_paths=(str(runner_home), str(runner_tmp)),
    )
    # F087-13 WS-5: defense-in-depth. The policy engine returns ALLOW on
    # action="allow" before its requires_approval check, so a test run must never
    # carry a network grant or explicit env that would otherwise need approval.
    # These are hardcoded above; assert it so a future change can't open a hole.
    assert request.network_allowed is False, "test runs must be network-off"
    assert not getattr(request, "explicit_env", ()), "test runs carry no explicit env"
    source_env = {"HOME": str(runner_home), "TMPDIR": str(runner_tmp),
                  "TMP": str(runner_tmp), "TEMP": str(runner_tmp)}
    import os
    source_env["PATH"] = os.environ.get("PATH", "")

    store = RunnerArtifactStore(root=runner_root / "artifacts")
    # The tester step is already authorized by the loop; the runner is not a
    # second approval — action=allow lets it run while still enforcing
    # workspace/location/env-allowlist/output-cap/sandbox.
    runner = LocalToolRunner(artifact_store=store, source_env=source_env,
                             policy={"action": "allow"})
    result = await runner.run(request)

    passed = result.status == "completed" and result.exit_code == 0
    reason = ""
    if not passed:
        if result.status == "blocked":
            reason = f"blocked: {result.reason_code or 'unknown'}"
        elif result.status == "timed_out":
            reason = f"timed out after {timeout}s"
        else:
            reason = f"exit {result.exit_code}"
    return TestRunResult(
        command_id=cmd_id, argv_sha256=request.argv_sha256,
        status=result.status, exit_code=result.exit_code, passed=passed,
        duration_ms=result.duration_ms,
        stdout_sha256=result.stdout_sha256,
        stdout_preview=result.stdout_preview[:_PREVIEW_CHARS],
        stderr_preview=result.stderr_preview[:_PREVIEW_CHARS], reason=reason)


def run_test_commands(
    workspace_root: Any, registry: dict[str, Any], command_ids: list[str], *,
    runner_root: Optional[Path] = None, sandbox: Optional[str] = None,
    should_cancel: Optional[Any] = None, require_sandbox: bool = False,
) -> TestRunSession:
    """Run each requested command for real and aggregate a grounded verdict.

    A session passes IFF: command_ids is non-empty, no id is unknown, and every
    resolved command exited 0. Anything else fails closed.

    F087-14 WS-1: ``should_cancel`` (a no-arg predicate) is checked BEFORE each
    command, so a cancel during a multi-command plan stops the run promptly
    instead of grinding through every command; a cancelled session fails closed.
    (A single already-launched command is bounded by its own timeout.)

    F087-15 M4: the resolved sandbox backend is recorded on the session (honest
    reporting — never claims "sandboxed" when it is ``none``). With
    ``require_sandbox`` and no OS sandbox available, the session fails closed
    (``sandbox_unavailable``) rather than running unjailed."""
    from errorta_tools.runner.sandbox import SANDBOX_NONE
    ws_root = Path(workspace_root)
    resolved, unknown = resolve_commands(registry, command_ids)
    if runner_root is None:
        runner_root = Path(tempfile.mkdtemp(prefix="f087-10-testrun-"))
    backend = sandbox if sandbox is not None else _default_sandbox()

    if require_sandbox and backend == SANDBOX_NONE:
        blocked = [TestRunResult(
            command_id=cid, argv_sha256="", status="blocked", exit_code=None,
            passed=False, duration_ms=0, stdout_sha256="", stdout_preview="",
            stderr_preview="", reason="sandbox_unavailable")
            for cid, _ in resolved]
        return TestRunSession(command_ids=list(command_ids), results=blocked,
                              unknown_ids=unknown, passed=False, sandbox=backend)

    results: list[TestRunResult] = []
    cancelled = False
    for idx, (cmd_id, spec) in enumerate(resolved):
        if should_cancel is not None and should_cancel():
            cancelled = True
            results.append(TestRunResult(
                command_id=cmd_id, argv_sha256="", status="blocked",
                exit_code=None, passed=False, duration_ms=0,
                stdout_sha256="", stdout_preview="", stderr_preview="",
                reason="cancelled before launch"))
            break
        results.append(asyncio.run(_run_one(
            cmd_id, spec, workspace_root=ws_root,
            runner_root=runner_root / f"c{idx}", sandbox=backend)))

    passed = (
        not cancelled
        and bool(command_ids) and not unknown and bool(resolved)
        and all(r.passed for r in results)
    )
    return TestRunSession(command_ids=list(command_ids), results=results,
                          unknown_ids=unknown, passed=passed, sandbox=backend)


# --------------------------------------------------------------------------- #
# F101 S4 — runtime test kinds (D6: new KINDS in the F087-10 registry, sharing
# the evidence-binds-to-head rule; NOT a duplicate registry).
#
# Unlike unit/typecheck/lint (argv commands from the per-project registry run by
# ``run_test_commands``), runtime kinds derive their verdict from a real
# sandboxed runtime SESSION via the S3 process manager: did the project start,
# reach a healthy health check, and does its demo respond? The manager owns the
# subprocess/HTTP egress, so this module stays import-clean.
# --------------------------------------------------------------------------- #
from .runtime import RUNTIME_TEST_KINDS  # re-export the canonical kind tuple


@dataclass(frozen=True)
class RuntimeTestResult:
    kind: str
    profile_id: str
    session_id: str
    passed: bool
    state: str                 # the runtime session's final state
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "profile_id": self.profile_id,
            "session_id": self.session_id, "passed": self.passed,
            "state": self.state, "detail": self.detail,
        }


def _drive_runtime(manager: Any, profile_id: str, *, timeout: float):
    """Start the runtime, wait for a stable outcome, return
    (session_id, final_session, reached_running, reached_healthy). The caller
    stops the runtime afterward (a runtime test never leaks a server)."""
    session = manager.start(profile_id)
    sid = session.session_id
    reached_running = reached_healthy = False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = manager.get_session(sid)
        if s is None:
            break
        if s.state in ("running", "healthy", "unhealthy"):
            reached_running = True
        if s.state == "healthy":
            reached_healthy = True
            break
        if s.state in ("stopped", "crashed"):
            break
        time.sleep(0.1)
    final = manager.get_session(sid)
    return sid, final, reached_running, reached_healthy


def _drive_launch(manager: Any, profile_id: str, *, liveness_seconds: float = 3.0):
    """Start a desktop/GUI runtime, watch it through the liveness window, grab a
    best-effort window screenshot, then tear it down. Returns
    (session_id, final_session, stayed_alive, crashed_early, got_screenshot)."""
    session = manager.start(profile_id)  # display inferred from a desktop kind
    sid = session.session_id
    deadline = time.monotonic() + liveness_seconds
    alive = crashed = False
    while time.monotonic() < deadline:
        s = manager.get_session(sid)
        if s is None:
            break
        if s.state in ("running", "healthy"):
            alive = True
        if s.state == "crashed" or (
                s.state == "stopped" and (s.exit_code or 0) != 0):
            crashed = True
            break
        time.sleep(0.1)
    shot = bool(manager.capture_screenshot(sid)) if alive and not crashed else False
    final = manager.get_session(sid)
    if final is not None and final.state not in ("stopped", "crashed"):
        manager.stop(profile_id)
    return sid, final, alive, crashed, shot


def _drive_cli(manager: Any, profile_id: str, *, timeout: float):
    """Run a one-shot CLI transcript via ``manager.run_cli`` and wait for the
    terminal session within the time-box. Returns
    (session_id, final_session). The run finalizes itself (it terminates) — no
    explicit stop is needed."""
    session = manager.run_cli(profile_id, timeout_seconds=timeout)
    sid = session.session_id
    # Wait a little past the run's own time-box for the monitor to land terminal.
    deadline = time.monotonic() + timeout + 5.0
    while time.monotonic() < deadline:
        s = manager.get_session(sid)
        if s is not None and s.state in ("stopped", "crashed"):
            return sid, s
        time.sleep(0.1)
    return sid, manager.get_session(sid)


def run_runtime_test(manager: Any, profile_id: str, kind: str, *,
                     head: str = "", timeout: float = 60.0) -> RuntimeTestResult:
    """Execute one runtime test kind against ``profile_id`` and return a grounded
    verdict (``passed`` is computed from the real session/health, never asserted
    by a model). The runtime is torn down before returning. ``head`` is the
    worktree head the caller binds the recorded evidence to."""
    if kind not in RUNTIME_TEST_KINDS:
        raise ValueError(f"unknown runtime test kind: {kind!r}")
    profile = manager.rstore.get_profile(profile_id)
    if profile is None:
        return RuntimeTestResult(kind=kind, profile_id=profile_id, session_id="",
                                 passed=False, state="crashed",
                                 detail="profile_not_found")

    # cli_transcript is a one-shot run (not a server start): grade "did the CLI
    # exit 0 against the current head?" via run_cli. WARN-only / non-blocking,
    # head-bound like every other runtime-test kind, and distinct from the demo
    # Run (CLI) — same manager method, separate evidence concept (spec D4).
    # launch (F101-03 desktop/GUI): "did it come up and stay up?" — liveness
    # (process up through the liveness window without a non-zero exit) plus a
    # best-effort screenshot of the app's own window. WARN-only, head-bound.
    if kind == "launch":
        sid, final, alive, crashed, shot = _drive_launch(manager, profile_id)
        state = final.state if final is not None else "crashed"
        passed = alive and not crashed
        if crashed:
            detail = f"crashed on startup (exit {final.exit_code if final else '?'})"
        elif shot:
            detail = "alive; window screenshot captured"
        else:
            detail = "alive; no screenshot (no display / window capture)"
        return RuntimeTestResult(kind=kind, profile_id=profile_id, session_id=sid,
                                 passed=passed, state=state, detail=detail)

    if kind == "cli_transcript":
        sid, final = _drive_cli(manager, profile_id, timeout=timeout)
        state = final.state if final is not None else "crashed"
        exit_code = final.exit_code if final is not None else None
        passed = state == "stopped" and exit_code == 0
        if final is not None and final.error == "timed_out":
            detail = "timed out"
        elif exit_code is not None:
            detail = f"exit {exit_code}"
        else:
            detail = state
        return RuntimeTestResult(kind=kind, profile_id=profile_id, session_id=sid,
                                 passed=passed, state=state, detail=detail)

    is_http = str((profile.health or {}).get("type")) == "http"
    demo_url = str((profile.demo or {}).get("type")) == "url"

    sid, final, reached_running, reached_healthy = _drive_runtime(
        manager, profile_id, timeout=timeout)
    state = final.state if final is not None else "crashed"
    exited_zero = state == "stopped" and (
        final is not None and final.exit_code == 0)

    detail = ""
    if kind == "runtime_start":
        passed = reached_running or exited_zero
    elif kind == "health_check":
        passed = reached_healthy if is_http else (reached_running or exited_zero)
        if final is not None and final.health_status:
            detail = str(final.health_status.get("detail", ""))
    else:  # demo_smoke
        if is_http and demo_url:
            probe = (
                manager.probe_demo(profile_id, session_id=sid)
                if reached_healthy else {"ok": False}
            )
            passed = reached_healthy and bool(probe.get("ok"))
            detail = str(probe.get("detail", ""))
        else:
            passed = exited_zero  # a CLI demo "smokes" by exiting 0

    if state not in ("stopped", "crashed"):
        manager.stop(profile_id)  # tear down a still-running server

    return RuntimeTestResult(kind=kind, profile_id=profile_id, session_id=sid,
                             passed=passed, state=state, detail=detail)


__all__ = [
    "TestRunResult", "TestRunSession", "resolve_commands", "run_test_commands",
    "RuntimeTestResult", "RUNTIME_TEST_KINDS", "run_runtime_test",
]
