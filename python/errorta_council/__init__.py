"""F031 Council — durable persistence foundation (Phase 0).

This package owns Council storage: schema, residency-aware paths,
room store, run/event log, recovery, and the deterministic fake run
writer. It does **not** call models, route context, or schedule turns.

Invariants enforced here (see docs/specs/F031-00-council-implementation-guide.md):

- 2  One writer per run; sequence assigned centrally by RunStore.
- 4  Fail closed in validation + recovery.
- 8  All paths resolve under errorta_app.paths.errorta_home().
- 10 Fake members ship here, not in tests.
- 11 format_version = 1; unknown fields tolerated; future versions rejected.
- 12 Errors use {code, message, retryable, details}.
"""
from __future__ import annotations

from .schema import (
    FORMAT_VERSION,
    CouncilEvent,
    CouncilEventError,
    EventStatus,
    EventType,
    MemberSnapshot,
    UnsupportedFormatVersion,
)

__all__ = [
    "FORMAT_VERSION",
    "CouncilEvent",
    "CouncilEventError",
    "EventStatus",
    "EventType",
    "MemberSnapshot",
    "UnsupportedFormatVersion",
]
