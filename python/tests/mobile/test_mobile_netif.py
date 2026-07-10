from __future__ import annotations

from errorta_mobile import netif


class _FakeSocket:
    def __init__(self, address: str | None) -> None:
        self.address = address

    def connect(self, _target) -> None:
        if self.address is None:
            raise OSError("offline")

    def getsockname(self):
        return (self.address, 49152)

    def close(self) -> None:
        pass


def _no_extra_interfaces(monkeypatch) -> None:
    # Isolate from the host's real Tailscale/interfaces for deterministic tests.
    monkeypatch.setattr(netif, "_tailscale_via_cli", lambda: None)
    monkeypatch.setattr(netif, "_interface_ipv4s", lambda: [])


def test_lan_ipv4_candidates_flags_default_and_filters_loopback(monkeypatch) -> None:
    monkeypatch.setattr(
        netif.socket,
        "socket",
        lambda *_args, **_kwargs: _FakeSocket("198.51.100.40"),
    )
    monkeypatch.setattr(
        netif.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (netif.socket.AF_INET, None, None, None, ("127.0.0.1", 0)),
            (netif.socket.AF_INET, None, None, None, ("198.51.100.41", 0)),
            (netif.socket.AF_INET, None, None, None, ("198.51.100.40", 0)),
        ],
    )
    _no_extra_interfaces(monkeypatch)

    assert netif.lan_ipv4_candidates() == [
        {"address": "198.51.100.40", "interface": "default", "kind": "lan", "is_default": True},
        {"address": "198.51.100.41", "interface": "hostname", "kind": "lan", "is_default": False},
    ]


def test_lan_ipv4_candidates_is_empty_safe_when_offline(monkeypatch) -> None:
    monkeypatch.setattr(
        netif.socket,
        "socket",
        lambda *_args, **_kwargs: _FakeSocket(None),
    )
    monkeypatch.setattr(
        netif.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    _no_extra_interfaces(monkeypatch)

    assert netif.lan_ipv4_candidates() == []


def test_tailscale_surfaces_from_cli_and_interface_scan(monkeypatch) -> None:
    # CLI is authoritative.
    monkeypatch.setattr(netif, "_tailscale_via_cli", lambda: "100.101.102.103")
    assert netif.tailscale_ipv4() == "100.101.102.103"

    # No CLI → fall back to the all-interface scan for the CGNAT address.
    monkeypatch.setattr(netif, "_tailscale_via_cli", lambda: None)
    monkeypatch.setattr(
        netif, "_interface_ipv4s",
        lambda: [("192.0.2.14", "en0"), ("100.64.5.6", "utun4")],
    )
    assert netif.tailscale_ipv4() == "100.64.5.6"


def test_lan_candidates_include_tailscale_when_present(monkeypatch) -> None:
    monkeypatch.setattr(netif, "_default_ipv4", lambda: "192.0.2.14")
    monkeypatch.setattr(netif, "_hostname_ipv4s", lambda: [])
    monkeypatch.setattr(netif, "_tailscale_via_cli", lambda: "100.64.5.6")
    monkeypatch.setattr(netif, "_interface_ipv4s", lambda: [])
    cands = netif.lan_ipv4_candidates()
    kinds = {c["address"]: c["kind"] for c in cands}
    assert kinds.get("192.0.2.14") == "lan"
    assert kinds.get("100.64.5.6") == "tailscale"
