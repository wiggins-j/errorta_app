"""Unsandboxed execution for an operator-declared trusted gate (spec
2026-08-23-trusted-gate).

This module is the process-launch point for the trusted tier, the same way
``local.py`` is for the sandboxed tier: ``errorta_council`` code is not
allowed to import ``subprocess`` itself (see
``tests/council/test_tool_runner_local.py`` and
``tests/council/test_toolgateway_slice1.py``), so the actual ``Popen`` call
lives here, in ``errorta_tools``, and ``errorta_council.coding.trusted_gate``
only re-exports a thin wrapper around it.

This module must not import anything from ``errorta_council`` — not even
lazily — so it returns a plain ``TrustedExecResult`` rather than the
council's ``TestRunResult``; the caller converts.
"""
from __future__ import annotations

import hashlib
import os
import select
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .env import is_secret_env_name, sanitize_text
from .types import stable_json_sha256

MAX_OUTPUT_BYTES = 2_000_000
PREVIEW_CHARS = 4000

_READ_CHUNK = 65536
_POLL_S = 0.5
_KILL_WAIT_S = 5.0
_DRAIN_GRACE_S = 5.0
_ABANDONED_NOTE = "output abandoned: detached child held the pipe"


@dataclass(frozen=True)
class TrustedExecResult:
    """Same shape as ``errorta_council.coding.testing.TestRunResult`` on
    purpose — the caller converts field-for-field — but defined here so this
    package never has to import the council's dataclass to produce one."""

    command_id: str
    argv_sha256: str
    status: str  # completed | failed | timed_out | blocked
    exit_code: int | None
    passed: bool
    duration_ms: int
    stdout_sha256: str
    stdout_preview: str
    stderr_preview: str
    reason: str = ""


def passthrough_env(names: tuple[str, ...]) -> dict[str, str]:
    """Only the listed, non-secret names, only when set — values read NOW from
    the sidecar's environment, never from the file."""
    out: dict[str, str] = {}
    for n in names:
        if is_secret_env_name(n):
            continue
        v = os.environ.get(n)
        if v is not None:
            out[n] = v
    return out


class _ReaderState:
    """Shared between the main thread and one drain thread. ``deadline_at``
    is set by the main thread to tell the drain thread when to stop waiting
    for more data. ``eof`` is set by the drain thread only on a genuine
    end-of-file; ``error`` is set instead when OUR OWN read machinery
    (``select``/``read``/``set_blocking``) raised, so the caller can tell a
    real fd failure apart from a writer that simply never closes — the two
    must never be conflated, and neither counts as ``eof``.

    ``lock`` protects ``buf`` (owned by the caller, not this object) against
    the main thread taking a snapshot at abandonment while the drain thread
    is still mid-``extend`` — without it a snapshot could observe a torn
    write. Both sides hold it only for the duration of one bounded
    read-and-append or one bounded copy, never across a blocking call."""

    __slots__ = ("deadline_at", "eof", "error", "lock")

    def __init__(self) -> None:
        self.deadline_at: float | None = None  # monotonic() cutoff; None = no cutoff yet
        self.eof = False
        self.error: str | None = None
        self.lock = threading.Lock()


