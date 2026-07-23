"""Single-instance sidecar lifecycle for the CLI (F147 spec §4.2, §13.1 S9b).

There is **exactly one** sidecar per ``ERRORTA_HOME``, SHARED between the desktop
app and every ``errorta`` invocation. Front-ends discover it through
``${ERRORTA_HOME}/sidecar.json``:

    resolve():
      under a cross-process file lock on sidecar.lock:
        read sidecar.json
        if it points at a live, healthz-confirmed sidecar (app- OR cli-started)
        that is COORDINATED with us (our own cli sidecar, or ANY same-build
        sidecar):
            adopt it (co-drive is safe — one shared sidecar; S9a's cross-process
            run lock + owner_pid + owner-aware boot recovery keep the two
            front-ends from corrupting each other)
        elif a live but VERSION-SKEWED foreign sidecar is advertised:
            refuse (can't confirm a coordinated build — safe fallback)
        else:
            spawn a new one (self-re-exec `__serve__`), poll /healthz, persist

**S9b co-drive.** Earlier slices refused to run the CLI next to a desktop app at
all. Now the CLI ADOPTS the app's sidecar (and vice versa), so concurrent GUI+CLI
on one store is supported. The refusal narrows to the genuinely unsafe cases: a
version-skewed advertised sidecar, or — when NO adoptable sidecar is advertised —
a foreign ``Errorta.app`` / ``errorta-sidecar`` process (or a live sidecar on the
app's default port 8770) detected by ``detect_foreign_sidecar``. Spawning a
*second* sidecar next to an unadoptable foreign one is the corruption hazard
(its boot recovery / every ``GET /run`` could flip the other's ``running`` run to
``interrupted``), so that stays refused. Adopting a coordinated sidecar never
triggers a refusal. ``require_sole_owner`` stays as defense-in-depth: it refuses
only when we are NOT driving the single advertised sidecar (a real second one).

The HTTP probe and the process launch are module-level seams
(``probe_healthz`` / ``_launch``) so tests exercise the adopt-vs-spawn logic and
the foreign-app refusal without a real sidecar or a real subprocess.
"""
from __future__ import annotations

import atexit
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

_warned_lock_degraded = False


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


# --------------------------------------------------------------------------- #
# Watchdog client registry (F147 S9b) — refcount a shared/adopted sidecar.
# --------------------------------------------------------------------------- #

# Mirrors errorta_app.parent_watchdog's registry format WITHOUT importing it
# (golden invariant #1: errorta_cli imports nothing from errorta_app). A pidfile
# named/holding this process's pid under ${ERRORTA_HOME}/sidecar-clients/ marks
# the CLI as a live client, so an app-started (watchdog-supervised) sidecar we
# co-drive does not exit while a run we started is still in flight.
_CLIENTS_DIRNAME = "sidecar-clients"
_registered_client_atexit: set[int] = set()


def _clients_dir(home: Path) -> Path:
    return home / _CLIENTS_DIRNAME


def _register_client(home: Path) -> None:
    """Register this CLI process as a live watchdog client. Best-effort.

    Written AFTER the sidecar is confirmed live: an app-started sidecar already
    ran its watchdog startup, and a cli-started sidecar has no watchdog at all, so
    a late pidfile never spuriously converts the persistent CLI sidecar into a
    refcounted one. Deregistered on process exit (``atexit``) so a shared/adopted
    sidecar can exit once its last client is gone."""
    pid = os.getpid()
    try:
        d = _clients_dir(home)
        d.mkdir(parents=True, exist_ok=True)
        (d / str(pid)).write_text(str(pid), encoding="utf-8")
    except Exception:  # noqa: BLE001 - registration is best-effort
        return
    if pid not in _registered_client_atexit:
        _registered_client_atexit.add(pid)
        atexit.register(_unregister_client, home)


def _unregister_client(home: Path) -> None:
    """Remove this CLI's watchdog-client pidfile (a clean disconnect). Best-effort."""
    try:
        (_clients_dir(home) / str(os.getpid())).unlink()
    except FileNotFoundError:
        return
    except Exception:  # noqa: BLE001 - best-effort
        return


