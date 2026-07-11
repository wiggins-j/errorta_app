"""CLI-owned single-instance sidecar lifecycle (F147 spec §4.2).

The CLI owns **exactly one** sidecar per ``ERRORTA_HOME``. Successive ``errorta``
invocations and multiple terminals share that one sidecar by discovering it
through ``${ERRORTA_HOME}/sidecar.json``:

    resolve():
      under a cross-process file lock on sidecar.lock:
        read sidecar.json
        if it points at a live, healthz-confirmed, started_by=="cli" sidecar:
            adopt it
        else:
            spawn a new one (self-re-exec `__serve__`), poll /healthz, persist

Foreign-app detection (``detect_foreign_sidecar``): the desktop app spawns its
sidecar on a random in-memory port that an external CLI cannot discover, and its
``GET /run`` recovery sweep would corrupt a run live in another process. So v1
refuses to *co-drive* a running app: before any run/mutation, the CLI checks for
a live sidecar on the app's default port (8770) that isn't its own, or an
``Errorta.app`` / ``errorta-sidecar`` process, and refuses (reads still proceed).

The HTTP probe and the process launch are module-level seams
(``probe_healthz`` / ``_launch``) so tests exercise the adopt-vs-spawn logic and
the foreign-app refusal without a real sidecar or a real subprocess.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import httpx

from . import config
from .errors import ForeignSidecar, SidecarUnreachable

# The desktop app's default sidecar port (server.py:_resolve_port). Used only to
# *detect* a foreign app — the CLI never drives a foreign sidecar.
APP_DEFAULT_PORT = 8770

_HEALTHZ_PROBE_TIMEOUT = 1.0
_SPAWN_READY_BUDGET = 15.0
_SPAWN_POLL_INTERVAL = 0.1

try:  # POSIX cross-process file lock; absent on Windows (handled gracefully).
    import fcntl

    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover — Windows only
    _HAVE_FCNTL = False


@dataclass
class SidecarHandle:
    """A resolved, reachable CLI sidecar."""

    base_url: str
    port: int
    pid: int | None
    commit: str | None
    started_by: str
    adopted: bool
    commit_mismatch: bool = False


# --------------------------------------------------------------------------- #
# Seams — monkeypatched in tests.
# --------------------------------------------------------------------------- #

def probe_healthz(port: int, *, timeout: float = _HEALTHZ_PROBE_TIMEOUT) -> dict | None:
    """GET ``/healthz`` on loopback ``port``; return the JSON body or ``None``.

    Never raises — an unreachable port simply means "no live sidecar here".
    """
    url = f"http://127.0.0.1:{port}/healthz"
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        body = resp.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _launch(argv: list[str], env: dict[str, str]) -> subprocess.Popen:
    """Spawn the sidecar process (its own session so it outlives this call)."""
    return subprocess.Popen(  # noqa: S603 — fixed argv, no shell
        argv,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


# --------------------------------------------------------------------------- #
# sidecar.json + the discover-or-spawn lock.
# --------------------------------------------------------------------------- #

def read_record(home: Path) -> dict | None:
    """Read ``${ERRORTA_HOME}/sidecar.json``; ``None`` if absent/unreadable."""
    path = config.sidecar_record_path(home)
    try:
        raw = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def write_record(home: Path, record: dict) -> None:
    """Atomically write the sidecar discovery record."""
    path = config.sidecar_record_path(home)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record), "utf-8")
    os.replace(tmp, path)


def clear_record(home: Path) -> None:
    with contextlib.suppress(OSError):
        config.sidecar_record_path(home).unlink()


@contextlib.contextmanager
def _home_lock(home: Path) -> Iterator[None]:
    """Cross-process exclusive lock around the discover-or-spawn decision.

    Uses ``fcntl.flock`` on ``sidecar.lock`` so two concurrent ``errorta``
    launches can't double-spawn. On a platform without ``fcntl`` (Windows) this
    degrades to a no-op — acceptable for v1 (the Windows port is later work).
    """
    if not _HAVE_FCNTL:
        yield
        return
    lock_path = config.sidecar_lock_path(home)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- #
# Resolve: adopt an existing CLI sidecar, else spawn one.
# --------------------------------------------------------------------------- #

def _record_is_adoptable(record: dict) -> dict | None:
    """Return the live healthz body if ``record`` names an adoptable sidecar.

    Adoptable = ``started_by == "cli"`` and a live ``/healthz`` on its port.
    Commit match is a *warning*, not an adoption gate (spec §4.2 step 4).
    """
    if record.get("started_by") != "cli":
        return None
    port = record.get("port")
    if not isinstance(port, int):
        return None
    return probe_healthz(port)


def _healthz_commit(body: dict | None) -> str | None:
    if not isinstance(body, dict):
        return None
    build = body.get("build")
    if isinstance(build, dict):
        commit = build.get("commit")
        return str(commit) if commit else None
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve_argv() -> list[str]:
    """Argv to re-exec *this* executable into the embedded sidecar.

    Frozen binary → ``[<self>, "__serve__"]`` (re-exec the multicall binary).
    Dev → ``[python, "-m", "errorta_cli", "__serve__"]`` (same self-re-exec via
    the package ``__main__``).
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "__serve__"]
    return [sys.executable, "-m", "errorta_cli", "__serve__"]


