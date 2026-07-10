"""F065 B3b — reconcile the LAN listener to the mobile-connector config.

A process-singleton listener. ``sync()`` is the single entry point: it stops any
running listener and (re)starts one bound to the configured LAN IP over a
freshly-ensured TLS cert, but ONLY when the connector is enabled. Called from
the sidecar lifespan on boot and from the settings PUT when the operator
enables/disables/repoints the connector.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from errorta_mobile import config as mobile_config

from .mobile_server import LanListener, start_lan_listener

_LOG = logging.getLogger("errorta_app.mobile_lifecycle")
# F071 — one LanListener per bound address (LAN + optionally Tailscale). Each
# binds a SPECIFIC IP (never 0.0.0.0); a single multi-SAN cert covers all so the
# pinned fingerprint stays stable across both networks.
_listeners: list[LanListener] = []
_lock = threading.Lock()


def _kind(host: str) -> str:
    import ipaddress

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "lan"
    if ip in ipaddress.ip_network("100.64.0.0/10"):
        return "tailscale"
    if ip.is_loopback:
        return "loopback"
    return "lan"


def _primary_host(cfg: dict[str, Any]) -> str | None:
    mode = str(cfg.get("bind_mode") or "disabled")
    if mode == "loopback_dev":
        return "127.0.0.1"
    if mode == "lan":
        return cfg.get("lan_bind_address")
    if mode == "explicit_host":
        return cfg.get("explicit_host")
    if mode == "tailscale":
        return cfg.get("tailscale_bind_address") or cfg.get("lan_bind_address")
    return None


def _bind_hosts(cfg: dict[str, Any]) -> list[str]:
    """Ordered, de-duplicated specific addresses to bind (LAN + maybe Tailscale)."""
    hosts: list[str] = []
    primary = _primary_host(cfg)
    if primary:
        hosts.append(str(primary))
    ts = cfg.get("tailscale_bind_address")
    if cfg.get("also_tailscale") and ts and str(ts) not in hosts:
        hosts.append(str(ts))
    return hosts


def _stop_locked() -> None:
    global _listeners
    for listener in _listeners:
        try:
            listener.stop()
        except Exception as exc:  # pragma: no cover - defensive
            _LOG.warning("error stopping mobile listener on %s: %s", listener.host, exc)
    _listeners = []


def sync(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """(Re)start or stop the listener(s) to match config. Returns a status."""
    cfg = config or mobile_config.load()
    with _lock:
        _stop_locked()
        if not cfg.get("enabled") or cfg.get("bind_mode") == "disabled":
            return {"running": False, "reason": "disabled", "listeners": []}
        hosts = _bind_hosts(cfg)
        if not hosts:
            return {"running": False, "reason": "no_bind_host", "listeners": []}
        from errorta_mobile import tls as mobile_tls

        # One cert covering ALL bind addresses → stable pinned fingerprint.
        try:
            cert, key = mobile_tls.ensure_self_signed(
                hosts, mobile_config.mobile_dir() / "tls"
            )
        except Exception as exc:
            _LOG.warning("mobile TLS cert generation failed for %s: %s", hosts, exc)
            return {"running": False, "reason": "cert_failed", "error": str(exc), "listeners": []}

        started: list[dict[str, Any]] = []
        for host in hosts:
            try:
                listener = start_lan_listener(
                    host=host, port=int(cfg["port"]),
                    certfile=cert, keyfile=key,
                    limit_concurrency=64,
                )
            except Exception as exc:
                # A failed bind on one address (e.g. Tailscale down) must not take
                # down the others — log and continue with what bound.
                _LOG.warning("mobile listener failed to start on %s: %s", host, exc)
                continue
            _listeners.append(listener)
            started.append({"host": host, "port": int(cfg["port"]), "kind": _kind(host)})
        if not _listeners:
            return {"running": False, "reason": "start_failed", "listeners": []}
        return {
            "running": True,
            # Back-compat: primary host/port mirror the first listener.
            "host": started[0]["host"],
            "port": started[0]["port"],
            "cert_sha256": mobile_tls.cert_der_sha256(cert),
            "listeners": started,
        }


def stop() -> None:
    with _lock:
        _stop_locked()


def status() -> dict[str, Any]:
    with _lock:
        alive = [item for item in _listeners if item.is_alive()]
        return {
            "running": bool(alive),
            "host": alive[0].host if alive else None,
            "port": alive[0].port if alive else None,
            "listeners": [
                {"host": item.host, "port": item.port, "kind": _kind(item.host)}
                for item in alive
            ],
        }


__all__ = ["status", "stop", "sync"]
