"""F101 S3 — egress primitives for the managed-local runtime preview.

This is the sanctioned egress boundary (``errorta_tools``) for F101's runtime
preview: spawning a sandboxed long-running child process and probing its HTTP
health endpoint. The orchestration (lifecycle state machine, process-group
teardown, log capping, threading) lives in
``errorta_council.coding.runtime_process`` and reaches the actual ``subprocess``
/ ``httpx`` egress ONLY through these two functions — mirroring how the F087-10
test runner reaches subprocess only through ``LocalToolRunner``. Keeping the
egress here is what lets ``errorta_council`` stay free of direct subprocess/HTTP
imports (Council invariant 3, enforced by the import-lint guards).

The child is wrapped through the F039 sandbox (``wrap_argv``). Network IS allowed
— a dev server must bind its loopback port and dependency installs must fetch —
so the isolation here is filesystem write confinement, not an air gap.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Mapping

from .sandbox import wrap_argv

# macOS GUI game engines (Godot, LÖVE) commonly install as ``.app`` bundles —
# or Homebrew *casks* — that put NO CLI binary on PATH, unlike cargo/go/docker.
# A GUI-launched app (the packaged Errorta.app) also inherits a minimal launchd
# PATH that usually excludes Homebrew, so even a ``brew``-installed engine may be
# invisible to ``shutil.which`` / ``execvp``. Map the detector's bare engine name
# to the binary inside a standard ``.app`` bundle as a PATH-independent fallback.
_MACOS_APP_ENGINES = {
    "godot": ("Godot.app", "Godot"),
    "love": ("love.app", "love"),
}
# Standard macOS application directories, searched in order. A module constant so
# tests can point it at a fixture bundle instead of the real ``/Applications``.
_MACOS_APP_DIRS: tuple[Path, ...] = (Path("/Applications"), Path.home() / "Applications")


def _resolve_macos_app_engine(cmd: str) -> str | None:
    """Absolute path to a macOS ``.app`` bundle engine binary for ``cmd`` when
    it isn't resolvable on PATH, or None (not macOS / not a known engine /
    bundle absent — fall through so the existing crash surfaces)."""
    if sys.platform != "darwin" or cmd not in _MACOS_APP_ENGINES:
        return None
    if shutil.which(cmd) is not None:
        return None  # a real CLI binary is on PATH — prefer it
    app_name, binary = _MACOS_APP_ENGINES[cmd]
    for base in _MACOS_APP_DIRS:
        candidate = base / app_name / "Contents" / "MacOS" / binary
        if candidate.exists():
            return str(candidate)
    return None


def _resolve_common_tool(argv: list[str]) -> list[str]:
    """Resolve common tool aliases before sandboxing.

    Generated projects and detectors naturally emit ``python`` / ``pip``.
    macOS and some modern Linux installs only provide ``python3`` / ``pip3``.
    Resolve those aliases through PATH before wrapping the command, otherwise
    sandbox-exec/bwrap reports an immediate child crash even though a usable
    interpreter is present. Likewise resolve macOS GUI engines (``godot`` /
    ``love``) that ship only as ``.app`` bundles with no CLI binary on PATH.
    """
    if not argv:
        return argv
    cmd = str(argv[0])
    rest = [str(a) for a in argv[1:]]
    if cmd == "python":
        resolved = shutil.which("python") or shutil.which("python3")
        return [resolved, *rest] if resolved else [cmd, *rest]
    if cmd == "pip":
        resolved = shutil.which("pip") or shutil.which("pip3")
        if resolved:
            return [resolved, *rest]
        py = shutil.which("python") or shutil.which("python3")
        return [py, "-m", "pip", *rest] if py else [cmd, *rest]
    app_engine = _resolve_macos_app_engine(cmd)
    if app_engine is not None:
        return [app_engine, *rest]
    return [cmd, *rest]


def spawn_sandboxed_child(
    *,
    backend: str,
    argv: list[str] | tuple[str, ...],
    workspace_root: str | Path,
    writable_paths: list[str | Path] | tuple[str | Path, ...] = (),
    cwd: str | Path,
    env: Mapping[str, str],
    network_allowed: bool = True,
    display_allowed: bool = False,
) -> subprocess.Popen:
    """Spawn ``argv`` as a sandboxed child in its own process group.

    ``start_new_session=True`` makes the child a session/group leader so the
    caller can tear the whole group down (SIGTERM/SIGKILL via ``os.killpg``).
    stdout+stderr are merged onto a pipe for the caller's log pump. Raises
    :class:`SandboxUnavailable` (fail closed) if the backend is unusable.

    ``display_allowed`` (F101-03 T1) lets a GUI child reach the OS window server
    without re-opening network or out-of-workspace writes.
    """
    resolved_argv = _resolve_common_tool([str(a) for a in argv])
    launcher = wrap_argv(
        backend=backend,
        argv=resolved_argv,
        workspace_root=str(workspace_root),
        writable_paths=[str(p) for p in writable_paths],
        network_allowed=network_allowed,
        display_allowed=display_allowed,
    )
    return subprocess.Popen(  # noqa: S603 — argv-only, no shell; sandboxed above
        launcher,
        cwd=str(cwd),
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )


def _macos_window_id_for_pids(pids: set[int]) -> int | None:
    """The on-screen window id owned by one of ``pids`` (frontmost/largest),
    via Quartz. Returns None if Quartz is unavailable or no window is owned —
    an honest 'no window to capture', never a raise."""
    try:
        import Quartz  # type: ignore
    except Exception:
        return None
    try:
        options = (
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements
        )
        infos = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID)
    except Exception:
        return None
    best_id: int | None = None
    best_area = -1
    for info in infos or []:
        owner = info.get("kCGWindowOwnerPID")
        if owner not in pids:
            continue
        bounds = info.get("kCGWindowBounds") or {}
        area = int(bounds.get("Width", 0)) * int(bounds.get("Height", 0))
        if area > best_area:
            best_area = area
            best_id = int(info.get("kCGWindowNumber", 0))
    return best_id


def capture_app_window(*, pids: set[int], out_path: str | Path) -> bool:
    """Best-effort capture of an app's OWN window (never the full desktop) to
    ``out_path``. macOS-only (``screencapture -l<id>``), gated on resolving the
    window id via Quartz. Returns False — an honest 'no screenshot' — when a
    display / Quartz / a matching window isn't available; never raises.

    Window-scoped by construction (spec threat model): only the app's window is
    captured, so no sensitive desktop content leaks into the evidence tree.
    """
    if sys.platform != "darwin":
        return False
    tool = shutil.which("screencapture") or "/usr/sbin/screencapture"
    if not Path(tool).exists():
        return False
    win_id = _macos_window_id_for_pids(pids)
    if win_id is None:
        return False
    out = Path(out_path)
    try:
        proc = subprocess.run(  # noqa: S603 — fixed tool, numeric window id
            [tool, "-x", "-o", f"-l{win_id}", str(out)],
            capture_output=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and out.exists()


def run_teardown_command(
    argv: list[str] | tuple[str, ...],
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = 30.0,
) -> int | None:
    """Run a short teardown command to completion (F101 S6 container cleanup,
    e.g. ``docker compose down``). Best-effort and bounded: returns the exit
    code, or ``None`` if it could not run / timed out. Never raises — teardown
    must not be blocked by a flaky cleanup command. argv-only, no shell."""
    try:
        proc = subprocess.run(  # noqa: S603 — argv-only, no shell
            _resolve_common_tool([str(a) for a in argv]),
            cwd=str(cwd) if cwd else None,
            env=dict(env) if env is not None else None,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
        return proc.returncode
    except Exception:
        return None


def probe_http(url: str, timeout: float) -> tuple[bool, str]:
    """One-shot HTTP health probe. Returns ``(ok, detail)`` where ``ok`` is a
    2xx/3xx response within ``timeout`` and ``detail`` is the status code or the
    exception class name (connection refused / timeout / etc.)."""
    if not url:
        return False, "no_url"
    try:
        import httpx
        resp = httpx.get(url, timeout=timeout)
        return (200 <= resp.status_code < 400), str(resp.status_code)
    except Exception as exc:  # connection refused / timeout / malformed url
        return False, type(exc).__name__


__all__ = ["spawn_sandboxed_child", "probe_http", "run_teardown_command"]