def spawn(home: Path, *, our_commit: str | None = None) -> SidecarHandle:
    """Spawn a fresh CLI-owned sidecar, wait for readiness, persist the record."""
    port = _free_port()
    env = {**os.environ, "ERRORTA_SIDECAR_PORT": str(port), "ERRORTA_HOME": str(home)}
    env.setdefault("ERRORTA_CLI_SIDECAR", "1")
    proc = _launch(_serve_argv(), env)

    body = _wait_ready(port, proc)
    commit = _healthz_commit(body) or our_commit
    record = {
        "port": port,
        "pid": proc.pid,
        "commit": commit,
        "started_by": "cli",
    }
    write_record(home, record)
    return SidecarHandle(
        base_url=f"http://127.0.0.1:{port}",
        port=port,
        pid=proc.pid,
        commit=commit,
        started_by="cli",
        adopted=False,
    )


def _wait_ready(port: int, proc: subprocess.Popen | None) -> dict | None:
    """Poll ``/healthz`` until the spawned sidecar answers (bounded)."""
    deadline = time.monotonic() + _SPAWN_READY_BUDGET
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            raise SidecarUnreachable(
                f"the sidecar exited during startup (code {proc.returncode})"
            )
        body = probe_healthz(port)
        if body is not None:
            return body
        time.sleep(_SPAWN_POLL_INTERVAL)
    raise SidecarUnreachable(
        f"the sidecar did not become ready on port {port} within "
        f"{_SPAWN_READY_BUDGET:.0f}s"
    )


def resolve(
    home: Path,
    *,
    allow_spawn: bool = True,
    our_commit: str | None = None,
) -> SidecarHandle:
    """Resolve the CLI's sidecar: adopt the running CLI one, else spawn.

    The whole decision runs under the cross-process home lock so two launches
    never double-spawn. ``allow_spawn=False`` (``--no-spawn``, for CI) turns a
    missing sidecar into :class:`SidecarUnreachable` instead of spawning.
    """
    if our_commit is None:
        our_commit = config.build_commit()
    with _home_lock(home):
        record = read_record(home)
        if record is not None:
            body = _record_is_adoptable(record)
            if body is not None:
                commit = _healthz_commit(body) or record.get("commit")
                mismatch = bool(
                    our_commit and commit and str(commit) != str(our_commit)
                )
                pid = record.get("pid")
                port = int(record["port"])
                return SidecarHandle(
                    base_url=f"http://127.0.0.1:{port}",
                    port=port,
                    pid=int(pid) if isinstance(pid, int) else None,
                    commit=str(commit) if commit else None,
                    started_by="cli",
                    adopted=True,
                    commit_mismatch=mismatch,
                )
            # Stale record (dead sidecar) — drop it before deciding to spawn.
            clear_record(home)
        if not allow_spawn:
            raise SidecarUnreachable(
                "no CLI sidecar is running and --no-spawn was given"
            )
        return spawn(home, our_commit=our_commit)


