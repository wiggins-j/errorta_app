"""Callout id minting. Path-safe, prefixed, collision-resistant."""
from __future__ import annotations

import uuid

_PREFIX = "co_"


def new_callout_id() -> str:
    return f"{_PREFIX}{uuid.uuid4().hex[:16]}"
