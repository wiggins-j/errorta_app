"""F008d-lifecycle — CollectState persistence + resume helpers.

A ``CollectState`` is the on-disk record of a single brief-driven collection
run. The orchestrator writes it after every meaningful checkpoint
(per-source page boundary, per-document commit) so a crashed/paused run can
resume without re-downloading what we already have.

Persistence is atomic: we serialize to JSON in a temp file in the same
directory as the target, ``fsync``, then ``os.replace`` over the target so
readers never see a partially-written file.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from errorta_briefs.lifecycle import BriefState


def _utcnow_iso() -> str:
    """Return an ISO-8601 UTC timestamp string."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class LastCheckpoint:
    """Where the most recent successful collection step stopped."""

    source_name: str
    page_or_offset: int | None = None
    docs_collected: int = 0
    last_canonical_id: str | None = None


@dataclass
class SourceState:
    """Per-source progress within a run.

    The canonical counter is ``docs_ingested_to_corpus`` — incremented only
    after the F004 corpus pipeline has successfully enqueued the doc.
    ``docs_collected`` survives as a backwards-compatible alias property so
    older callers (including persisted JSON written before the rename) keep
    working without migration.
    """

    state: str = "pending"
    docs_ingested_to_corpus: int = 0
    page_or_offset: int | None = None

    def __init__(
        self,
        state: str = "pending",
        docs_ingested_to_corpus: int | None = None,
        page_or_offset: int | None = None,
        *,
        docs_collected: int | None = None,
    ) -> None:
        # Accept either spelling so legacy callers (and older on-disk JSON
        # rehydrated via ``SourceState(**values)``) keep working.
        self.state = state
        if docs_ingested_to_corpus is None:
            docs_ingested_to_corpus = docs_collected if docs_collected is not None else 0
        self.docs_ingested_to_corpus = docs_ingested_to_corpus
        self.page_or_offset = page_or_offset

    @property
    def docs_collected(self) -> int:
        return self.docs_ingested_to_corpus

    @docs_collected.setter
    def docs_collected(self, value: int) -> None:
        self.docs_ingested_to_corpus = value


@dataclass
class FailureRecord:
    """One failure event logged during a run (retryable or fatal)."""

    error_class: str
    message: str
    occurred_at: str = field(default_factory=_utcnow_iso)
    retry_count: int = 0


@dataclass
class CollectState:
    """Top-level resumable state for a brief collection run."""

    brief_id: str
    corpus_name: str
    run_id: str
    started_at: str = field(default_factory=_utcnow_iso)
    updated_at: str = field(default_factory=_utcnow_iso)
    state: BriefState = BriefState.DRAFT
    last_checkpoint: LastCheckpoint | None = None
    per_source: dict[str, SourceState] = field(default_factory=dict)
    failures: list[FailureRecord] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Serialization                                                               #
# --------------------------------------------------------------------------- #


def _to_jsonable(state: CollectState) -> dict[str, Any]:
    """Convert a CollectState to a JSON-safe dict."""
    payload = asdict(state)
    # asdict turns the enum into its raw value when the enum is a str subclass,
    # but be explicit so behaviour is stable across Python versions.
    payload["state"] = state.state.value
    return payload


def save_collect_state(state: CollectState, path: Path) -> None:
    """Atomically persist ``state`` to ``path`` as JSON.

    Writes to ``<path>.tmp`` in the same directory, fsyncs, then
    ``os.replace`` is the atomic rename. If the write raises mid-flush the
    temp file is removed so the target is never partially overwritten.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    state.updated_at = _utcnow_iso()
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(_to_jsonable(state), fh, indent=2, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        # Make sure no half-written temp file lingers.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def load_collect_state(path: Path) -> CollectState | None:
    """Load a previously-saved ``CollectState`` from ``path``.

    Returns ``None`` if the file does not exist (a fresh run).
    """
    path = Path(path)
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    checkpoint_data = data.get("last_checkpoint")
    last_checkpoint = (
        LastCheckpoint(**checkpoint_data) if checkpoint_data is not None else None
    )
    per_source = {
        name: SourceState(**values) for name, values in (data.get("per_source") or {}).items()
    }
    failures = [FailureRecord(**rec) for rec in (data.get("failures") or [])]

    return CollectState(
        brief_id=data["brief_id"],
        corpus_name=data["corpus_name"],
        run_id=data["run_id"],
        started_at=data["started_at"],
        updated_at=data["updated_at"],
        state=BriefState(data["state"]),
        last_checkpoint=last_checkpoint,
        per_source=per_source,
        failures=failures,
    )


# --------------------------------------------------------------------------- #
# Resumability helpers                                                         #
# --------------------------------------------------------------------------- #


def should_resume(state: CollectState) -> bool:
    """True if ``state`` is mid-flight (RUNNING/PAUSED) with a checkpoint."""
    return (
        state.state in {BriefState.RUNNING, BriefState.PAUSED}
        and state.last_checkpoint is not None
    )


def resume_offset(state: CollectState, source_name: str) -> int | None:
    """Return the stored page/offset for ``source_name``, or ``None``.

    Prefers the per-source map (richer; survives across sources), and falls
    back to ``last_checkpoint`` when its ``source_name`` matches.
    """
    src = state.per_source.get(source_name)
    if src is not None and src.page_or_offset is not None:
        return src.page_or_offset
    if (
        state.last_checkpoint is not None
        and state.last_checkpoint.source_name == source_name
    ):
        return state.last_checkpoint.page_or_offset
    return None
