"""Persistent remote-AIAR endpoint settings for F088.

This stores the Mac-side configuration for the topology where Errorta runs
locally but the project corpus lives in a remote AIAR instance reached through
an SSH tunnel. The bearer token is secret: raw reads are for adapter dispatch
only, masked reads are for HTTP/UI surfaces, and the file is written 0600.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from errorta_app.paths import errorta_home


@dataclass(frozen=True)
class RemoteAiarSettings:
    base_url: str = ""
    tunnel_port: int | None = None
    token: str | None = None
    timeout_s: float = 60.0
    verify: bool = True
    updated_at: str | None = None
    # F089 managed-tunnel mode: when ssh_host is set, Errorta owns an
    # ``ssh -N -L`` tunnel to ``remote_host:remote_port`` on that host alias and
    # derives base_url from the live local forward port. When unset, the bare
    # base_url is used as-is (bring-your-own-tunnel — today's behavior).
    ssh_host: str | None = None
    remote_host: str = "127.0.0.1"
    remote_port: int | None = None
    ssh_port: int | None = None
    ssh_username: str | None = None
    ssh_key_path: str | None = None
    auto_start: bool = True

    @property
    def configured(self) -> bool:
        return bool(self.base_url.strip()) or self.managed

    @property
    def managed(self) -> bool:
        return bool((self.ssh_host or "").strip()) and bool(self.remote_port)


def path() -> Path:
    return errorta_home() / "remote-aiar.json"


def _now_iso_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _mask_secret(raw: str | None) -> str | None:
    if not raw:
        return None
    if len(raw) <= 4:
        return "…"
    return "…" + raw[-4:]


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _normalize_base_url(base_url: str, tunnel_port: int | None) -> str:
    url = base_url.strip()
    if not url and tunnel_port is not None:
        url = f"http://127.0.0.1:{tunnel_port}"
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url must be an http(s) URL")
    # Footgun guard: the user typed a loopback URL with NO port (e.g.
    # "http://127.0.0.1") AND set a tunnel port. Merge them instead of silently
    # dropping the port and hitting :80 — this is the SSH-tunnel case the tunnel
    # port exists for. Only loopback hosts are touched, so a real remote URL is
    # never rewritten.
    if (parsed.port is None and tunnel_port is not None
            and (parsed.hostname or "") in _LOOPBACK_HOSTS):
        url = parsed._replace(netloc=f"{parsed.hostname}:{tunnel_port}").geturl()
    return url.rstrip("/")


def _validate_timeout(value: float) -> float:
    timeout = float(value)
    if timeout <= 0 or timeout > 600:
        raise ValueError("timeout_s must be in 0..600 seconds")
    return timeout


def _validate_port(value: int | None) -> int | None:
    if value is None:
        return None
    port = int(value)
    if port < 1 or port > 65535:
        raise ValueError("tunnel_port must be in 1..65535")
    return port


def _clean_str(value: Any) -> str | None:
    return (str(value).strip() or None) if value is not None else None


def _from_dict(raw: dict[str, Any]) -> RemoteAiarSettings:
    try:
        tunnel_port = _validate_port(raw.get("tunnel_port"))
        return RemoteAiarSettings(
            base_url=_normalize_base_url(str(raw.get("base_url") or ""), tunnel_port),
            tunnel_port=tunnel_port,
            token=(str(raw.get("token") or "").strip() or None),
            timeout_s=_validate_timeout(float(raw.get("timeout_s", 60.0))),
            verify=bool(raw.get("verify", True)),
            updated_at=raw.get("updated_at") if isinstance(raw.get("updated_at"), str) else None,
            ssh_host=_clean_str(raw.get("ssh_host")),
            remote_host=(_clean_str(raw.get("remote_host")) or "127.0.0.1"),
            remote_port=_validate_port(raw.get("remote_port")),
            ssh_port=_validate_port(raw.get("ssh_port")),
            ssh_username=_clean_str(raw.get("ssh_username")),
            ssh_key_path=_clean_str(raw.get("ssh_key_path")),
            auto_start=bool(raw.get("auto_start", True)),
        )
    except (TypeError, ValueError):
        return RemoteAiarSettings()


def load_raw() -> RemoteAiarSettings:
    p = path()
    if not p.exists():
        return RemoteAiarSettings()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return RemoteAiarSettings()
    if not isinstance(raw, dict):
        return RemoteAiarSettings()
    return _from_dict(raw)


def _write(settings: RemoteAiarSettings) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(settings)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".remote-aiar-", suffix=".tmp", dir=str(p.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        if os.name == "posix":
            os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp_path, p)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


_SSH_TOKEN_RE = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _validate_ssh_token(value: str | None, *, what: str) -> str | None:
    if value is None:
        return None
    v = str(value).strip()
    if not v:
        return None
    if not _SSH_TOKEN_RE.match(v):
        raise ValueError(f"invalid {what}: {value!r}")
    return v


def save(
    *,
    base_url: str = "",
    tunnel_port: int | None = None,
    token: str | None = None,
    timeout_s: float = 60.0,
    verify: bool = True,
    ssh_host: str | None = None,
    remote_host: str = "127.0.0.1",
    remote_port: int | None = None,
    ssh_port: int | None = None,
    ssh_username: str | None = None,
    ssh_key_path: str | None = None,
    auto_start: bool = True,
) -> RemoteAiarSettings:
    port = _validate_port(tunnel_port)
    ssh_host = _validate_ssh_token(ssh_host, what="ssh_host")
    settings = RemoteAiarSettings(
        base_url=_normalize_base_url(base_url, port),
        tunnel_port=port,
        token=(token or "").strip() or None,
        timeout_s=_validate_timeout(timeout_s),
        verify=bool(verify),
        updated_at=_now_iso_z(),
        ssh_host=ssh_host,
        remote_host=(_validate_ssh_token(remote_host, what="remote_host") or "127.0.0.1"),
        remote_port=_validate_port(remote_port),
        ssh_port=_validate_port(ssh_port),
        ssh_username=_validate_ssh_token(ssh_username, what="ssh_username"),
        ssh_key_path=(str(ssh_key_path).strip() or None) if ssh_key_path else None,
        auto_start=bool(auto_start),
    )
    # Managed mode needs a host + remote port; BYO mode needs a base_url.
    if ssh_host:
        if not settings.remote_port:
            raise ValueError("managed mode (ssh_host) requires remote_port")
    elif not settings.base_url:
        raise ValueError("base_url or tunnel_port is required")
    _write(settings)
    return settings


def update(
    *,
    base_url: str | None = None,
    tunnel_port: int | None = None,
    token: str | None = None,
    timeout_s: float | None = None,
    verify: bool | None = None,
    clear_token: bool = False,
    ssh_host: str | None = None,
    remote_host: str | None = None,
    remote_port: int | None = None,
    ssh_port: int | None = None,
    ssh_username: str | None = None,
    ssh_key_path: str | None = None,
    auto_start: bool | None = None,
) -> RemoteAiarSettings:
    current = load_raw()
    next_token = None if clear_token else (token if token is not None else current.token)
    return save(
        base_url=current.base_url if base_url is None else base_url,
        tunnel_port=current.tunnel_port if tunnel_port is None else tunnel_port,
        token=next_token,
        timeout_s=current.timeout_s if timeout_s is None else timeout_s,
        verify=current.verify if verify is None else verify,
        ssh_host=current.ssh_host if ssh_host is None else ssh_host,
        remote_host=current.remote_host if remote_host is None else remote_host,
        remote_port=current.remote_port if remote_port is None else remote_port,
        ssh_port=current.ssh_port if ssh_port is None else ssh_port,
        ssh_username=current.ssh_username if ssh_username is None else ssh_username,
        ssh_key_path=current.ssh_key_path if ssh_key_path is None else ssh_key_path,
        auto_start=current.auto_start if auto_start is None else auto_start,
    )


def clear() -> RemoteAiarSettings:
    p = path()
    try:
        p.unlink()
    except FileNotFoundError:
        pass
    return RemoteAiarSettings()


def tunnel_spec(settings: RemoteAiarSettings | None = None) -> Any | None:
    """Build a TunnelSpec for managed mode, or None for BYO mode. Lazy import so
    errorta_project_grounding has no hard dependency on errorta_tunnels."""
    state = settings or load_raw()
    if not state.managed:
        return None
    from errorta_tunnels import TunnelSpec
    return TunnelSpec(
        ssh_host=str(state.ssh_host),
        remote_port=int(state.remote_port or 0),
        remote_host=state.remote_host or "127.0.0.1",
        ssh_port=state.ssh_port,
        ssh_username=state.ssh_username,
        ssh_key_path=state.ssh_key_path,
    )


def masked(settings: RemoteAiarSettings | None = None) -> dict[str, Any]:
    state = settings or load_raw()
    out: dict[str, Any] = {
        "configured": state.configured,
        "managed": state.managed,
        "base_url": state.base_url,
        "tunnel_port": state.tunnel_port,
        "timeout_s": state.timeout_s,
        "verify": state.verify,
        "token_configured": bool(state.token),
        "token_preview": _mask_secret(state.token),
        "updated_at": state.updated_at,
        "ssh_host": state.ssh_host,
        "remote_host": state.remote_host,
        "remote_port": state.remote_port,
        "ssh_port": state.ssh_port,
        "ssh_username": state.ssh_username,
        "ssh_key_path": state.ssh_key_path,
        "auto_start": state.auto_start,
    }
    # Live tunnel state, when a managed tunnel exists.
    if state.managed:
        try:
            from errorta_tunnels import tunnel_manager
            out["tunnel"] = tunnel_manager.status_for(tunnel_spec(state))
        except Exception:  # noqa: BLE001
            out["tunnel"] = None
    return out


def effective_for_adapter() -> RemoteAiarSettings | None:
    state = load_raw()
    return state if state.configured else None


__all__ = [
    "RemoteAiarSettings",
    "clear",
    "effective_for_adapter",
    "load_raw",
    "masked",
    "path",
    "save",
    "update",
]
