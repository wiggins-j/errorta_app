"""Best-effort LAN IPv4 discovery for the mobile connector UI."""
from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import subprocess
from typing import Any


_TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_BINARIES = (
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
)


def _is_pairable_ipv4(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    if ip.version != 4:
        return False
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return False
    # RFC1918 private LANs plus Tailscale's shared-CGNAT range.
    return ip.is_private or ip in _TAILSCALE_CGNAT


def _kind_for(value: str) -> str:
    """F071 — classify a pairable address. The Tailscale 100.64.0.0/10 CGNAT
    range is the off-LAN overlay; everything else pairable is a regular LAN."""
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return "lan"
    return "tailscale" if ip in _TAILSCALE_CGNAT else "lan"


def _tailscale_via_cli() -> str | None:
    """Authoritative Tailscale IPv4 via `tailscale ip -4`, if the CLI is present."""
    seen: list[str] = []
    which = shutil.which("tailscale")
    for binpath in ([which] if which else []) + list(_TAILSCALE_BINARIES):
        if not binpath or binpath in seen or not os.path.exists(binpath):
            continue
        seen.append(binpath)
        try:
            out = subprocess.run(
                [binpath, "ip", "-4"],
                capture_output=True, text=True, timeout=3, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        for line in out.stdout.splitlines():
            line = line.strip()
            if _is_pairable_ipv4(line) and _kind_for(line) == "tailscale":
                return line
    return None


def _interface_ipv4s() -> list[tuple[str, str]]:
    """(address, interface_name) for every pairable IPv4 on any interface — the
    only way to see the Tailscale `utun` address, which isn't the default route
    or a hostname-resolved IP. Uses psutil (already bundled via hwdetect)."""
    try:
        import psutil  # lazy — already a sidecar dep
    except ImportError:
        return []
    try:
        addrs = psutil.net_if_addrs()
    except Exception:  # pragma: no cover - defensive
        return []
    out: list[tuple[str, str]] = []
    for iface, snics in addrs.items():
        for snic in snics:
            if getattr(snic, "family", None) == socket.AF_INET:
                addr = str(snic.address)
                if _is_pairable_ipv4(addr):
                    out.append((addr, str(iface)))
    return out


def tailscale_ipv4() -> str | None:
    """Best-effort detected Tailscale IPv4 (100.64.0.0/10), or None. CLI first
    (authoritative), then an all-interface scan for the CGNAT address."""
    cli = _tailscale_via_cli()
    if cli:
        return cli
    for addr, _iface in _interface_ipv4s():
        if _kind_for(addr) == "tailscale":
            return addr
    return None


def _default_ipv4() -> str | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        value = str(sock.getsockname()[0])
        return value if _is_pairable_ipv4(value) else None
    except OSError:
        return None
    finally:
        sock.close()


def _hostname_ipv4s() -> list[str]:
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    out: list[str] = []
    for info in infos:
        try:
            address = str(info[4][0])
        except (IndexError, TypeError):
            continue
        if _is_pairable_ipv4(address) and address not in out:
            out.append(address)
    return out


def lan_ipv4_candidates() -> list[dict[str, Any]]:
    """Return private IPv4 candidates for the settings picker.

    The stdlib cannot reliably name interfaces cross-platform without extra
    dependencies, so we label the default-route address as ``default`` and
    hostname-derived addresses as ``hostname``. This route is advisory only; the
    UI still offers a free-text fallback.
    """
    default = _default_ipv4()
    ordered: list[tuple[str, str]] = []

    def _add(address: str, label: str) -> None:
        if all(existing != address for existing, _label in ordered):
            ordered.append((address, label))

    if default is not None:
        _add(default, "default")
    for address in _hostname_ipv4s():
        _add(address, "hostname")
    # All-interface scan surfaces the Tailscale `utun` address (and anything else
    # not on the default route) so the picker can offer it. The Tailscale CLI is
    # authoritative for its own address; prefer it.
    ts = _tailscale_via_cli()
    if ts is not None:
        _add(ts, "tailscale")
    for address, iface in _interface_ipv4s():
        _add(address, iface)
    return [
        {
            "address": address,
            "interface": label,
            "kind": _kind_for(address),
            "is_default": default is not None and address == default,
        }
        for address, label in ordered
    ]


__all__ = ["lan_ipv4_candidates", "tailscale_ipv4"]
