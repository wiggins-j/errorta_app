"""License record persistence (``license.json``).

Holds the server-issued facts the sidecar needs to decide the lock state
offline: the signed token, the grace deadline, the last known server status,
the forward-only clock high-water mark, and any build-EOL signal. All writes are
atomic + 0600 (see ``storage``). This is the ONLY app-side artifact retired at
v1.0.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from errorta_app.paths import alpha_license_path

from .storage import read_json, write_json_0600


def _coerce_int(value: Any) -> int | None:
    try:
        if isinstance(value, bool):  # bool is an int subclass — reject it
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class LicenseRecord:
    token: str
    grace_until: int
    status: str = "active"  # "active" | "revoked"
    code: str | None = None
    last_heartbeat: int | None = None
    # Forward-only high-water mark of the local clock at successful check-ins,
    # so a clock rollback can't silently extend grace (spec §8).
    max_seen_epoch: int = 0
    # build_eol = the server flagged this build as retired (soft nudge);
    # build_eol_required = the update is mandatory (locks answering). A soft EOL
    # only shows a non-blocking "update available" banner.
    build_eol: bool = False
    build_eol_required: bool = False
    update_url: str | None = None
    revoke_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "grace_until": self.grace_until,
            "status": self.status,
            "code": self.code,
            "last_heartbeat": self.last_heartbeat,
            "max_seen_epoch": self.max_seen_epoch,
            "build_eol": self.build_eol,
            "build_eol_required": self.build_eol_required,
            "update_url": self.update_url,
            "revoke_reason": self.revoke_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LicenseRecord | None":
        token = data.get("token")
        grace_until = _coerce_int(data.get("grace_until"))
        if not isinstance(token, str) or not token or grace_until is None:
            return None
        status = data.get("status")
        status = status if status in ("active", "revoked") else "active"
        return cls(
            token=token,
            grace_until=grace_until,
            status=status,
            code=data.get("code") if isinstance(data.get("code"), str) else None,
            last_heartbeat=_coerce_int(data.get("last_heartbeat")),
            max_seen_epoch=_coerce_int(data.get("max_seen_epoch")) or 0,
            build_eol=bool(data.get("build_eol")),
            build_eol_required=bool(data.get("build_eol_required")),
            update_url=data.get("update_url")
            if isinstance(data.get("update_url"), str)
            else None,
            revoke_reason=data.get("revoke_reason")
            if isinstance(data.get("revoke_reason"), str)
            else None,
        )


def load() -> LicenseRecord | None:
    """Return the persisted license record, or ``None`` if absent/corrupt."""
    data = read_json(alpha_license_path())
    if not data:
        return None
    return LicenseRecord.from_dict(data)


def store(record: LicenseRecord) -> None:
    write_json_0600(alpha_license_path(), record.to_dict())


def clear() -> None:
    """Remove ``license.json`` (used on reactivation / tests)."""
    alpha_license_path().unlink(missing_ok=True)
