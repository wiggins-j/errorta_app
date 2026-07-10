"""Budget ledger and enforcement for the model gateway."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from . import storage
from .policy import BudgetPolicy


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso_z() -> str:
    return _now().strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class BudgetEntry:
    created_at: str
    audit_id: str
    provider: str
    role: str
    remote: bool
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    session_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BudgetStatus:
    allowed: bool
    reason: str | None
    remote_calls_today: int
    remote_calls_this_session: int
    remote_tokens_today: int
    estimated_usd_this_month: float
    requested_estimated_cost_usd: float
    session_id_bucket: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def record_usage(
    *,
    audit_id: str,
    provider: str,
    role: str,
    remote: bool,
    input_tokens: int,
    output_tokens: int = 0,
    estimated_cost_usd: float = 0.0,
    session_id: str | None = None,
) -> BudgetEntry:
    entry = BudgetEntry(
        created_at=_now_iso_z(),
        audit_id=audit_id,
        provider=provider,
        role=role,
        remote=bool(remote),
        input_tokens=max(0, int(input_tokens)),
        output_tokens=max(0, int(output_tokens)),
        estimated_cost_usd=max(0.0, float(estimated_cost_usd)),
        session_id=session_id,
    )
    storage.append_jsonl(storage.budget_ledger_path(), entry.to_dict())
    return entry


def _rows() -> list[dict[str, Any]]:
    return storage.read_jsonl(storage.budget_ledger_path())


def _session_bucket(policy: BudgetPolicy, session_id: str | None) -> str | None:
    if policy.max_remote_calls_per_session is None:
        return None
    normalized = (session_id or "").strip()
    return normalized or "anonymous"


def status(
    policy: BudgetPolicy,
    *,
    requested_input_tokens: int = 0,
    requested_estimated_cost_usd: float = 0.0,
    session_id: str | None = None,
) -> BudgetStatus:
    """Return whether a new remote call fits the configured budget."""
    now = _now()
    today = now.date()
    month_key = (now.year, now.month)
    session_bucket = _session_bucket(policy, session_id)
    remote_calls_today = 0
    remote_calls_this_session = 0
    remote_tokens_today = 0
    estimated_usd_this_month = 0.0

    for row in _rows():
        if not row.get("remote"):
            continue
        created = _parse_dt(row.get("created_at"))
        if created is None:
            continue
        input_tokens = int(row.get("input_tokens") or 0)
        output_tokens = int(row.get("output_tokens") or 0)
        if created.date() == today:
            remote_calls_today += 1
            remote_tokens_today += input_tokens + output_tokens
        if (created.year, created.month) == month_key:
            estimated_usd_this_month += float(row.get("estimated_cost_usd") or 0.0)
        row_bucket = _session_bucket(policy, row.get("session_id"))
        if session_bucket is not None and row_bucket == session_bucket:
            remote_calls_this_session += 1

    requested = max(0, int(requested_input_tokens))
    requested_cost = max(0.0, float(requested_estimated_cost_usd))
    reason: str | None = None
    if policy.max_tokens_per_call is not None and requested > policy.max_tokens_per_call:
        reason = "max_tokens_per_call exceeded"
    elif policy.max_remote_calls_per_day is not None and (
        remote_calls_today >= policy.max_remote_calls_per_day
    ):
        reason = "max_remote_calls_per_day exhausted"
    elif policy.max_remote_calls_per_session is not None and (
        remote_calls_this_session >= policy.max_remote_calls_per_session
    ):
        reason = "max_remote_calls_per_session exhausted"
    elif policy.max_remote_tokens_per_day is not None and (
        remote_tokens_today + requested > policy.max_remote_tokens_per_day
    ):
        reason = "max_remote_tokens_per_day exceeded"
    elif policy.max_usd_per_month is not None and (
        estimated_usd_this_month + requested_cost > policy.max_usd_per_month
    ):
        reason = "max_usd_per_month exhausted"

    return BudgetStatus(
        allowed=reason is None,
        reason=reason,
        remote_calls_today=remote_calls_today,
        remote_calls_this_session=remote_calls_this_session,
        remote_tokens_today=remote_tokens_today,
        estimated_usd_this_month=round(estimated_usd_this_month, 6),
        requested_estimated_cost_usd=round(requested_cost, 6),
        session_id_bucket=session_bucket,
    )


def summary(policy: BudgetPolicy) -> dict[str, Any]:
    return status(policy).to_dict()
