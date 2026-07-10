"""Persistent configuration for the desktop mobile connector."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

from errorta_app.paths import errorta_home

BindMode = Literal["disabled", "loopback_dev", "lan", "tailscale", "explicit_host"]
NetworkKind = Literal["lan", "tailscale"]

ALLOWED_BIND_MODES = frozenset({
    "disabled", "loopback_dev", "lan", "tailscale", "explicit_host",
})
ALLOWED_NETWORKS = frozenset({"lan", "tailscale"})

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "bind_mode": "disabled",
    "explicit_host": None,
    # F065: the SPECIFIC LAN IPv4 the listener binds (never 0.0.0.0). The
    # operator picks it; the TLS cert SAN matches it.
    "lan_bind_address": None,
    "port": 8788,
    "require_tls": True,
    "pairing_enabled": False,
    "pairing_pin_required": False,
    "allowed_networks": ["lan", "tailscale"],
    "max_event_streams": 4,
    # F071: also bind/advertise the Tailscale IP so the phone can reach the
    # desktop off-LAN. Opt-in; the address is a SPECIFIC 100.64/10 IP (never
    # 0.0.0.0). Off by default — Tailscale binding widens the reach surface.
    "also_tailscale": False,
    "tailscale_bind_address": None,
}


def mobile_dir() -> Path:
    p = errorta_home() / "mobile"
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_path() -> Path:
    return mobile_dir() / "mobile-connector.json"


def devices_path() -> Path:
    return mobile_dir() / "devices.json"


def _bool(value: Any) -> bool:
    return bool(value)


def _int(value: Any, *, default: int, min_value: int, max_value: int) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        out = default
    if out < min_value or out > max_value:
        raise ValueError(f"integer_out_of_range:{min_value}:{max_value}")
    return out


def _normalize_bind_mode(value: Any) -> BindMode:
    mode = str(value or "disabled").strip()
    if mode not in ALLOWED_BIND_MODES:
        raise ValueError("mobile_bind_mode_unknown")
    return mode  # type: ignore[return-value]


def _normalize_networks(value: Any) -> list[NetworkKind]:
    raw = value if isinstance(value, list) else DEFAULT_CONFIG["allowed_networks"]
    out: list[NetworkKind] = []
    for item in raw:
        name = str(item).strip()
        if name not in ALLOWED_NETWORKS:
            raise ValueError("mobile_allowed_network_unknown")
        if name not in out:
            out.append(name)  # type: ignore[arg-type]
    return out or list(DEFAULT_CONFIG["allowed_networks"])


def normalize(raw: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(DEFAULT_CONFIG)
    if raw:
        merged.update(raw)

    bind_mode = _normalize_bind_mode(merged.get("bind_mode"))
    enabled = _bool(merged.get("enabled"))
    explicit_host_raw = merged.get("explicit_host")
    explicit_host = (
        str(explicit_host_raw).strip() if explicit_host_raw is not None else None
    ) or None
    if enabled and bind_mode == "disabled":
        raise ValueError("mobile_enabled_requires_bind_mode")
    if bind_mode == "explicit_host" and enabled and not explicit_host:
        raise ValueError("mobile_explicit_host_required")

    lan_bind_raw = merged.get("lan_bind_address")
    lan_bind_address = (
        str(lan_bind_raw).strip() if lan_bind_raw is not None else None
    ) or None
    if lan_bind_address is not None:
        import ipaddress
        try:
            ipaddress.ip_address(lan_bind_address)
        except ValueError as exc:
            raise ValueError("mobile_lan_bind_address_invalid") from exc
        if lan_bind_address in ("0.0.0.0", "::"):
            raise ValueError("mobile_lan_bind_must_be_specific")
    if enabled and bind_mode == "lan" and not lan_bind_address:
        raise ValueError("mobile_lan_bind_address_required")

    pairing_pin_required = bind_mode not in {"disabled", "loopback_dev"}

    # F071 — optional Tailscale bind. A SPECIFIC 100.64/10 IPv4, never 0.0.0.0.
    also_tailscale = _bool(merged.get("also_tailscale"))
    ts_bind_raw = merged.get("tailscale_bind_address")
    tailscale_bind_address = (
        str(ts_bind_raw).strip() if ts_bind_raw is not None else None
    ) or None
    if tailscale_bind_address is not None:
        import ipaddress as _ip
        try:
            addr = _ip.ip_address(tailscale_bind_address)
        except ValueError as exc:
            raise ValueError("mobile_tailscale_bind_address_invalid") from exc
        if tailscale_bind_address in ("0.0.0.0", "::"):
            raise ValueError("mobile_tailscale_bind_must_be_specific")
        if addr not in _ip.ip_network("100.64.0.0/10"):
            raise ValueError("mobile_tailscale_bind_not_cgnat")
    if also_tailscale and not tailscale_bind_address:
        raise ValueError("mobile_tailscale_bind_address_required")

    return {
        "enabled": enabled,
        "bind_mode": bind_mode,
        "explicit_host": explicit_host,
        "lan_bind_address": lan_bind_address,
        "also_tailscale": also_tailscale,
        "tailscale_bind_address": tailscale_bind_address,
        "port": _int(
            merged.get("port"),
            default=int(DEFAULT_CONFIG["port"]),
            min_value=1,
            max_value=65535,
        ),
        "require_tls": _bool(merged.get("require_tls")),
        "pairing_enabled": _bool(merged.get("pairing_enabled")),
        "pairing_pin_required": pairing_pin_required,
        "allowed_networks": _normalize_networks(merged.get("allowed_networks")),
        "max_event_streams": _int(
            merged.get("max_event_streams"),
            default=int(DEFAULT_CONFIG["max_event_streams"]),
            min_value=1,
            max_value=32,
        ),
    }


def load() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return save(dict(DEFAULT_CONFIG))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_CONFIG)
    if not isinstance(raw, dict):
        return dict(DEFAULT_CONFIG)
    try:
        return normalize(raw)
    except ValueError:
        return dict(DEFAULT_CONFIG)


def save(config: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize(config)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".mobile-connector-",
        suffix=".json",
        dir=str(path.parent),
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(normalized, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return normalized


def device_count() -> int:
    path = devices_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return 0
    if isinstance(raw, list):
        return len(raw)
    if isinstance(raw, dict) and isinstance(raw.get("devices"), list):
        return len(raw["devices"])
    return 0


def public_status(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = normalize(config or load())
    return {
        "enabled": cfg["enabled"],
        "bind_mode": cfg["bind_mode"],
        "explicit_host_set": bool(cfg.get("explicit_host")),
        "port": cfg["port"],
        "require_tls": cfg["require_tls"],
        "pairing_enabled": cfg["pairing_enabled"],
        "pairing_pin_required": cfg["pairing_pin_required"],
        "allowed_networks": list(cfg["allowed_networks"]),
        "max_event_streams": cfg["max_event_streams"],
        "device_count": device_count(),
    }


def desktop_settings(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = normalize(config or load())
    out = dict(cfg)
    out["device_count"] = device_count()
    return out


__all__ = [
    "ALLOWED_BIND_MODES",
    "ALLOWED_NETWORKS",
    "BindMode",
    "NetworkKind",
    "config_path",
    "desktop_settings",
    "device_count",
    "devices_path",
    "load",
    "mobile_dir",
    "normalize",
    "public_status",
    "save",
]
