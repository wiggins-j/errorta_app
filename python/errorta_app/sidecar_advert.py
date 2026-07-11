"""F147 S9a — boot-time sidecar advertisement (``${ERRORTA_HOME}/sidecar.json``).

A running sidecar picks its bind port at runtime (Tauri passes an ephemeral one
via ``ERRORTA_SIDECAR_PORT``), so an out-of-process front-end — the headless CLI,
app-doctor, a second terminal — has no way to *discover* a live sidecar. This
module writes a small discovery file at boot so any front-end can read
``{port, pid, commit, started_by, started_at}`` and decide whether to adopt the
running sidecar instead of spawning a competing one (the single-instance
contract, F147 §13.1 / §4.2).

The file is written 0600 (it reveals a loopback port + our pid) and removed on a
graceful shutdown IFF our pid still owns it — a crash leaves a stale file, which
a reader validates against a live ``GET /healthz`` before trusting.

This is purely additive: nothing consumes the file yet inside the app (the app
still always spawns its own sidecar in S9a); the adopt/co-drive wiring is a later
slice. Writing it now makes a CLI-spawned sidecar discoverable.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from pathlib import Path
from typing import Optional

_LOG = logging.getLogger("errorta_app.sidecar_advert")

_FILENAME = "sidecar.json"


def sidecar_json_path() -> Path:
    from errorta_app.paths import errorta_home

    return errorta_home() / _FILENAME


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def started_by() -> str:
    """Who spawned this sidecar — ``app`` / ``cli`` / ``unknown``. Front-ends set
    ``ERRORTA_STARTED_BY`` at spawn; absent it, we can't know, so ``unknown``."""
    return (os.environ.get("ERRORTA_STARTED_BY") or "unknown").strip() or "unknown"


def write_advertisement(
    *,
    port: int,
    pid: Optional[int] = None,
    commit: Optional[str] = None,
    started_by_value: Optional[str] = None,
) -> Optional[Path]:
    """Atomically write the discovery file (mode 0600). Best-effort — never
    raises (a discovery-file failure must not block the sidecar from serving).
    Returns the path on success, else ``None``."""
    payload = {
        "port": int(port),
        "pid": int(pid if pid is not None else os.getpid()),
        "commit": commit,
        "started_by": started_by_value if started_by_value is not None else started_by(),
        "started_at": _now_iso(),
    }
    try:
        path = sidecar_json_path()
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        # O_CREAT with 0600 so the file is never briefly world-readable.
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:  # pragma: no cover - defensive
            pass
        return path
    except Exception as exc:  # noqa: BLE001 - advertisement is best-effort
        _LOG.warning("could not write sidecar advertisement: %s", exc)
        try:
            if "tmp" in dir() and Path(tmp).exists():  # type: ignore[possibly-undefined]
                os.unlink(tmp)  # type: ignore[possibly-undefined]
        except Exception:  # pragma: no cover - defensive
            pass
        return None


def read_advertisement() -> Optional[dict]:
    """Read the discovery file, or ``None`` if absent/unreadable/corrupt."""
    try:
        path = sidecar_json_path()
        if not path.is_file():
            return None
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def remove_advertisement(*, only_if_pid: Optional[int] = None) -> bool:
    """Remove the discovery file, but only if it still describes OUR pid (so a
    successor sidecar that already overwrote it isn't clobbered on our teardown).
    ``only_if_pid`` defaults to the current process. Returns True if removed."""
    want = int(only_if_pid if only_if_pid is not None else os.getpid())
    try:
        current = read_advertisement()
        if not current:
            return False
        if int(current.get("pid") or -1) != want:
            return False
        sidecar_json_path().unlink()
        return True
    except FileNotFoundError:
        return False
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("could not remove sidecar advertisement: %s", exc)
        return False


__all__ = [
    "read_advertisement",
    "remove_advertisement",
    "sidecar_json_path",
    "started_by",
    "write_advertisement",
]
