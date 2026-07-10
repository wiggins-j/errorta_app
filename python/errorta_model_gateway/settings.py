"""Load and persist model gateway policy."""
from __future__ import annotations

import json
from typing import Any

from . import storage
from .policy import GatewayPolicy


def load_policy() -> GatewayPolicy:
    path = storage.policy_path()
    if not path.exists():
        return GatewayPolicy.default()
    try:
        raw: Any = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return GatewayPolicy.default()
    return GatewayPolicy.from_dict(raw)


def save_policy(policy: GatewayPolicy) -> GatewayPolicy:
    stamped = policy.with_timestamp()
    storage.write_json_atomic(storage.policy_path(), stamped.to_dict())
    return stamped


def reset_policy() -> GatewayPolicy:
    return save_policy(GatewayPolicy.default())
