"""F089 — the SSH tunnel manager: spec, hardened argv, lifecycle, watcher."""
from __future__ import annotations

import os
import re
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

# --- states -----------------------------------------------------------------
STATE_DOWN = "down"
STATE_CONNECTING = "connecting"
STATE_UP = "up"
STATE_RECONNECTING = "reconnecting"
STATE_ERROR = "error"

# A host/user/host-name token. No leading '-' (flag injection), no shell chars.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Watcher cadence + connect budget + backoff ceiling (seconds).
_WATCH_INTERVAL = 5.0
_CONNECT_BUDGET = 8.0
_BACKOFF_MAX = 30.0
_STDERR_TAIL = 2000


class TunnelValidationError(ValueError):
    """A spec field failed validation (rejected before reaching argv)."""


def _now_iso_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _validate_token(value: str, *, what: str) -> str:
    v = (value or "").strip()
    if not _TOKEN_RE.match(v):
        raise TunnelValidationError(f"invalid {what}: {value!r}")
    return v


def _validate_port(value: int, *, what: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise TunnelValidationError(f"{what} must be in 1..65535, got {value!r}")
    return port


@dataclass(frozen=True)
class TunnelSpec:
    """The dedup key for a managed tunnel. Two consumers asking for the same spec
    share one ssh child. ``ssh_host`` is an alias resolved via ``~/.ssh/config``."""
    ssh_host: str
    remote_port: int
    remote_host: str = "127.0.0.1"
    ssh_port: Optional[int] = None
    ssh_username: Optional[str] = None
    ssh_key_path: Optional[str] = None

    def validated(self) -> "TunnelSpec":
        ssh_host = _validate_token(self.ssh_host, what="ssh_host")
        remote_host = _validate_token(self.remote_host, what="remote_host")
        remote_port = _validate_port(self.remote_port, what="remote_port")
        ssh_port = _validate_port(self.ssh_port, what="ssh_port") if self.ssh_port else None
        ssh_username = (
            _validate_token(self.ssh_username, what="ssh_username")
            if self.ssh_username else None
        )
        key = self.ssh_key_path
        if key:
            key = os.path.expanduser(str(key))
            if not os.path.isfile(key):
                raise TunnelValidationError(f"ssh_key_path is not a file: {key!r}")
        return TunnelSpec(
            ssh_host=ssh_host, remote_port=remote_port, remote_host=remote_host,
            ssh_port=ssh_port, ssh_username=ssh_username, ssh_key_path=key)

    def label(self) -> str:
        user = f"{self.ssh_username}@" if self.ssh_username else ""
        return f"{user}{self.ssh_host}:{self.remote_host}:{self.remote_port}"


def build_ssh_argv(spec: TunnelSpec, local_port: int, *, ssh_bin: str = "ssh") -> list[str]:
    """Fixed argv (no shell) for an ``ssh -N -L`` loopback forward. The spec must
    already be ``.validated()``; ports are validated here defensively."""
    local_port = _validate_port(local_port, what="local_port")
    argv = [
        ssh_bin, "-N",
        "-o", "BatchMode=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
    ]
    if spec.ssh_port:
        argv += ["-p", str(spec.ssh_port)]
    if spec.ssh_key_path:
        argv += ["-i", spec.ssh_key_path]
    # Loopback-only forward — never expose the tunnel to the LAN.
    argv += ["-L", f"127.0.0.1:{local_port}:{spec.remote_host}:{spec.remote_port}"]
    target = f"{spec.ssh_username}@{spec.ssh_host}" if spec.ssh_username else spec.ssh_host
    argv += [target]
    return argv


class _Child:
    """Owns a real ssh subprocess + a temp stderr file (drained on demand)."""

    def __init__(self, argv: list[str]) -> None:
        self._stderr = tempfile.NamedTemporaryFile(
            prefix="errorta-ssh-", suffix=".err", delete=False)
        # Own process group so a teardown kill can't escape to the parent.
        self._proc = subprocess.Popen(  # noqa: S603 — fixed, validated argv
            argv, stdout=subprocess.DEVNULL, stderr=self._stderr,
            stdin=subprocess.DEVNULL, start_new_session=True)

    def poll(self) -> Optional[int]:
        return self._proc.poll()

    def kill(self) -> None:
        try:
            self._proc.kill()
        except ProcessLookupError:
            pass
        try:
            self._proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._stderr.close()
            os.unlink(self._stderr.name)
        except OSError:
            pass

    def stderr_tail(self) -> str:
        try:
            with open(self._stderr.name, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - _STDERR_TAIL))
                return fh.read().decode("utf-8", "replace").strip()
        except OSError:
            return ""


# A spawn callable is injectable so tests can avoid a real ssh.
SpawnFn = Callable[[list[str]], Any]


def _default_spawn(argv: list[str]) -> _Child:
    return _Child(argv)


