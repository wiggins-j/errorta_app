"""F037 callout policy + roster resolution.

Room JSON arrives as a typed ``CouncilRoom`` or a raw room snapshot. Keep
defaulting in one place so the route, scheduler, and validation do not each
hand-parse ``escalation_policy`` / ``escalation_roster``.
"""
from __future__ import annotations

from typing import Any

from errorta_council.schema import (
    CouncilRoom,
    EscalationPolicy,
    EscalationRosterEntry,
)


def resolve_callout_policy(
    room: CouncilRoom | dict[str, Any],
) -> EscalationPolicy:
    """Return a defaults-applied EscalationPolicy for a room or snapshot."""
    if isinstance(room, CouncilRoom):
        return room.escalation_policy
    return EscalationPolicy.from_dict(dict(room.get("escalation_policy") or {}))


def resolve_roster(
    room: CouncilRoom | dict[str, Any],
) -> list[EscalationRosterEntry]:
    if isinstance(room, CouncilRoom):
        return list(room.escalation_roster)
    return [
        EscalationRosterEntry.from_dict(dict(e))
        for e in (room.get("escalation_roster") or [])
    ]


def find_target(
    room: CouncilRoom | dict[str, Any], target_id: str
) -> EscalationRosterEntry | None:
    for entry in resolve_roster(room):
        if entry.id == target_id:
            return entry
    return None