@contextlib.contextmanager
def _home_lock(home: Path) -> Iterator[None]:
    """Cross-process exclusive lock around the discover-or-spawn decision.

    Uses ``fcntl.flock`` on ``sidecar.lock`` so two concurrent ``errorta``
    launches can't double-spawn. On a platform without ``fcntl`` (Windows) this
    degrades to a no-op — acceptable for v1 (the Windows port is later work).
    """
    if not _HAVE_FCNTL:
        global _warned_lock_degraded
        if not _warned_lock_degraded:
            print(
                "warning: sidecar launch locking is unavailable on this platform; "
                "concurrent CLI launches may start duplicate sidecars.",
                file=sys.stderr,
            )
            _warned_lock_degraded = True
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

_ADOPT_PROBE_ATTEMPTS = 3
_ADOPT_PROBE_BACKOFF = 0.15


def _record_is_adoptable(record: dict) -> dict | None:
    """Return the live healthz body if ``record`` names a discoverable sidecar.

    F147 S9b — a live ``/healthz`` on the recorded port is all that's required to
    DISCOVER a candidate; ``started_by`` is NOT gated here (an ``app``-started
    sidecar is just as discoverable as a ``cli``-started one — the desktop app now
    advertises itself too). ``resolve`` applies the co-drive policy on top (adopt
    a coordinated same-build sidecar; refuse a version-skewed foreign one). Commit
    match is decided in ``resolve``, not here.

    Probes a few times before giving up: a single 1 s ``/healthz`` timeout on a
    momentarily-busy but *live* sidecar must not be read as "gone" (that would
    orphan it and double-spawn — see ``resolve``).
    """
    port = record.get("port")
    if not isinstance(port, int):
        return None
    for attempt in range(_ADOPT_PROBE_ATTEMPTS):
        body = probe_healthz(port)
        if body is not None:
            return body
        if attempt + 1 < _ADOPT_PROBE_ATTEMPTS:
            time.sleep(_ADOPT_PROBE_BACKOFF)
    return None


def _pid_alive(pid: object) -> bool:
    """Return True if ``pid`` names a live process (best-effort, signal 0)."""
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists but owned by another user — still alive
        return True
    except OSError:
        return False
    return True


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
    # F147 S9b: stamp who spawned it so its /healthz + sidecar.json advertisement
    # honestly report `started_by=cli` (the desktop app reads this when deciding
    # whether to adopt this shared sidecar).
    env.setdefault("ERRORTA_STARTED_BY", "cli")
    proc = _launch(_serve_argv(), env)

    try:
        body = _wait_ready(port, proc)
    except BaseException:
        # A failed spawn must not leave a detached child running: the next
        # invocation would find no record and spawn *another*, accumulating
        # orphan sidecars. Kill the child we launched before re-raising.
        _kill_child(proc)
        raise
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