def _alloc_local_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _port_accepts(port: int, *, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


@dataclass
class _Tunnel:
    spec: TunnelSpec
    local_port: int
    state: str = STATE_DOWN
    last_error: str = ""
    since: str = field(default_factory=_now_iso_z)
    child: Any = None
    thread: Optional[threading.Thread] = None
    stop: threading.Event = field(default_factory=threading.Event)
    backoff: float = 1.0

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.since = _now_iso_z()


class TunnelManager:
    """Owns one ssh child per :class:`TunnelSpec`. Thread-safe; consumers call
    :meth:`ensure` to get a live local port and surface :meth:`status`."""

    def __init__(self, *, spawn: SpawnFn = _default_spawn,
                 watch_interval: float = _WATCH_INTERVAL,
                 connect_budget: float = _CONNECT_BUDGET) -> None:
        self._spawn = spawn
        self._watch_interval = watch_interval
        self._connect_budget = connect_budget
        self._lock = threading.RLock()
        self._tunnels: dict[TunnelSpec, _Tunnel] = {}

    # -- public API -------------------------------------------------------- #
    def ensure(self, spec: TunnelSpec, *, wait: bool = True) -> int:
        """Bring up (or reuse) the tunnel for ``spec`` and return its stable local
        port. Returns even while connecting/reconnecting — callers get a refused
        connection and surface it honestly rather than blocking on a dead remote."""
        spec = spec.validated()
        with self._lock:
            tun = self._tunnels.get(spec)
            if tun is None:
                tun = _Tunnel(spec=spec, local_port=_alloc_local_port())
                tun._set_state(STATE_CONNECTING)
                self._tunnels[spec] = tun
                tun.thread = threading.Thread(
                    target=self._watch, args=(tun,), daemon=True,
                    name=f"ssh-tunnel-{spec.ssh_host}")
                tun.thread.start()
            local_port = tun.local_port
        if wait:
            self._wait_up(spec, budget=self._connect_budget)
        return local_port

    def reconnect(self, spec: TunnelSpec) -> bool:
        """Force an immediate reconnect (operator 'kick it'). Returns True if a
        tunnel for the spec exists."""
        spec = spec.validated()
        with self._lock:
            tun = self._tunnels.get(spec)
            if tun is None:
                return False
            self._kill_child(tun)  # watcher respawns promptly
            tun.backoff = 1.0
            tun._set_state(STATE_RECONNECTING)
        return True

    def status(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "ssh_host": t.spec.ssh_host,
                    "remote_host": t.spec.remote_host,
                    "remote_port": t.spec.remote_port,
                    "local_port": t.local_port,
                    "state": t.state,
                    "last_error": t.last_error,
                    "since": t.since,
                }
                for t in self._tunnels.values()
            ]

    def status_for(self, spec: TunnelSpec) -> Optional[dict[str, Any]]:
        spec = spec.validated()
        with self._lock:
            tun = self._tunnels.get(spec)
            if tun is None:
                return None
            return {
                "ssh_host": tun.spec.ssh_host,
                "remote_host": tun.spec.remote_host,
                "remote_port": tun.spec.remote_port,
                "local_port": tun.local_port,
                "state": tun.state,
                "last_error": tun.last_error,
                "since": tun.since,
            }

    def teardown(self) -> None:
        """Stop every watcher + kill every child. Idempotent (sidecar shutdown)."""
        with self._lock:
            tunnels = list(self._tunnels.values())
            self._tunnels.clear()
        for tun in tunnels:
            tun.stop.set()
            self._kill_child(tun)
        for tun in tunnels:
            if tun.thread is not None:
                tun.thread.join(timeout=5)

    # -- internals --------------------------------------------------------- #
    def _wait_up(self, spec: TunnelSpec, *, budget: float) -> None:
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            with self._lock:
                tun = self._tunnels.get(spec)
                if tun is None or tun.state in (STATE_UP, STATE_ERROR):
                    return
            time.sleep(0.1)

    def _kill_child(self, tun: _Tunnel) -> None:
        child = tun.child
        tun.child = None
        if child is not None:
            try:
                child.kill()
            except Exception:  # noqa: BLE001
                pass

    def _watch(self, tun: _Tunnel) -> None:
        while not tun.stop.is_set():
            # (Re)spawn if there is no live child.
            if tun.child is None:
                try:
                    tun.child = self._spawn(build_ssh_argv(tun.spec, tun.local_port))
                except Exception as exc:  # noqa: BLE001
                    with self._lock:
                        tun.last_error = f"spawn failed: {exc}"
                        tun._set_state(STATE_ERROR)
                    if tun.stop.wait(min(tun.backoff, _BACKOFF_MAX)):
                        break
                    tun.backoff = min(tun.backoff * 2, _BACKOFF_MAX)
                    continue

            rc = tun.child.poll()
            if rc is not None:
                # Child exited (dead link / ServerAlive / forward-bind failure).
                err = ""
                try:
                    err = tun.child.stderr_tail()
                except Exception:  # noqa: BLE001
                    pass
                with self._lock:
                    tun.last_error = err or f"ssh exited rc={rc}"
                    tun._set_state(STATE_RECONNECTING)
                self._kill_child(tun)
                if tun.stop.wait(min(tun.backoff, _BACKOFF_MAX)):
                    break
                tun.backoff = min(tun.backoff * 2, _BACKOFF_MAX)
                continue

            # Child alive — is the forward actually accepting yet?
            if _port_accepts(tun.local_port):
                with self._lock:
                    if tun.state != STATE_UP:
                        tun._set_state(STATE_UP)
                        tun.last_error = ""
                    tun.backoff = 1.0
            elif tun.state not in (STATE_CONNECTING, STATE_RECONNECTING):
                with self._lock:
                    tun._set_state(STATE_CONNECTING)

            if tun.stop.wait(self._watch_interval):
                break
        # exiting the loop -> ensure the child is gone
        self._kill_child(tun)


# Process-wide singleton (the sidecar's tunnel registry).
tunnel_manager = TunnelManager()
