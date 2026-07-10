"""F-INFRA-12 Phase B Slice 1 — persistent data-residency state.

Stores the active residency mode and connection details in
``errorta_home() / "data-residency.json"``. The on-disk schema is the
one documented in ``docs/specs/F-INFRA-12-configurable-data-residency.md``
sections 4 and 5.

Pattern mirrors ``errorta_shell.config``: atomic JSON write via tmp +
rename, module-level threading lock, malformed-JSON tolerance on load.

The Cloud-mode access token is **never** persisted to disk. ``save()``
always writes ``cloud_token: null``; the in-memory value stays in
process memory only. Keychain integration is deferred to v0.6.
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Literal, Optional

from errorta_app.paths import data_residency_path

ResidencyMode = Literal["local", "ssh-remote", "cloud"]

_VALID_MODES: tuple[str, ...] = ("local", "ssh-remote", "cloud")

_lock = Lock()
_live_local_tunnel_port: Optional[int] = None
_SSH_CONNECTION_FIELDS = {
    "mode",
    "ssh_host",
    "ssh_port",
    "ssh_key_path",
    "ssh_username",
    "remote_sidecar_port",
}


@dataclass(frozen=True)
class ResidencyState:
    mode: ResidencyMode = "local"
    ssh_host: Optional[str] = None
    ssh_port: int = 22
    ssh_key_path: Optional[str] = None
    ssh_username: Optional[str] = None
    remote_sidecar_port: Optional[int] = None
    local_tunnel_port: Optional[int] = None
    cloud_url: Optional[str] = None
    cloud_token: Optional[str] = None
    updated_at: Optional[str] = None


def _now_iso_z() -> str:
    # Timezone-aware UTC, rendered with a trailing Z (drop +00:00).
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _from_dict(data: dict[str, Any]) -> ResidencyState:
    """Build a ResidencyState from a parsed JSON dict, defensively."""
    mode = data.get("mode", "local")
    if mode not in _VALID_MODES:
        return ResidencyState()
    try:
        return ResidencyState(
            mode=mode,
            ssh_host=data.get("ssh_host"),
            ssh_port=int(data.get("ssh_port", 22)),
            ssh_key_path=data.get("ssh_key_path"),
            ssh_username=data.get("ssh_username"),
            remote_sidecar_port=(
                int(data["remote_sidecar_port"])
                if data.get("remote_sidecar_port") is not None
                else None
            ),
            # Runtime-only: never trust an on-disk tunnel port. SSH tunnel
            # ports are ephemeral and can be reused by unrelated local
            # services after app restart.
            local_tunnel_port=None,
            cloud_url=data.get("cloud_url"),
            # cloud_token is intentionally NOT read from disk (redacted on save).
            cloud_token=None,
            updated_at=data.get("updated_at"),
        )
    except (TypeError, ValueError):
        return ResidencyState()


def _load() -> ResidencyState:
    p = data_residency_path()
    if not p.exists():
        return ResidencyState()
    try:
        raw = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return ResidencyState()
    if not isinstance(raw, dict):
        return ResidencyState()
    state = _from_dict(raw)
    if state.mode == "ssh-remote" and _live_local_tunnel_port is not None:
        return replace(state, local_tunnel_port=_live_local_tunnel_port)
    return state


def _save(state: ResidencyState) -> None:
    p = data_residency_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = dataclasses.asdict(state)
    # cloud_token never lands on disk; held in process memory only.
    payload["cloud_token"] = None
    # local_tunnel_port never lands on disk; held in process memory only.
    payload["local_tunnel_port"] = None
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(p)


def _validate(state: ResidencyState) -> None:
    if state.mode not in _VALID_MODES:
        raise ValueError(
            f"mode must be one of {_VALID_MODES!r}, got {state.mode!r}"
        )
    if not isinstance(state.ssh_port, int) or not (1 <= state.ssh_port <= 65535):
        raise ValueError(
            f"ssh_port must be an int in 1..65535, got {state.ssh_port!r}"
        )
    if state.remote_sidecar_port is not None and not (
        isinstance(state.remote_sidecar_port, int) and 1 <= state.remote_sidecar_port <= 65535
    ):
        raise ValueError(
            "remote_sidecar_port must be an int in 1..65535, "
            f"got {state.remote_sidecar_port!r}"
        )
    if state.local_tunnel_port is not None and not (
        isinstance(state.local_tunnel_port, int) and 1 <= state.local_tunnel_port <= 65535
    ):
        raise ValueError(
            "local_tunnel_port must be an int in 1..65535, "
            f"got {state.local_tunnel_port!r}"
        )
    if state.mode == "ssh-remote":
        if not isinstance(state.ssh_host, str) or not state.ssh_host.strip():
            raise ValueError("ssh_host must be a non-empty string when mode='ssh-remote'")
    if state.mode == "cloud":
        if not isinstance(state.cloud_url, str) or not state.cloud_url.strip():
            raise ValueError("cloud_url must be a non-empty string when mode='cloud'")
        if not state.cloud_url.startswith("https://"):
            raise ValueError("cloud_url must start with https://")


def load() -> ResidencyState:
    """Load the persisted residency state.

    Returns ``ResidencyState(mode="local")`` if the file is missing,
    unreadable, or malformed (matches the shell.config tolerance).
    """
    with _lock:
        return _load()


def save(state: ResidencyState) -> None:
    """Persist ``state`` atomically. ``cloud_token`` is redacted on disk."""
    with _lock:
        _save(state)


def update(**fields: Any) -> ResidencyState:
    """Load → replace named fields → validate → save → return new state.

    ``updated_at`` is stamped automatically on every successful save.
    """
    global _live_local_tunnel_port

    with _lock:
        current = _load()
        # Strip the auto-managed timestamp from caller overrides so a
        # stale `updated_at` can't sneak through.
        fields.pop("updated_at", None)
        try:
            candidate = replace(current, **fields)
        except TypeError as exc:
            raise ValueError(str(exc)) from exc
        _validate(candidate)
        ssh_connection_changed = bool(_SSH_CONNECTION_FIELDS.intersection(fields))
        if candidate.mode != "ssh-remote":
            _live_local_tunnel_port = None
            candidate = replace(candidate, local_tunnel_port=None)
        elif "local_tunnel_port" in fields:
            _live_local_tunnel_port = fields["local_tunnel_port"]
        elif ssh_connection_changed:
            _live_local_tunnel_port = None
            candidate = replace(candidate, local_tunnel_port=None)
        elif _live_local_tunnel_port is not None:
            candidate = replace(candidate, local_tunnel_port=_live_local_tunnel_port)
        stamped = replace(candidate, updated_at=_now_iso_z())
        _save(stamped)
        # Re-attach cloud_token from the caller's payload (or carry the
        # current in-memory value forward) so callers see the live token
        # even though it never lands on disk.
        if "cloud_token" in fields:
            stamped = replace(stamped, cloud_token=fields["cloud_token"])
        else:
            stamped = replace(stamped, cloud_token=current.cloud_token)
        return stamped
