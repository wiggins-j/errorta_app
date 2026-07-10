"""F039 — pluggable OS-level sandbox for the code_exec runner.

The base ``LocalToolRunner`` is a *constrained* subprocess (env allowlist,
workspace cwd, timeout, output cap) but it is NOT a filesystem/network jail: a
child can still read outside the workspace and open sockets. This module adds
an opt-in hardened tier that wraps the child argv in an OS sandbox:

- ``seatbelt`` — macOS ``sandbox-exec`` (native). Denies network, confines
  *writes* to the workspace + the per-run home/tmp, allows reads (interpreters
  and shared libraries live all over the filesystem). Present on every Mac.
- ``bwrap`` — Linux ``bubblewrap`` (native). A namespace sandbox: read-only
  host root for libs, read-write workspace + home/tmp, network namespace
  unshared unless granted. Lightweight — no daemon, no image.
- ``docker`` — a throwaway container with ``--network none`` (unless the call
  is granted network) and only the workspace bind-mounted read-write.
  Cross-platform / stronger isolation, but requires a reachable docker daemon
  + a base image (opt-in; heavier than the native backends).
- ``none`` — the legacy constrained-subprocess tier (default).

Each platform has a native, dependency-free backend (seatbelt / bwrap); docker
is the cross-platform fallback and the stronger-isolation choice where a daemon
is already available.

The wrapper is a pure ``argv`` transform: ``wrap_argv`` returns the launcher
argv to hand to ``asyncio.create_subprocess_exec``. Detection is fail-closed —
if a sandbox is *requested* but its backend is unavailable, callers must block
the launch rather than silently downgrade to ``none``.

This is the egress boundary (``errorta_tools``), not ``errorta_council``: it is
allowed to shell out to ``sandbox-exec`` / ``docker`` and inspect ``PATH``.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SANDBOX_NONE = "none"
SANDBOX_SEATBELT = "seatbelt"   # macOS native (sandbox-exec)
SANDBOX_BWRAP = "bwrap"         # Linux native (bubblewrap namespaces)
SANDBOX_DOCKER = "docker"       # cross-platform / stronger isolation (opt-in)

_KNOWN_BACKENDS = frozenset(
    {SANDBOX_NONE, SANDBOX_SEATBELT, SANDBOX_BWRAP, SANDBOX_DOCKER}
)

_DEFAULT_DOCKER_IMAGE = "python:3.12-slim"

# Docker image reference: [registry[:port]/]name[:tag][@digest]. Must NOT start
# with '-' (would be parsed as a docker flag in the image position — argv
# injection that could weaken isolation, e.g. --privileged).
_DOCKER_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]*$")


class SandboxUnavailable(RuntimeError):
    """A sandbox backend was requested but is not usable on this host.

    Carries a stable ``reason_code`` so the runner can surface it without
    leaking host detail.
    """

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def normalize_backend(name: str | None) -> str:
    backend = (name or SANDBOX_NONE).strip().lower()
    if backend not in _KNOWN_BACKENDS:
        # Unknown backend → fail closed (do not silently run unsandboxed).
        raise SandboxUnavailable("sandbox_backend_unknown")
    return backend


def is_available(backend: str) -> bool:
    """Whether ``backend`` can actually run on this host (no exceptions)."""
    if backend == SANDBOX_NONE:
        return True
    if backend == SANDBOX_SEATBELT:
        return sys.platform == "darwin" and shutil.which("sandbox-exec") is not None
    if backend == SANDBOX_BWRAP:
        if not sys.platform.startswith("linux") or shutil.which("bwrap") is None:
            return False
        # The binary existing is NOT enough — on hardened hosts (userns
        # disabled, restrictive seccomp, nested containers) bwrap can't create
        # the namespaces and exits non-zero at launch. Probe with the same core
        # flags the real wrap uses so detection matches reality (mirrors the
        # docker daemon probe). A probe failure -> unavailable -> fail closed.
        try:
            proc = subprocess.run(
                ["bwrap", "--unshare-user-try", "--unshare-pid", "--unshare-net",
                 "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                 "--", "true"],
                capture_output=True,
                timeout=8,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0
    if backend == SANDBOX_DOCKER:
        if shutil.which("docker") is None:
            return False
        try:
            proc = subprocess.run(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                timeout=8,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0
    return False


def _seatbelt_profile(
    *, writable: list[Path], network_allowed: bool, display_allowed: bool = False,
) -> str:
    """Build a seatbelt profile string.

    Strategy: allow by default (so interpreters/dylibs read freely), then deny
    the two things we actually want jailed — outbound network and writes
    outside the granted areas. ``literal``/``subpath`` filters use canonical
    (symlink-resolved) absolute paths.

    ``display_allowed`` (F101-03 T1 — sandboxed *windowed*) explicitly permits
    the WindowServer / render / font mach services a GUI app needs to draw a
    window. Under this ``(allow default)`` base those mach-lookups are already
    permitted, so the block is defensive/self-documenting; crucially it does NOT
    re-open ``network*`` or ``file-write*`` — a windowed app still cannot reach
    the network or write outside the workspace.
    """
    lines = [
        "(version 1)",
        "(allow default)",
    ]
    if not network_allowed:
        lines.append("(deny network*)")
    # Confine writes: deny all, then re-allow only the granted subpaths.
    lines.append("(deny file-write*)")
    for path in writable:
        resolved = str(path.resolve())
        escaped = resolved.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'(allow file-write* (subpath "{escaped}"))')
    # The child still needs the standard write devices (stdout/stderr/null/tty),
    # but a blanket /dev write-allow is broader than necessary — grant only the
    # specific nodes a normal program writes.
    lines.append(
        '(allow file-write-data'
        ' (literal "/dev/null") (literal "/dev/zero")'
        ' (literal "/dev/stdout") (literal "/dev/stderr")'
        ' (literal "/dev/dtracehelper") (subpath "/dev/tty") (subpath "/dev/fd"))'
    )
    if display_allowed:
        lines.append(
            "(allow mach-lookup"
            ' (global-name "com.apple.windowserver.active")'
            ' (global-name "com.apple.fonts")'
            ' (global-name "com.apple.FontServer")'
            ' (global-name "com.apple.CoreServices.coreservicesd")'
            " (global-name \"com.apple.tsm.uiserver\"))"
        )
    return "\n".join(lines) + "\n"


def _x11_socket_dir() -> Path:
    return Path("/tmp/.X11-unix")


def _bwrap_argv(
    *, writable: list[Path], workspace: Path, network_allowed: bool,
    base: list[str], display_allowed: bool = False,
) -> list[str]:
    """Build a bubblewrap launcher.

    Model mirrors seatbelt: the host root is bind-mounted READ-ONLY (so
    interpreters/shared libs resolve), then the workspace + per-run home/tmp are
    bind-mounted read-WRITE on top (later binds win in bwrap). A fresh
    ``/dev`` + ``/proc`` are provided and ``/run`` is masked so host runtime
    state doesn't leak. Network is denied by unsharing the net namespace unless
    explicitly granted.

    ``display_allowed`` (F101-03 T1) read-only-binds the X11 filesystem socket
    dir (``/tmp/.X11-unix``) and/or the Wayland socket so a GUI toolkit can draw
    a window. It binds the *filesystem* socket (not the abstract one), so the
    net namespace stays unshared — a windowed app still has no network and still
    writes only inside the workspace. ``DISPLAY`` / ``WAYLAND_DISPLAY`` are
    passed through by the caller's env allowlist.
    """
    args = [
        "bwrap",
        "--die-with-parent",
        "--unshare-user-try",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
    ]
    if not network_allowed:
        args.append("--unshare-net")
    args += [
        "--ro-bind", "/", "/",   # read-only host root for libs/interpreters
        # --ro-bind alone doesn't recursively re-mark pre-existing submounts
        # (a writable /var, /mnt/*, scratch volumes) read-only — remount the
        # root ro so write-confinement holds on multi-mount hosts.
        "--remount-ro", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        # Give a writable, EPHEMERAL /run + /tmp (not the host's): masks host
        # runtime state and keeps tools that hardcode /tmp working without
        # poking a hole in the read-only root. TMPDIR still points at the
        # rw-bound per-run tmp below.
        "--tmpfs", "/run",
        "--tmpfs", "/tmp",
    ]
    if display_allowed:
        x11 = _x11_socket_dir()
        if x11.exists():
            # Re-expose the X11 socket dir read-only on top of the ephemeral /tmp.
            args += ["--ro-bind", str(x11), str(x11)]
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
        wayland = os.environ.get("WAYLAND_DISPLAY")
        if runtime_dir and wayland:
            sock = Path(runtime_dir) / wayland
            if sock.exists():
                args += ["--ro-bind", str(sock), str(sock)]
    for path in writable:
        resolved = str(path.resolve())
        args += ["--bind", resolved, resolved]
    args += ["--chdir", str(workspace.resolve()), "--", *base]
    return args


def wrap_argv(
    *,
    backend: str,
    argv: list[str] | tuple[str, ...],
    workspace_root: str | Path,
    writable_paths: list[str | Path] | tuple[str | Path, ...] = (),
    network_allowed: bool = False,
    display_allowed: bool = False,
    docker_image: str | None = None,
) -> list[str]:
    """Return the launcher argv that runs ``argv`` under ``backend``.

    Raises :class:`SandboxUnavailable` (fail closed) if the backend is unknown
    or not usable on this host. ``backend == "none"`` returns ``argv`` verbatim.

    ``display_allowed`` (F101-03 T1) lets a GUI child reach the OS window server
    (seatbelt mach services / bwrap X11-Wayland socket bind) WITHOUT re-opening
    network or out-of-workspace writes. It is a no-op for ``none`` / ``docker``.
    """
    backend = normalize_backend(backend)
    base = [str(a) for a in argv]
    if not base:
        raise SandboxUnavailable("sandbox_empty_argv")
    if backend == SANDBOX_NONE:
        return base
    if not is_available(backend):
        raise SandboxUnavailable(f"sandbox_unavailable_{backend}")

    ws = Path(workspace_root)
    writable = [ws, *[Path(p) for p in writable_paths]]

    if backend == SANDBOX_SEATBELT:
        profile = _seatbelt_profile(
            writable=writable, network_allowed=network_allowed,
            display_allowed=display_allowed)
        return ["sandbox-exec", "-p", profile, *base]

    if backend == SANDBOX_BWRAP:
        return _bwrap_argv(
            writable=writable, workspace=ws,
            network_allowed=network_allowed, base=base,
            display_allowed=display_allowed,
        )

    if backend == SANDBOX_DOCKER:
        image = (docker_image or _DEFAULT_DOCKER_IMAGE).strip()
        if not image:
            raise SandboxUnavailable("sandbox_docker_image_missing")
        # Reject anything that isn't a plain image reference — a value starting
        # with '-' (or otherwise) would be parsed by `docker run` as a flag in
        # the image position (argv injection that could weaken isolation).
        if not _DOCKER_IMAGE_RE.match(image):
            raise SandboxUnavailable("sandbox_docker_image_invalid")
        ws_abs = str(ws.resolve())
        net = [] if network_allowed else ["--network", "none"]
        # Mount the workspace read-write at the same path; run there. The rest
        # of the container fs is ephemeral and discarded on --rm.
        return [
            "docker", "run", "--rm",
            *net,
            "--volume", f"{ws_abs}:{ws_abs}:rw",
            "--workdir", ws_abs,
            image,
            *base,
        ]

    raise SandboxUnavailable(f"sandbox_unavailable_{backend}")


__all__ = [
    "SANDBOX_BWRAP",
    "SANDBOX_DOCKER",
    "SANDBOX_NONE",
    "SANDBOX_SEATBELT",
    "SandboxUnavailable",
    "is_available",
    "normalize_backend",
    "wrap_argv",
]
