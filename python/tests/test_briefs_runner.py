"""F008e — BriefRunner orchestration tests.

Covers:
- Happy-path run: accepted docs are passed to ingest callback, state COMPLETED.
- Compliance refusal: refused docs are NOT ingested, surfaced in compliance_refusals.
- Interrupt + resume: state is checkpointed; a new runner continues from the
  last checkpoint without double-ingesting.
- Retryable error: exponential backoff is exercised via an injected sleep.

All hermetic — no network. Connectors are in-process fakes.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest

from errorta_briefs import BriefConfig, SourceSpec
from errorta_briefs.compliance import ComplianceGate
from errorta_briefs.connector import (
    FatalError,
    RetryableError,
    SourceConnector,
    SourceDoc,
)
from errorta_briefs.lifecycle import BriefState
from errorta_briefs.runner import (
    CONNECTOR_REGISTRY,
    BriefRunner,
    load_collect_state,
    load_dedup_index,
    load_run_extras,
    reset_active_run,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _accepted_doc(canonical_id: str) -> SourceDoc:
    return SourceDoc(
        canonical_id=canonical_id,
        title=f"Doc {canonical_id}",
        source_url=f"https://example.test/{canonical_id}",
        publication_date="2026-01-01",
        sensitivity_class="Public",
        redistribution_allowed=True,
        license="CC-BY",
        extra={},
    )


def _refused_doc(canonical_id: str) -> SourceDoc:
    return SourceDoc(
        canonical_id=canonical_id,
        title=f"Doc {canonical_id}",
        source_url=f"https://example.test/{canonical_id}",
        publication_date="2026-01-01",
        sensitivity_class="Internal",  # forces refusal
        redistribution_allowed=False,
        license="unknown",
        extra={},
    )


class FakeConnector(SourceConnector):
    """In-memory connector driven by a class-level pages list.

    Tests configure ``FakeConnector.pages`` (list of list-of-SourceDoc) before
    instantiation. An empty page terminates the source.
    """

    pages: list[list[SourceDoc]] = []
    fetch_log: list[str] = []

    def __init__(self, config: dict) -> None:
        self.config = config

    def search(self, page: int) -> Iterator[SourceDoc]:
        if page < len(self.pages):
            for d in self.pages[page]:
                yield d
        # else: empty -> end of stream

    def fetch(self, doc: SourceDoc) -> bytes:
        self.fetch_log.append(doc.canonical_id)
        return b"payload:" + doc.canonical_id.encode()

    def canonical_id(self, doc: SourceDoc) -> str:
        return doc.canonical_id

    def metadata(self, doc: SourceDoc) -> dict:
        return {"canonical_id": doc.canonical_id, "title": doc.title}

    def status(self) -> dict:
        return {"ok": True}


@pytest.fixture(autouse=True)
def _reset_singleton_lock():
    """Ensure no leaked active-run lock from a prior test."""
    reset_active_run()
    CONNECTOR_REGISTRY.clear()
    FakeConnector.pages = []
    FakeConnector.fetch_log = []
    yield
    reset_active_run()
    CONNECTOR_REGISTRY.clear()


def _config(sources: list[str] | None = None) -> BriefConfig:
    return BriefConfig(
        project="Test Project",
        corpus="test-corpus",
        sources=[SourceSpec(name=n, config={}) for n in (sources or ["fake"])],
        sensitivity="Public",
        refresh="manual",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_happy_path_run_completes_and_ingests(tmp_errorta_home: Path) -> None:
    CONNECTOR_REGISTRY["fake"] = FakeConnector
    FakeConnector.pages = [
        [_accepted_doc("doc-1"), _accepted_doc("doc-2")],
        [_accepted_doc("doc-3")],
        [],
    ]
    runner = BriefRunner(sleep=lambda s: None)
    corpus_root = tmp_errorta_home / ".errorta" / "corpora"
    corpus_root.mkdir(parents=True, exist_ok=True)
    run_id = runner.submit("test-corpus", _config(), corpus_root)
    assert isinstance(run_id, str) and run_id
    assert runner.wait(timeout=5.0)

    brief_dir = corpus_root / "test-corpus"
    cs = load_collect_state(brief_dir)
    assert cs is not None
    assert cs.state == BriefState.COMPLETED
    assert runner.ingest_call_count == 3
    assert cs.per_source["fake"].docs_collected == 3
    assert cs.per_source["fake"].state == "completed"
    assert load_dedup_index(brief_dir) == {"doc-1", "doc-2", "doc-3"}


def test_refusal_only_run_ingests_nothing(tmp_errorta_home: Path) -> None:
    CONNECTOR_REGISTRY["fake"] = FakeConnector
    FakeConnector.pages = [[_refused_doc("bad-1"), _refused_doc("bad-2")], []]

    ingest_calls: list[str] = []

    def ingest(doc: SourceDoc, payload: bytes, metadata: dict) -> None:
        ingest_calls.append(doc.canonical_id)

    runner = BriefRunner(ingest=ingest, sleep=lambda s: None)
    corpus_root = tmp_errorta_home / ".errorta" / "corpora"
    corpus_root.mkdir(parents=True, exist_ok=True)
    runner.submit("test-corpus", _config(), corpus_root)
    assert runner.wait(timeout=5.0)

    brief_dir = corpus_root / "test-corpus"
    cs = load_collect_state(brief_dir)
    assert cs is not None
    assert cs.state == BriefState.COMPLETED
    assert ingest_calls == []
    extras = load_run_extras(brief_dir)
    assert len(extras.compliance_refusals) == 2
    assert all("refusal_reason" in r for r in extras.compliance_refusals)
    assert extras.per_source["fake"].docs_refused == 2
    # ingested ids list is empty
    assert extras.ingested_canonical_ids == []


def test_interrupt_then_resume_continues_without_double_ingest(
    tmp_errorta_home: Path,
) -> None:
    CONNECTOR_REGISTRY["fake"] = FakeConnector
    # Six documents across two pages. The first runner is paused after the
    # first page; the second runner must pick up at page 1 and only ingest
    # the remaining three.
    FakeConnector.pages = [
        [_accepted_doc(f"doc-{i}") for i in range(5)],
        [_accepted_doc(f"doc-{i}") for i in range(5, 8)],
        [],
    ]

    pause_after = threading.Event()
    ingested: list[str] = []
    ingest_lock = threading.Lock()

    runner1 = BriefRunner(sleep=lambda s: None)

    def ingest_1(doc: SourceDoc, payload: bytes, metadata: dict) -> None:
        with ingest_lock:
            ingested.append(doc.canonical_id)
            if len(ingested) == 2:
                pause_after.set()
                # Cooperatively halt the loop after this doc is dedup-recorded.
                runner1.stop()

    runner1._ingest = ingest_1
    corpus_root = tmp_errorta_home / ".errorta" / "corpora"
    corpus_root.mkdir(parents=True, exist_ok=True)

    run_id_1 = runner1.submit("test-corpus", _config(), corpus_root)
    assert pause_after.wait(timeout=5.0)
    assert runner1.wait(timeout=5.0)

    brief_dir = corpus_root / "test-corpus"
    cs_1 = load_collect_state(brief_dir)
    assert cs_1 is not None
    first_round_count = len(ingested)
    assert first_round_count >= 2  # at least 2 (we stopped at exactly 2)
    # dedup index persists what was actually ingested.
    dedup_after_1 = load_dedup_index(brief_dir)
    assert dedup_after_1 == set(ingested)

    # Simulate process death: clear singleton state and start a fresh runner.
    reset_active_run()

    second_round_ingested: list[str] = []

    def ingest_2(doc: SourceDoc, payload: bytes, metadata: dict) -> None:
        second_round_ingested.append(doc.canonical_id)

    runner2 = BriefRunner(ingest=ingest_2, sleep=lambda s: None)
    runner2.submit("test-corpus", _config(), corpus_root, resume=True)
    assert runner2.wait(timeout=5.0)

    cs_2 = load_collect_state(brief_dir)
    assert cs_2 is not None
    assert cs_2.state == BriefState.COMPLETED.value
    # No double-ingest: nothing already in the dedup index is re-ingested.
    assert not (set(second_round_ingested) & dedup_after_1)
    # All 8 docs end up in the dedup index.
    final_dedup = load_dedup_index(brief_dir)
    assert final_dedup == {f"doc-{i}" for i in range(8)}


def test_unknown_connector_marks_source_failed(tmp_errorta_home: Path) -> None:
    # No CONNECTOR_REGISTRY entry for 'fake'.
    runner = BriefRunner(sleep=lambda s: None)
    corpus_root = tmp_errorta_home / ".errorta" / "corpora"
    corpus_root.mkdir(parents=True, exist_ok=True)
    runner.submit("test-corpus", _config(), corpus_root)
    assert runner.wait(timeout=5.0)
    cs = load_collect_state(corpus_root / "test-corpus")
    assert cs is not None
    assert cs.state == BriefState.FAILED.value
    assert cs.per_source["fake"].state == "failed"
    assert any(f.error_class == "FatalError" for f in cs.failures)


def test_retryable_then_success_uses_backoff(tmp_errorta_home: Path) -> None:
    class FlakyConnector(SourceConnector):
        call_count = 0

        def __init__(self, config: dict) -> None:
            pass

        def search(self, page: int) -> Iterator[SourceDoc]:
            FlakyConnector.call_count += 1
            if FlakyConnector.call_count == 1:
                raise RetryableError("transient 503")
            if page == 0:
                yield _accepted_doc("ok-1")
            # else: empty page -> done

        def fetch(self, doc: SourceDoc) -> bytes:
            return b"ok"

        def canonical_id(self, doc: SourceDoc) -> str:
            return doc.canonical_id

        def metadata(self, doc: SourceDoc) -> dict:
            return {}

        def status(self) -> dict:
            return {"ok": True}

    CONNECTOR_REGISTRY["fake"] = FlakyConnector
    sleeps: list[float] = []
    runner = BriefRunner(sleep=lambda s: sleeps.append(s))
    corpus_root = tmp_errorta_home / ".errorta" / "corpora"
    corpus_root.mkdir(parents=True, exist_ok=True)
    runner.submit("test-corpus", _config(), corpus_root)
    assert runner.wait(timeout=5.0)
    cs = load_collect_state(corpus_root / "test-corpus")
    assert cs is not None
    assert cs.state == BriefState.COMPLETED.value
    assert runner.ingest_call_count == 1
    # one retry triggered one sleep
    assert any(s > 0 for s in sleeps)


def test_fatal_error_stops_source(tmp_errorta_home: Path) -> None:
    class BadConnector(SourceConnector):
        def __init__(self, config: dict) -> None:
            pass

        def search(self, page: int) -> Iterator[SourceDoc]:
            raise FatalError("auth denied")

        def fetch(self, doc: SourceDoc) -> bytes:  # pragma: no cover
            return b""

        def canonical_id(self, doc: SourceDoc) -> str:  # pragma: no cover
            return doc.canonical_id

        def metadata(self, doc: SourceDoc) -> dict:  # pragma: no cover
            return {}

        def status(self) -> dict:
            return {"ok": False}

    CONNECTOR_REGISTRY["fake"] = BadConnector
    runner = BriefRunner(sleep=lambda s: None)
    corpus_root = tmp_errorta_home / ".errorta" / "corpora"
    corpus_root.mkdir(parents=True, exist_ok=True)
    runner.submit("test-corpus", _config(), corpus_root)
    assert runner.wait(timeout=5.0)
    cs = load_collect_state(corpus_root / "test-corpus")
    assert cs is not None
    assert cs.state == BriefState.FAILED.value
    assert cs.per_source["fake"].state == "failed"


def test_dedup_skips_already_ingested(tmp_errorta_home: Path) -> None:
    CONNECTOR_REGISTRY["fake"] = FakeConnector
    # Pre-seed dedup index.
    corpus_root = tmp_errorta_home / ".errorta" / "corpora"
    brief_dir = corpus_root / "test-corpus"
    brief_dir.mkdir(parents=True, exist_ok=True)
    (brief_dir / "dedup-index.json").write_text(
        '{"canonical_ids": ["doc-1"]}', encoding="utf-8"
    )
    FakeConnector.pages = [[_accepted_doc("doc-1"), _accepted_doc("doc-2")], []]
    runner = BriefRunner(sleep=lambda s: None)
    runner.submit("test-corpus", _config(), corpus_root)
    assert runner.wait(timeout=5.0)
    # Only doc-2 was newly ingested.
    assert runner.ingest_call_count == 1
    cs = load_collect_state(brief_dir)
    assert cs is not None
    extras = load_run_extras(brief_dir)
    assert extras.ingested_canonical_ids == ["doc-2"]
    assert load_dedup_index(brief_dir) == {"doc-1", "doc-2"}


def test_second_submit_while_active_raises(tmp_errorta_home: Path) -> None:
    """One brief run at a time across the process."""

    class SlowConnector(SourceConnector):
        gate = threading.Event()

        def __init__(self, config: dict) -> None:
            pass

        def search(self, page: int) -> Iterator[SourceDoc]:
            SlowConnector.gate.wait(timeout=2.0)
            return iter([])

        def fetch(self, doc: SourceDoc) -> bytes:  # pragma: no cover
            return b""

        def canonical_id(self, doc: SourceDoc) -> str:  # pragma: no cover
            return doc.canonical_id

        def metadata(self, doc: SourceDoc) -> dict:  # pragma: no cover
            return {}

        def status(self) -> dict:
            return {"ok": True}

    CONNECTOR_REGISTRY["fake"] = SlowConnector
    runner1 = BriefRunner(sleep=lambda s: None)
    corpus_root = tmp_errorta_home / ".errorta" / "corpora"
    corpus_root.mkdir(parents=True, exist_ok=True)
    runner1.submit("test-corpus", _config(), corpus_root)

    runner2 = BriefRunner(sleep=lambda s: None)
    with pytest.raises(RuntimeError, match="already active"):
        runner2.submit("test-corpus", _config(), corpus_root)

    # Release the first runner.
    SlowConnector.gate.set()
    assert runner1.wait(timeout=5.0)