def _kill_child(proc: subprocess.Popen | None) -> None:
    """Best-effort kill+reap of a spawned child (no-op if already dead)."""
    if proc is None:
        return
    with contextlib.suppress(Exception):
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


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
                started = str(record.get("started_by") or "unknown")
                mismatch = bool(
                    our_commit and commit and str(commit) != str(our_commit)
                )
                # F147 S9b co-drive policy + S9 follow-up (review LOW-3). Adopt
                # when we can confirm a COORDINATED, single shared sidecar:
                #   * our own previously-spawned CLI sidecar (started == "cli"): a
                #     sidecar this CLI started is coordinated BY CONSTRUCTION (we
                #     are its sole owner) regardless of whether a build commit is
                #     known — a mismatched commit there is just a harmless upgrade
                #     warning, NOT a reason to refuse; OR
                #   * a FOREIGN (app-/unknown-started) live sidecar whose build
                #     commit is KNOWN on both sides AND MATCHES ours — co-driving
                #     one same-build sidecar is safe (S9a's cross-process lock +
                #     owner_pid + owner-aware boot recovery coordinate the two
                #     front-ends).
                # LOW-3 safe fallback: for a FOREIGN sidecar an unknown/blank
                # commit on EITHER side means we CANNOT positively confirm a
                # coordinated build, so we fail toward NOT adopting it (refuse) —
                # mirroring the Rust app's "empty commit → never adopt". Only a
                # positively-confirmed match adopts a foreign sidecar; a
                # version-skewed OR unconfirmable foreign sidecar is neither
                # adopted nor spawned-next-to (safe fallback — concurrency simply
                # doesn't engage).
                commits_confirmed_match = bool(
                    our_commit and commit and str(commit) == str(our_commit)
                )
                coordinated = (started == "cli") or commits_confirmed_match
                if coordinated:
                    pid = record.get("pid")
                    port = int(record["port"])
                    handle = SidecarHandle(
                        base_url=f"http://127.0.0.1:{port}",
                        port=port,
                        pid=int(pid) if isinstance(pid, int) else None,
                        commit=str(commit) if commit else None,
                        started_by=started,
                        adopted=True,
                        commit_mismatch=mismatch,
                    )
                    _register_client(home)
                    return handle
                raise ForeignSidecar(
                    "refusing to co-drive a sidecar whose build could not be "
                    f"confirmed to match (it advertises commit {str(commit)!r}, "
                    f"this CLI is {str(our_commit)!r}). Update so both are known "
                    "and match, or use a separate --home — co-driving an "
                    "unconfirmed or mismatched build on one store can corrupt "
                    "in-flight work.",
                    code="foreign_sidecar",
                )
            # The record didn't yield a live /healthz. Only discard it if the
            # recorded process is genuinely GONE. A slow-but-live sidecar (probe
            # timed out under load) must not be nuked + duplicated — that orphans
            # the running one and double-spawns. Refuse instead of racing it.
            if _pid_alive(record.get("pid")):
                raise SidecarUnreachable(
                    f"the recorded sidecar (pid {record.get('pid')}, port "
                    f"{record.get('port')}) is not answering /healthz but its "
                    "process is still alive; refusing to spawn a duplicate. Run "
                    "`errorta sidecar restart` if it is wedged."
                )
            # Stale record (dead sidecar) — drop it before deciding to spawn.
            clear_record(home)
        if not allow_spawn:
            raise SidecarUnreachable(
                "no sidecar is running and --no-spawn was given"
            )
        # About to spawn a *new* sidecar on this ERRORTA_HOME. We reach here only
        # when NO adoptable sidecar was advertised. A second sidecar next to a
        # foreign desktop app / sidecar is the core B1 hazard: its boot recovery —
        # and any GET /run — flips the app's live run to `interrupted`, requeues
        # its `doing` tasks, and prunes its worktrees. So refuse to spawn when a
        # foreign owner is detected but could NOT be confirmed as an adoptable
        # coordinated sidecar — for ALL commands, reads included. (Adopting a
        # coordinated same-build sidecar above is always safe and never reaches
        # here.)
        reasons = detect_foreign_sidecar(home)
        if reasons:
            joined = "; ".join(reasons)
            raise ForeignSidecar(
                "refusing to start a second sidecar: "
                f"{joined}. Close the desktop app (or use a separate --home) and "
                "retry — a second sidecar on one store can corrupt in-flight "
                "work.",
                code="foreign_sidecar",
            )
        handle = spawn(home, our_commit=our_commit)
        _register_client(home)
        return handle


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

    An empty list means "sole owner, safe to spawn". Detection signals, in order
    of reliability:

    * **Primary** — a running ``Errorta.app`` / ``errorta-sidecar`` process
      (``psutil`` scan). This is the real signal for the *bundled* desktop app:
      its Tauri sidecar binds a random ephemeral port (``ERRORTA_SIDECAR_PORT``),
      so the fixed-8770 probe below structurally misses it. ``psutil`` is a
      declared dependency (``python/pyproject.toml``) and is collected into the
      frozen binary (``sidecar.spec`` hiddenimports + it is pulled in by
      ``errorta_hwdetect``); if it is somehow unimportable the scan degrades
      gracefully to "no signal" rather than crashing.
    * **Secondary** — a live sidecar on the app's default port (8770) that isn't
      our own. Only fires for a *dev* app launched on the fallback port.
    """
    reasons: list[str] = []

    for name in _scan_errorta_processes(exclude_pid=our_pid):
        reasons.append(f"the process {name!r} is running against this data")

    if our_port != APP_DEFAULT_PORT:
        if probe_healthz(APP_DEFAULT_PORT) is not None:
            reasons.append(
                "a sidecar is live on 127.0.0.1:8770 — the desktop app's "
                "default port"
            )

    return reasons


def _scan_errorta_processes(*, exclude_pid: int | None) -> list[str]:
    """Best-effort scan for a desktop-app / sidecar process.

    ``psutil`` is a declared dependency (``python/pyproject.toml``) and is
    bundled in the frozen binary, so this normally runs. It degrades gracefully
    to an empty list only in a stripped environment where ``psutil`` can't be
    imported (the guard is then silent — a known v1 limitation, spec §18 B1).
    """
    try:
        import psutil
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


def _driving_advertised_sidecar(home: Path, handle: SidecarHandle) -> bool:
    """Are we driving the ONE sidecar currently advertised in ``sidecar.json``?

    True when the on-disk record names the exact port we hold, a live ``/healthz``
    confirms it, AND the process actually serving there is the SAME one we're
    driving (pid match). That is the co-drive-safe case: a single, shared,
    coordinated sidecar (whether the app adopted ours or we adopted the app's)
    can't corrupt itself. If a DIFFERENT sidecar now owns the advertisement (e.g.
    an app that couldn't adopt spawned its own second one), our port won't match
    and this returns False → the foreign scan below decides.

    F147 S9 follow-up (review LOW-4): port equality alone is a narrow TOCTOU — a
    sidecar swap that reused the same port between adopt and this call would pass
    a port-only check. So we also confirm the live ``/healthz`` pid matches the
    pid we adopted/spawned (``handle.pid``), and that the advert names that same
    pid. SAFE FALLBACK: any uncertainty (no live healthz, healthz without a pid,
    a pid that differs, or an advert pid that differs) returns False → treated as
    NOT driving the shared sidecar → the conservative foreign scan decides."""
    record = read_record(home)
    if not record:
        return False
    if record.get("port") != handle.port:
        return False
    body = probe_healthz(handle.port)
    if body is None:
        return False
    # Confirm identity, not just the port. The sidecar answering on our port must
    # be the process we're driving. Our own sidecar's /healthz always reports its
    # pid (server.py); if it's missing or differs, we can't prove it's ours → not
    # driving (conservative).
    serving_pid = body.get("pid")
    if not isinstance(serving_pid, int):
        return False
    if not isinstance(handle.pid, int) or serving_pid != handle.pid:
        return False
    advert_pid = record.get("pid")
    if not isinstance(advert_pid, int) or advert_pid != serving_pid:
        return False
    return True


def require_sole_owner(home: Path, handle: SidecarHandle | None) -> None:
    """Raise :class:`ForeignSidecar` unless we're driving the coordinated sidecar.

    **Defense-in-depth** behind :func:`resolve`'s adopt/refuse decision. Called
    before every run/mutation command (spec §4.2).

    F147 S9b — co-driving is now *supported*: a desktop app and the CLI may share
    ONE sidecar via adoption. So this guard no longer refuses merely because an
    ``Errorta.app`` process exists. It refuses only an UNADOPTABLE foreign owner —
    i.e. when we are NOT driving the single advertised sidecar (a second,
    uncoordinated sidecar is genuinely present) — which is the real corruption
    hazard. When we hold the advertised shared sidecar, co-drive is safe and this
    is a no-op."""
    if handle is not None and _driving_advertised_sidecar(home, handle):
        return
    reasons = detect_foreign_sidecar(
        home,
        our_port=handle.port if handle else None,
        our_pid=handle.pid if handle else None,
    )
    if reasons:
        joined = "; ".join(reasons)
        raise ForeignSidecar(
            "refusing to co-drive an uncoordinated second sidecar: "
            f"{joined}. Close the desktop app (or use a separate --home) and "
            "retry — two sidecars on one store can corrupt in-flight work.",
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
    return resolve(home, allow_spawn=True, our_commit=our_commit)
