"""F129 append-only, metadata-only model-attempt performance corpus."""
from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any

_LOCK = threading.RLock()
_OUTCOMES = {"accepted", "rejected", "gateway_failed", "cancelled"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: str
    assignment_id: str
    project_id: str
    run_id: str
    task_id: str
    member_id: str
    route_id: str
    task_type: str
    difficulty_tier: str
    capability_tier: str
    cost_tier: int
    started_at: str
    latency_ms: int
    outcome: str
    reason_code: str = ""
    triggered_escalation: bool = False
    task_had_prior_escalation: bool = False

    def __post_init__(self) -> None:
        if self.outcome not in _OUTCOMES:
            raise ValueError(f"unknown attempt outcome: {self.outcome}")
        if not self.route_id:
            raise ValueError("route_id is required")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AttemptRecord":
        known = {key: value for key, value in raw.items() if key in cls.__dataclass_fields__}
        return cls(**known)


def make_attempt(**kwargs: Any) -> AttemptRecord:
    return AttemptRecord(
        attempt_id=str(kwargs.pop("attempt_id", f"mat-{uuid.uuid4().hex[:12]}")),
        started_at=str(kwargs.pop("started_at", _now())),
        **kwargs,
    )


def corpus_path() -> Path:
    from errorta_app.paths import errorta_home

    path = errorta_home() / "council" / "performance" / "attempts.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def append(record: AttemptRecord, path: Path | None = None) -> None:
    destination = path or corpus_path()
    line = json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
    with _LOCK:
        fd = os.open(destination, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.chmod(destination, 0o600)
        except OSError:
            pass


def read_records(path: Path | None = None, *, limit: int = 5000) -> list[AttemptRecord]:
    source = path or corpus_path()
    try:
        lines = source.read_text("utf-8").splitlines()
    except OSError:
        return []
    records: list[AttemptRecord] = []
    for line in lines[-max(0, limit):]:
        try:
            raw = json.loads(line)
            if isinstance(raw, dict):
                records.append(AttemptRecord.from_dict(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return records


# Rolling window the corpus is aggregated over (F129 choice; F135 surfaces it).
WINDOW_DAYS = 90


def _bucket_stats(
    path: Path | None = None, *, now: datetime | None = None,
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Structured aggregation keyed ``(route_id, task_type, difficulty_tier)``.

    Single grouping pass consumed by both ``digest()`` (which fuses the key into
    the ``"task_type:difficulty"`` string the selector reads) and F135's
    ``learning_digest()`` (which keeps the tuple structured — no string-splitting).
    """
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=WINDOW_DAYS)
    grouped: dict[tuple[str, str, str], list[AttemptRecord]] = {}
    for record in read_records(path):
        try:
            ts = datetime.fromisoformat(record.started_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts < cutoff:
            continue
        grouped.setdefault(
            (record.route_id, record.task_type, record.difficulty_tier), [],
        ).append(record)
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for key, records in grouped.items():
        accepted = sum(record.outcome == "accepted" for record in records)
        failures = sum(record.outcome == "gateway_failed" for record in records)
        out[key] = {
            "attempts": len(records),
            "accepted": accepted,
            "accepted_rate": accepted / len(records),
            "gateway_failure_rate": failures / len(records),
            "p50_latency_ms": int(median(record.latency_ms for record in records)),
            "avg_cost_tier": sum(record.cost_tier for record in records) / len(records),
        }
    return out


def digest(path: Path | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for (route_id, task_type, difficulty), stats in _bucket_stats(path, now=now).items():
        out.setdefault(route_id, {})[f"{task_type}:{difficulty}"] = stats
    return out


def _standing(attempts: int, accepted_rate: float) -> str:
    """F135 four-way standing from the shared selector thresholds."""
    from .model_selector import (
        DEMOTION_ACCEPTED_RATE,
        MIN_ATTEMPTS_FOR_SIGNAL,
        PREFERRED_ACCEPTED_RATE,
    )

    if attempts < MIN_ATTEMPTS_FOR_SIGNAL:
        return "insufficient_data"
    if accepted_rate < DEMOTION_ACCEPTED_RATE:
        return "demoted"
    if accepted_rate < PREFERRED_ACCEPTED_RATE:
        return "cautioned"
    return "preferred"


def learning_digest(
    path: Path | None = None, *, now: datetime | None = None,
) -> dict[str, Any]:
    """F135 global, cross-project projection of the performance corpus.

    Read-only. Never filtered by ``project_id`` — a new project benefits from
    every prior project's attempts. Metadata only (no task content). Fail-open:
    a missing/empty/malformed corpus yields ``corpus_available: false`` and an
    empty ``routes`` list rather than raising.
    """
    from .model_selector import (
        DEMOTION_ACCEPTED_RATE,
        MIN_ATTEMPTS_FOR_SIGNAL,
        PREFERRED_ACCEPTED_RATE,
    )

    record_count = len(read_records(path))
    buckets = _bucket_stats(path, now=now)
    by_route: dict[str, list[dict[str, Any]]] = {}
    total_attempts = 0
    for (route_id, task_type, difficulty), stats in buckets.items():
        total_attempts += stats["attempts"]
        by_route.setdefault(route_id, []).append({
            "task_type": task_type,
            "difficulty_tier": difficulty,
            **stats,
            "standing": _standing(stats["attempts"], stats["accepted_rate"]),
        })

    try:
        from .model_catalog import load_catalog

        catalog = load_catalog(list(by_route.keys()))
    except Exception:
        catalog = {}

    routes: list[dict[str, Any]] = []
    for route_id in sorted(by_route):
        entry = catalog.get(route_id)
        routes.append({
            "route_id": route_id,
            "capability_tier": entry.capability_tier if entry else "mid",
            "cost_tier": entry.cost_tier if entry else 0,
            "tiers_unset": bool(entry.tiers_unset) if entry else True,
            "buckets": sorted(
                by_route[route_id],
                key=lambda b: (b["difficulty_tier"], b["task_type"]),
            ),
        })

    generated_at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    return {
        "summary": {
            "total_attempts": total_attempts,
            "distinct_routes": len(routes),
            "window_days": WINDOW_DAYS,
            "generated_at": generated_at,
            "corpus_available": record_count > 0,
        },
        "thresholds": {
            "min_attempts": MIN_ATTEMPTS_FOR_SIGNAL,
            "demotion_rate": DEMOTION_ACCEPTED_RATE,
            "preferred_rate": PREFERRED_ACCEPTED_RATE,
        },
        "routes": routes,
    }


def buffer_pending_attempt(
    task_extras: dict[str, Any], payload: dict[str, Any],
) -> dict[str, Any]:
    """Return a copy of ``task_extras`` with ``payload`` appended to the
    ``_f129_pending`` list. Used when a productive turn should be held pending
    until task-boundary review closes/escalates it (F129 Contract #7)."""
    prior = list(task_extras.get("_f129_pending") or [])
    prior.append(dict(payload))
    return {**task_extras, "_f129_pending": prior}


def flush_pending_attempts(
    task_extras: dict[str, Any], outcome: str, *, path: Path | None = None,
) -> tuple[int, dict[str, Any]]:
    """Flush every buffered pending payload on the task as ``outcome`` records.

    Returns ``(count_written, cleaned_extras)`` where ``cleaned_extras`` is a
    copy of ``task_extras`` with ``_f129_pending`` removed. The caller must
    persist ``cleaned_extras`` (typically via ``ledger.update_task``) so the
    same payloads aren't flushed twice on resume/compaction."""
    if outcome not in _OUTCOMES:
        raise ValueError(f"unknown flush outcome: {outcome}")
    pending = task_extras.get("_f129_pending") or []
    if not isinstance(pending, list) or not pending:
        return 0, dict(task_extras)
    written = 0
    for payload in pending:
        if not isinstance(payload, dict):
            continue
        clean: dict[str, Any] = {k: v for k, v in payload.items() if k != "outcome"}
        try:
            append(make_attempt(outcome=outcome, **clean), path=path)
            written += 1
        except (ValueError, TypeError, OSError):
            # Telemetry loss must never break the run loop.
            continue
    cleaned = {k: v for k, v in task_extras.items() if k != "_f129_pending"}
    return written, cleaned


__all__ = [
    "AttemptRecord", "append", "buffer_pending_attempt", "corpus_path",
    "digest", "flush_pending_attempts", "learning_digest", "make_attempt",
    "read_records",
]