# --------------------------------------------------------------------------- #
# Foreign-app detection + the sole-owner guard.
# --------------------------------------------------------------------------- #

def detect_foreign_sidecar(
    home: Path,
    *,
    our_port: int | None = None,
    our_pid: int | None = None,
) -> list[str]:
    """Return human-readable reasons a foreign app owns this ``ERRORTA_HOME``.

    An empty list means "sole owner, safe to mutate". Detection signals:

    * a live sidecar on the app's default port (8770) that isn't our own; and
    * a running ``Errorta.app`` / ``errorta-sidecar`` process (best-effort — only
      when ``psutil`` happens to be importable; never a hard dependency).
    """
    reasons: list[str] = []

    if our_port != APP_DEFAULT_PORT:
        if probe_healthz(APP_DEFAULT_PORT) is not None:
            reasons.append(
                "a sidecar is live on 127.0.0.1:8770 — the desktop app's "
                "default port"
            )

    for name in _scan_errorta_processes(exclude_pid=our_pid):
        reasons.append(f"the process {name!r} is running against this data")

    return reasons


def _scan_errorta_processes(*, exclude_pid: int | None) -> list[str]:
    """Best-effort scan for a desktop-app / sidecar process (needs psutil)."""
    try:
        import psutil  # optional; not a declared CLI dependency
    except ImportError:
        return []
    hits: list[str] = []
    needles = ("errorta.app", "errorta-sidecar")
    self_pid = os.getpid()
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            pid = proc.info.get("pid")
            if pid in (self_pid, exclude_pid):
                continue
            haystack = " ".join(
                str(proc.info.get(k) or "") for k in ("name", "exe")
            ).lower()
        except (psutil.Error, KeyError):  # pragma: no cover — race on exit
            continue
        for needle in needles:
            if needle in haystack:
                hits.append(str(proc.info.get("name") or needle))
                break
    return hits


def require_sole_owner(home: Path, handle: SidecarHandle | None) -> None:
    """Raise :class:`ForeignSidecar` if a foreign app owns this store.

    Called before every run/mutation command in v1 (spec §4.2 step 3). Reads do
    not call this — read-only inspection may proceed alongside a running app.
    """
    reasons = detect_foreign_sidecar(
        home,
        our_port=handle.port if handle else None,
        our_pid=handle.pid if handle else None,
    )
    if reasons:
        joined = "; ".join(reasons)
        raise ForeignSidecar(
            "refusing to co-drive: "
            f"{joined}. Close the desktop app (or use a separate --home) and "
            "retry — concurrent GUI+CLI use on one store can corrupt in-flight "
            "work.",
            code="foreign_sidecar",
        )


# --------------------------------------------------------------------------- #
# Explicit lifecycle controls: `errorta sidecar {status,stop,restart}`.
# --------------------------------------------------------------------------- #

def status(home: Path) -> dict[str, Any]:
    """Describe the CLI-owned sidecar without spawning one."""
    record = read_record(home)
    if record is None:
        return {"running": False, "record": None, "healthz": None}
    port = record.get("port")
    body = probe_healthz(port) if isinstance(port, int) else None
    return {"running": body is not None, "record": record, "healthz": body}


def stop(home: Path) -> dict[str, Any]:
    """Stop the CLI-owned sidecar (SIGTERM) and drop its record."""
    record = read_record(home)
    if record is None:
        return {"stopped": False, "reason": "no CLI sidecar record"}
    pid = record.get("pid")
    stopped = False
    if isinstance(pid, int):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(pid, 15)
            stopped = True
    clear_record(home)
    return {"stopped": stopped, "pid": pid}


def restart(home: Path, *, our_commit: str | None = None) -> SidecarHandle:
    """Stop the current CLI sidecar and spawn a fresh one."""
    stop(home)
    return spawn(home, our_commit=our_commit)
