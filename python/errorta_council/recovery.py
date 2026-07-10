"""F031-02 recovery (invariant 4 — fail closed).

Three failure modes are handled explicitly:

1. **Trailing truncated line.** Ignored. Meta marked ``needs_repair`` so
   the operator can see the run was not cleanly finished.
2. **Mid-file invalid JSON.** Raise ``CorruptedRun``. The store must not
   silently skip data and pretend the run completed.
3. **Interrupted active run.** A run with cached status ``running``,
   ``paused``, or ``finalizing`` at sidecar boot is marked
   ``interrupted``. We never auto-resume model calls in Phase 0.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .schema import FORMAT_VERSION, RunMeta


class CorruptedRun(RuntimeError):
    def __init__(self, run_id: str, reason: str) -> None:
        super().__init__(f"run {run_id} corrupted: {reason}")
        self.run_id = run_id
        self.reason = reason


@dataclass(frozen=True)
class RecoveryReport:
    interrupted: list[str] = field(default_factory=list)
    needs_repair: list[str] = field(default_factory=list)
    corrupted: list[str] = field(default_factory=list)


_ACTIVE_STATUSES = {"running", "paused", "finalizing"}


def _meta_path(runs_dir: Path, run_id: str) -> Path:
    return runs_dir / f"{run_id}.meta.json"


def _log_path(runs_dir: Path, run_id: str) -> Path:
    return runs_dir / f"{run_id}.jsonl"


def _load_meta_raw(runs_dir: Path, run_id: str) -> dict[str, Any] | None:
    path = _meta_path(runs_dir, run_id)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _write_meta_raw(runs_dir: Path, run_id: str, raw: dict[str, Any]) -> None:
    path = _meta_path(runs_dir, run_id)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(raw, indent=2, sort_keys=True))
    tmp.replace(path)


def _parse_log(
    runs_dir: Path, run_id: str
) -> tuple[list[dict[str, Any]], bool]:
    """Return (events, trailing_truncated)."""
    log = _log_path(runs_dir, run_id)
    if not log.exists():
        return [], False
    raw_text = log.read_text()
    lines = raw_text.split("\n")
    # The file may or may not end with a trailing newline. After split
    # we usually get an empty trailing element when it does.
    if lines and lines[-1] == "":
        lines.pop()
    events: list[dict[str, Any]] = []
    trailing_truncated = False
    for idx, line in enumerate(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            if idx == len(lines) - 1:
                trailing_truncated = True
                continue
            raise CorruptedRun(run_id, f"invalid JSON at line {idx + 1}")
        events.append(obj)
    return events, trailing_truncated


def recover_run(run_id: str, *, runs_dir: Path) -> RunMeta:
    raw_meta = _load_meta_raw(runs_dir, run_id)
    events, trailing_truncated = _parse_log(runs_dir, run_id)

    if raw_meta is not None and not _log_path(runs_dir, run_id).exists():
        if raw_meta.get("status") in _ACTIVE_STATUSES or raw_meta.get("last_sequence", 0) > 0:
            raise CorruptedRun(run_id, "meta exists but event log missing")

    if raw_meta is None and not events:
        raise CorruptedRun(run_id, "no metadata and no events")

    if raw_meta is None:
        # Rebuild minimal meta from the event log.
        last = events[-1]
        raw_meta = {
            "format_version": FORMAT_VERSION,
            "id": run_id,
            "room_id": "",
            "room_snapshot": {},
            "conversation_id": None,
            "conversation_turn_id": None,
            "prompt": "",
            "corpus_ids": [],
            "status": "running",
            "created_at": events[0]["created_at"],
            "started_at": events[0]["created_at"],
            "updated_at": last["created_at"],
            "finished_at": None,
            "last_sequence": int(last["sequence"]),
            "event_count": len(events),
            "terminal_event_id": None,
            "resume_policy": "mark_interrupted",
            "costs": {"remote_calls": 0, "local_calls": 0,
                      "input_tokens": 0, "output_tokens": 0, "estimated_usd": 0.0},
            "capabilities": {"streaming": False, "fake_members": True, "recovered": True},
        }
    else:
        raw_meta["last_sequence"] = events[-1]["sequence"] if events else 0
        raw_meta["event_count"] = len(events)

    if trailing_truncated:
        raw_meta["resume_policy"] = "needs_repair"

    _write_meta_raw(runs_dir, run_id, raw_meta)
    return RunMeta.from_dict(raw_meta)


@dataclass(frozen=True)
class RecoverySummary:
    """Phase 1 store-based recovery result (separate from Phase-0 RecoveryReport)."""

    interrupted_runs: list[str] = field(default_factory=list)
    corrupted_runs: list[str] = field(default_factory=list)


def scan_and_recover(store_or_runs_dir=None, *, runs_dir: Path | None = None):
    """Recover non-terminal runs.

    Two call signatures (Phase 0 / Phase 1):

    - ``scan_and_recover(runs_dir=...)`` returns a Phase 0 ``RecoveryReport``
      based on filesystem scanning only.
    - ``scan_and_recover(store)`` returns a Phase 1 ``RecoverySummary`` that
      drives mid-flight cancel terminals via the ``RunStore`` API and treats
      orphan running/paused runs as ``interrupted``.
    """
    # Phase 1 path — caller passed a RunStore positional.
    # Import here to avoid module-import cycles via run_store ↔ recovery.
    from errorta_council.run_store import RunStore
    if isinstance(store_or_runs_dir, RunStore):
        return _scan_with_store(store_or_runs_dir)

    target_dir = runs_dir or store_or_runs_dir
    if target_dir is None:
        raise TypeError("scan_and_recover requires runs_dir= or a RunStore arg")

    interrupted: list[str] = []
    needs_repair: list[str] = []
    corrupted: list[str] = []
    for child in sorted(Path(target_dir).iterdir()):
        if not child.is_file() or not child.name.endswith(".meta.json"):
            continue
        run_id = child.name[: -len(".meta.json")]
        try:
            raw = json.loads(child.read_text())
        except json.JSONDecodeError:
            corrupted.append(run_id)
            continue
        if raw.get("status") in _ACTIVE_STATUSES:
            raw["status"] = "interrupted"
            raw["resume_policy"] = "mark_interrupted"
            _write_meta_raw(Path(target_dir), run_id, raw)
            interrupted.append(run_id)
    return RecoveryReport(
        interrupted=interrupted, needs_repair=needs_repair, corrupted=corrupted,
    )


def _scan_with_store(store) -> "RecoverySummary":
    """Phase 1: drive non-terminal runs to a stable terminal via the store API."""
    from dataclasses import replace as _replace
    from errorta_council.schema import EventStatus, EventType
    interrupted: list[str] = []
    corrupted: list[str] = []
    for run_id in store.list_run_ids():
        try:
            meta, events = store.read_run(run_id)
        except Exception:
            corrupted.append(run_id)
            continue
        if meta.status in ("completed", "failed", "cancelled"):
            continue
        had_cancel_req = any(e.type == EventType.RUN_CANCEL_REQUESTED for e in events)
        had_terminal = any(
            e.type in (EventType.RUN_COMPLETED, EventType.RUN_FAILED, EventType.RUN_CANCELLED)
            for e in events
        )
        if had_cancel_req and not had_terminal:
            token = store.acquire_writer(run_id)
            try:
                store.append_event(
                    run_id,
                    type=EventType.RUN_CANCELLED,
                    status=EventStatus.CANCELLED,
                    payload={"reason": "cancel_requested", "recovered_on_boot": True},
                    writer=token,
                )
            finally:
                store.release_writer(token)
            # Re-read meta to preserve the new last_sequence/event_count.
            fresh, _ = store.read_run(run_id)
            store.write_meta(_replace(
                fresh, status="cancelled", terminal_reason="cancel_requested"
            ))
            interrupted.append(run_id)
            continue
        if meta.status in ("running", "paused", "finalizing", "awaiting_user_decision"):
            store.write_meta(_replace(
                meta, status="interrupted", terminal_reason="interrupted_on_boot"
            ))
            interrupted.append(run_id)
    return RecoverySummary(interrupted_runs=interrupted, corrupted_runs=corrupted)


def validate_decision_event(payload: dict, *, current_max_rounds: int | None) -> None:
    """Invariant 7: caps are absolute. Decisions cannot raise them.

    Any payload field that would override or bypass a frozen cap raises
    ValueError('cap_invariant_violated').
    """
    forbidden_keys = (
        "override_max_rounds",
        "override_max_messages_per_member",
        "override_max_total_member_messages",
        "override_per_turn_timeout_seconds",
    )
    decision = payload.get("decision", {})
    for key in forbidden_keys:
        if key in decision:
            raise ValueError(f"cap_invariant_violated: decision_field={key}")
