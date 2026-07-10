"""F089 — managed SSH tunnels for remote services (sidecar-owned).

Errorta runs locally but a service (a remote AIAR instance, a watchdog model
backend) lives on another host reached over SSH. This package owns an
``ssh -N -L`` child per ``TunnelSpec``, brings it up, health-watches it, and
auto-reconnects — so a consumer points at a host alias (``example-host``) instead of
hand-running ``ssh -fN -L`` and hoping the tunnel is up.

The manager shells out to the system ``ssh``; it lives in the app/infra layer and
is never imported by ``errorta_council`` (invariant 3 — gateway is the only
council egress). See ``docs/specs/F089-managed-ssh-tunnels.md``.
"""
from __future__ import annotations

from .manager import (
    STATE_CONNECTING,
    STATE_DOWN,
    STATE_ERROR,
    STATE_RECONNECTING,
    STATE_UP,
    TunnelManager,
    TunnelSpec,
    TunnelValidationError,
    build_ssh_argv,
    tunnel_manager,
)

__all__ = [
    "STATE_CONNECTING",
    "STATE_DOWN",
    "STATE_ERROR",
    "STATE_RECONNECTING",
    "STATE_UP",
    "TunnelManager",
    "TunnelSpec",
    "TunnelValidationError",
    "build_ssh_argv",
    "tunnel_manager",
]
