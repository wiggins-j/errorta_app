"""F-DIST-01 — alpha delivery, licensing, and transparent telemetry (sidecar half).

This package owns everything the desktop sidecar needs to gate the private
alpha: the first-run device identity, the Ed25519 license token, the offline
grace / lock state machine, and the *sole* network egress to
``api.errorta.app``.

Design invariants (see docs/specs/F-DIST-01-*.md):
  - Invariant 1 (no silent egress): ``client`` is the ONLY module here that
    talks to ``api.errorta.app``; ``errorta_council`` never imports
    ``errorta_alpha`` (locked by ``tests/alpha/test_no_egress_guard.py``).
  - Invariant 4 (offline never bricks): the app only locks on a ``revoked``
    heartbeat or a fully-expired grace window; a transient ``404`` in grace
    does not lock; a clock rollback cannot extend grace (``max_seen_epoch``).
  - Invariant 5 (lock is server-side): ``enforce_not_locked`` is called by the
    answering routes so a locked tester gets ``403 alpha_locked`` even if the
    UI is bypassed.
  - Invariant 6 (the gate is throwaway): with ``ERRORTA_ALPHA_GATE`` off the
    app never activates and never locks — the exact v1.0 posture, no migration.

Nothing here imports ``aiar`` or ``errorta_council``.
"""
from __future__ import annotations

from .config import GRACE_DAYS, api_base_url, gate_enabled
from .state import AlphaState, current_status, enforce_not_locked, is_locked

__all__ = [
    "AlphaState",
    "GRACE_DAYS",
    "api_base_url",
    "current_status",
    "enforce_not_locked",
    "gate_enabled",
    "is_locked",
]