def _drain(stream, buf: bytearray, cap: int, state: _ReaderState) -> None:
    """Read a pipe to EOF, keeping at most ``cap`` bytes and discarding the
    rest, so the child can never block writing to a full pipe and captured
    memory stays bounded regardless of how much output it produces.

    Waits for readability with a short, bounded ``select()`` instead of a
    blocking ``read()``, and re-checks ``state.deadline_at`` at the TOP of
    every loop iteration — not only when a poll comes back empty — so a
    writer that keeps the pipe continuously readable (e.g. a detached
    grandchild spinning out output) can't starve the deadline check and keep
    this thread alive forever. Once the caller sets ``state.deadline_at``
    this thread stops within about ``_POLL_S`` of it passing, even if the
    pipe's write end is still held open by a process outside the group we
    killed. The thread owns the stream's full lifecycle, including closing
    it, entirely from within itself, so no other thread ever has to
    interrupt a call already in progress on the same stream object."""
    try:
        try:
            os.set_blocking(stream.fileno(), False)
        except (OSError, ValueError) as exc:
            state.error = type(exc).__name__
            return
        while True:
            if state.deadline_at is not None and time.monotonic() >= state.deadline_at:
                return
            try:
                ready, _, _ = select.select([stream], [], [], _POLL_S)
            except (OSError, ValueError) as exc:
                state.error = type(exc).__name__
                return
            if not ready:
                continue
            try:
                chunk = stream.read(_READ_CHUNK)
            except (BlockingIOError, InterruptedError):
                continue
            except (OSError, ValueError) as exc:
                state.error = type(exc).__name__
                return
            if chunk is None:  # non-blocking "no data yet" — NOT EOF
                time.sleep(0.001)  # don't busy-spin the select/read loop
                continue
            if not chunk:
                state.eof = True
                return
            if len(buf) < cap:
                with state.lock:
                    buf.extend(chunk[: cap - len(buf)])
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _terminate_group(proc: subprocess.Popen) -> bool:
    """Best-effort SIGTERM-then-SIGKILL of the whole process group, always
    bounded so a stubborn group — or one we can't even signal — can never
    hang the caller. Returns True once the process is confirmed dead (its
    exit status reaped), or False if even a final direct ``proc.kill()``
    couldn't get it reaped within the same bound: a bounded zombie beats an
    infinite hang, and the caller notes this in the record's reason as
    ``child_unreaped``.

    ``killpg`` can raise more than ``ProcessLookupError`` here: macOS
    returns ``EPERM`` (``PermissionError``) when the group's only member is
    a just-exited zombie, so any ``OSError`` from the signal itself is
    tolerated the same way for both TERM and KILL — the bounded ``wait``
    calls are what actually decide whether the process is gone."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            pass
        try:
            proc.wait(timeout=_KILL_WAIT_S)
            return True
        except subprocess.TimeoutExpired:
            continue
    # The group signal was never confirmed delivered (killpg may have raised
    # every time). Fall back to signaling just the direct child before we
    # give up and report a bounded zombie instead of hanging.
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=_KILL_WAIT_S)
        return True
    except subprocess.TimeoutExpired:
        return False


def run_trusted_command(
    spec: dict[str, Any], *, command_id: str, workspace_root: Path,
    env_passthrough: tuple[str, ...], should_cancel=None,
    _expose_threads: dict[str, threading.Thread] | None = None,
) -> TrustedExecResult:
    """Run ONE trusted command with no sandbox wrapper and the declared
    passthrough environment.

    Evidence fields mirror the sandboxed tier (``errorta_tools/runner/local.py``
    and ``errorta_council/coding/testing.py::_run_one``) on purpose: the same
    hash function, HEAD-not-tail previews run through the same sanitizer, and
    hashes taken over the same output-capped bytes, so a reader can't tell
    which tier produced a record just from its shape.

    ``_expose_threads`` is a test-only seam: pass a dict and this function
    fills in ``out_thread``/``err_thread`` with the actual reader ``Thread``
    objects before returning, so a test can assert on their liveness after
    the call — the public contract never needs this."""

    def _blocked(reason: str) -> TrustedExecResult:
        return TrustedExecResult(command_id=command_id, argv_sha256="", status="blocked",
                                 exit_code=None, passed=False, duration_ms=0, stdout_sha256="",
                                 stdout_preview="", stderr_preview="", reason=reason)

    invalid = spec.get("invalid")
    if invalid:
        return _blocked(f"trusted_gate_invalid:{invalid}")
    if should_cancel is not None and should_cancel():
        return _blocked("cancelled before launch")
    argv = [str(a) for a in spec.get("argv", [])]
    if not argv:
        return _blocked("empty_argv")
    cwd = (Path(workspace_root) / str(spec.get("cwd", "."))).resolve()
    try:
        cwd.relative_to(Path(workspace_root).resolve())
    except ValueError:
        return _blocked("cwd_outside_workspace")
    timeout = float(spec.get("timeout_seconds", 1) or 1)
    argv_sha = stable_json_sha256(argv)
    env = passthrough_env(env_passthrough)
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(  # noqa: S603 — operator-declared, validated argv; no shell
            argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    except OSError as exc:
        return TrustedExecResult(command_id=command_id, argv_sha256=argv_sha, status="failed",
                                 exit_code=None, passed=False, duration_ms=0, stdout_sha256="",
                                 stdout_preview="", stderr_preview=str(exc)[:PREVIEW_CHARS],
                                 reason=f"launch failed: {type(exc).__name__}")

    out_buf, err_buf = bytearray(), bytearray()
    out_state, err_state = _ReaderState(), _ReaderState()
    out_thread = threading.Thread(
        target=_drain, args=(proc.stdout, out_buf, MAX_OUTPUT_BYTES, out_state), daemon=True)
    err_thread = threading.Thread(
        target=_drain, args=(proc.stderr, err_buf, MAX_OUTPUT_BYTES, err_state), daemon=True)
    out_thread.start()
    err_thread.start()
    if _expose_threads is not None:
        _expose_threads["out_thread"] = out_thread
        _expose_threads["err_thread"] = err_thread

    status = "completed"
    child_unreaped = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        status = "timed_out"
        if not _terminate_group(proc):
            child_unreaped = True

    # The process itself is done (or forcibly killed, or left as a bounded
    # zombie), but a detached grandchild (e.g. a backgrounded ``setsid``
    # daemon that inherited our stdout/stderr fds) can keep a pipe's write
    # end open forever. Give the reader threads one bounded grace window —
    # shared across both streams, not stacked — to drain what they can, then
    # let them go either way. A thread that already reached true EOF returns
    # near-instantly regardless of this deadline, so the common case pays
    # none of this latency.
    cutoff = time.monotonic() + _DRAIN_GRACE_S
    out_state.deadline_at = cutoff
    err_state.deadline_at = cutoff
    join_deadline = cutoff + _POLL_S * 2  # slack for the select() poll granularity
    out_thread.join(timeout=max(0.0, join_deadline - time.monotonic()))
    err_thread.join(timeout=max(0.0, join_deadline - time.monotonic()))
    # A thread that stopped without ever reaching real EOF — whether it's
    # still running past our join budget, hit a genuine read error, or gave
    # up at the deadline — never produced a TRUSTWORTHY (complete) capture,
    # but it may still hold real, honest partial output (e.g. a detached
    # grandchild held the pipe open after the command's own output was
    # already written). Snapshot under the lock rather than discard it: the
    # abandonment note in `reason` already tells the reader this capture may
    # be incomplete, so keeping the partial bytes is strictly more honest
    # than reporting nothing.
    output_abandoned = not (out_state.eof and err_state.eof)
    with out_state.lock:
        out = bytes(out_buf)
    with err_state.lock:
        err = bytes(err_buf)

    duration_ms = int((time.monotonic() - t0) * 1000)
    exit_code = proc.returncode
    if status == "completed" and exit_code != 0:
        status = "failed"
    passed = status == "completed" and exit_code == 0

    reader_errors = [(name, st.error) for name, st in
                     (("stdout", out_state), ("stderr", err_state)) if st.error]
    notes: list[str] = []
    if child_unreaped:
        notes.append("child_unreaped")
    if reader_errors:
        notes.extend(f"{name} reader_error:{err}" for name, err in reader_errors)
    elif output_abandoned:
        notes.append(_ABANDONED_NOTE)
    note = f" ({'; '.join(notes)})" if notes else ""

    if passed:
        reason = "; ".join(notes)
    elif status == "timed_out":
        reason = f"timed out after {timeout}s{note}"
    else:
        reason = f"exit {exit_code}{note}"

    return TrustedExecResult(
        command_id=command_id, argv_sha256=argv_sha, status=status, exit_code=exit_code,
        passed=passed, duration_ms=duration_ms, stdout_sha256=hashlib.sha256(out).hexdigest(),
        stdout_preview=sanitize_text(out.decode("utf-8", "replace"))[:PREVIEW_CHARS],
        stderr_preview=sanitize_text(err.decode("utf-8", "replace"))[:PREVIEW_CHARS],
        reason=reason)


__all__ = ["MAX_OUTPUT_BYTES", "PREVIEW_CHARS", "TrustedExecResult", "passthrough_env",
           "run_trusted_command"]
