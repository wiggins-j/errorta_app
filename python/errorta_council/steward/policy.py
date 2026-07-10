"""F038 Council Steward policy resolution.

Room JSON can arrive as a typed ``CouncilRoom`` or as a raw room snapshot. Keep
defaulting in one place so validation, scheduling, and future UI rebuild routes
do not each hand-parse ``steward_policy``.
"""
from __future__ import annotations

from typing import Any

from errorta_council.schema import CouncilRoom, StewardPolicy


def resolve_steward_policy(room: CouncilRoom | dict[str, Any]) -> StewardPolicy:
    """Return a defaults-applied StewardPolicy for a room or room snapshot."""
    if isinstance(room, CouncilRoom):
        return room.steward_policy
    raw = dict(room.get("steward_policy") or {})
    return StewardPolicy.from_dict(raw)
