"""Tests for errorta_briefs.state — CollectState persistence + resume."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from errorta_briefs.lifecycle import BriefState
from errorta_briefs.state import (
    CollectState,
    FailureRecord,
    LastCheckpoint,
    SourceState,
    load_collect_state,
    resume_offset,
    save_collect_state,
    should_resume,
)


def _make_state() -> CollectState:
    return CollectState(
        brief_id="aerospace-v1",
        corpus_name="aerospace",
        run_id="run-abc123",
        state=BriefState.RUNNING,
        last_checkpoint=LastCheckpoint(
            source_name="arxiv",
            page_or_offset=3,
            docs_collected=87,
            last_canonical_id="arxiv:2401.00123",
        ),
        per_source={
            "arxiv": SourceState(state="running", docs_collected=87, page_or_offset=3),
            "nasa_ntrs": SourceState(state="completed", docs_collected=42, page_or_offset=None),
        },
        failures=[
            FailureRecord(
                error_class="RetryableError",
                message="429 from arxiv",
                occurred_at="2026-06-07T12:00:00+00:00",
                retry_count=2,
            )
        ],
    )


def test_load_returns_none_when_file_missing(tmp_path: Path) -> None:
    assert load_collect_state(tmp_path / "nope.json") is None


def test_roundtrip_preserves_all_fields(tmp_errorta_home: Path) -> None:
    path = tmp_errorta_home / ".errorta" / "collect-state.json"
    original = _make_state()
    save_collect_state(original, path)

    loaded = load_collect_state(path)
    assert loaded is not None

    assert loaded.brief_id == original.brief_id
    assert loaded.corpus_name == original.corpus_name
    assert loaded.run_id == original.run_id
    assert loaded.state == BriefState.RUNNING
    assert isinstance(loaded.state, BriefState)

    assert loaded.last_checkpoint == original.last_checkpoint
    assert loaded.per_source == original.per_source
    assert loaded.failures == original.failures


def test_mid_run_resume_simulated_fresh_process(tmp_errorta_home: Path) -> None:
    """Save RUNNING state with checkpoint; load from a fresh handle; resume."""
    path = tmp_errorta_home / ".errorta" / "collect-state.json"
    save_collect_state(_make_state(), path)

    # Fresh load — no shared object reference with the saved instance.
    loaded = load_collect_state(path)
    assert loaded is not None
    assert loaded.state == BriefState.RUNNING
    assert should_resume(loaded)
    assert resume_offset(loaded, "arxiv") == 3
    # source without a stored offset returns None
    assert resume_offset(loaded, "nasa_ntrs") is None
    # unknown source returns None
    assert resume_offset(loaded, "no_such_source") is None


def test_should_resume_states() -> None:
    base = _make_state()
    base.state = BriefState.PAUSED
    assert should_resume(base)

    base.state = BriefState.RUNNING
    assert should_resume(base)

    base.state = BriefState.DRAFT
    assert not should_resume(base)

    base.state = BriefState.RUNNING
    base.last_checkpoint = None
    assert not should_resume(base)


def test_atomic_write_no_partial_file_on_mid_flush_exception(
    tmp_errorta_home: Path,
) -> None:
    """If the temp write raises mid-flush, the target file is untouched and no
    .tmp leftover lingers."""
    path = tmp_errorta_home / ".errorta" / "collect-state.json"

    # Pre-existing good state — must survive the failed write.
    good = _make_state()
    good.state = BriefState.PAUSED
    save_collect_state(good, path)
    pre_bytes = path.read_bytes()

    bad = _make_state()
    # Force json.dump to blow up mid-flush.
    boom = RuntimeError("disk exploded")
    with patch("errorta_briefs.state.json.dump", side_effect=boom):
        with pytest.raises(RuntimeError):
            save_collect_state(bad, path)

    # Target file untouched.
    assert path.read_bytes() == pre_bytes
    # No stray .tmp file.
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    assert not tmp_path.exists()

    # And it still loads cleanly.
    loaded = load_collect_state(path)
    assert loaded is not None
    assert loaded.state == BriefState.PAUSED


def test_save_creates_parent_dir(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deeper" / "state.json"
    save_collect_state(_make_state(), target)
    assert target.exists()


def test_save_writes_valid_json(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    save_collect_state(_make_state(), target)
    raw = json.loads(target.read_text())
    assert raw["state"] == "RUNNING"
    assert raw["brief_id"] == "aerospace-v1"
    assert raw["last_checkpoint"]["source_name"] == "arxiv"
    assert raw["last_checkpoint"]["page_or_offset"] == 3


def test_load_handles_no_checkpoint(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    fresh = CollectState(
        brief_id="b", corpus_name="c", run_id="r", state=BriefState.DRAFT
    )
    save_collect_state(fresh, target)
    loaded = load_collect_state(target)
    assert loaded is not None
    assert loaded.last_checkpoint is None
    assert loaded.per_source == {}
    assert loaded.failures == []
    assert loaded.state == BriefState.DRAFT
