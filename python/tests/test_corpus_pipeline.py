"""Tests for errorta_corpus.pipeline."""
from __future__ import annotations

import queue
import sys
import time
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def pipeline_module(tmp_errorta_home: Path, isolated_manifest_locks):
    """Import the pipeline module fresh and clear its in-process state."""
    from errorta_corpus import pipeline

    # Reset the in-process event bus between tests.
    with pipeline._sub_lock:
        pipeline._subscribers.clear()
    return pipeline


@pytest.fixture
def pipeline_worker_cleanup(pipeline_module):
    """Drain the worker queue after a test (the daemon thread can't be killed)."""
    yield
    # Drain any pending jobs so a later test isn't surprised by leftovers.
    try:
        while True:
            pipeline_module._worker_q.get_nowait()
    except queue.Empty:
        pass


def _seed_entry(corpus_name: str, file_id: str, ext: str = ".txt") -> Path:
    """Create a corpus, drop a real file on disk, and write a manifest entry."""
    from errorta_corpus import corpus_dir
    from errorta_corpus.manifest import FileEntry, upsert_entry

    d = corpus_dir(corpus_name)
    copied = d / "files" / f"sample{ext}"
    copied.write_text("hello world from errorta tests")
    entry = FileEntry(
        file_id=file_id,
        original_path=str(copied),
        copied_path=str(copied),
        sha256="deadbeef",
        size_bytes=copied.stat().st_size,
        mime_ext=ext,
        status="queued",
    )
    upsert_entry(corpus_name, entry)
    return copied


# ---- event bus ----------------------------------------------------------
def test_subscribe_and_publish_delivers_event(pipeline_module) -> None:
    q1 = pipeline_module.subscribe()
    q2 = pipeline_module.subscribe()
    pipeline_module.publish({"type": "test", "n": 1})

    payload1 = q1.get(timeout=1.0)
    payload2 = q2.get(timeout=1.0)
    assert '"type": "test"' in payload1
    assert payload1 == payload2


def test_unsubscribe_removes_subscriber(pipeline_module) -> None:
    q = pipeline_module.subscribe()
    assert q in pipeline_module._subscribers
    pipeline_module.unsubscribe(q)
    assert q not in pipeline_module._subscribers

    # A second unsubscribe of the same queue is a no-op (no exception).
    pipeline_module.unsubscribe(q)


def test_publish_does_not_block_when_subscriber_full(pipeline_module) -> None:
    """A slow subscriber must not block other subscribers or the publisher."""
    slow: queue.Queue[str] = queue.Queue(maxsize=1)
    fast = pipeline_module.subscribe()
    with pipeline_module._sub_lock:
        pipeline_module._subscribers.append(slow)

    # Fill the slow queue so the next publish would block if put_nowait wasn't used.
    slow.put_nowait("preload")

    start = time.monotonic()
    pipeline_module.publish({"type": "x"})
    elapsed = time.monotonic() - start
    assert elapsed < 0.5, "publish should be non-blocking"

    # The fast subscriber still got the message.
    assert fast.get(timeout=1.0)


# ---- id + path helpers --------------------------------------------------
def test_new_file_id_returns_unique_ids(pipeline_module) -> None:
    ids = {pipeline_module.new_file_id() for _ in range(50)}
    assert len(ids) == 50
    for fid in ids:
        assert isinstance(fid, str) and len(fid) == 32


def test_copied_path_for_avoids_collisions(pipeline_module) -> None:
    corpus_name = "test-corpus"
    first = pipeline_module.copied_path_for(corpus_name, "doc.txt")
    assert first.name == "doc.txt"
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_text("a")

    second = pipeline_module.copied_path_for(corpus_name, "doc.txt")
    assert second.name == "doc-1.txt"
    second.write_text("b")

    third = pipeline_module.copied_path_for(corpus_name, "doc.txt")
    assert third.name == "doc-2.txt"


def test_copied_path_for_no_extension(pipeline_module) -> None:
    corpus_name = "test-corpus"
    path = pipeline_module.copied_path_for(corpus_name, "README")
    assert path.name == "README"


# ---- enqueue + worker ---------------------------------------------------
def test_enqueue_runs_through_worker(
    pipeline_module, pipeline_worker_cleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: enqueue a file with a stubbed extractor and observe a 'ready' event."""
    corpus_name = "wcorpus"
    file_id = pipeline_module.new_file_id()
    _seed_entry(corpus_name, file_id)

    def fake_extractor(path: Path) -> list[dict[str, Any]]:
        return [
            {"text": "hello world", "meta": {"page": 1}},
            {"text": "second chunk here", "meta": {"page": 2}},
        ]

    # Patch the symbols used inside pipeline.py (imported at module load).
    monkeypatch.setattr(pipeline_module, "get_extractor", lambda ext: fake_extractor)
    monkeypatch.setattr(pipeline_module, "_ingest_chunks_into_aiar", lambda *a: None)

    q = pipeline_module.subscribe()
    pipeline_module.enqueue(corpus_name, file_id)

    # Collect events until we see "ready" or time out.
    statuses: list[str] = []
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            payload = q.get(timeout=1.0)
        except queue.Empty:
            continue
        import json as _json

        evt = _json.loads(payload)
        if evt.get("file_id") == file_id:
            statuses.append(evt.get("status", ""))
            if evt.get("status") == "ready":
                break

    assert "ready" in statuses, f"never saw 'ready' status; got {statuses!r}"

    from errorta_corpus.manifest import load_manifest

    entry = load_manifest(corpus_name)[file_id]
    assert entry.status == "ready"
    assert entry.chunk_count == 2
    assert entry.token_count > 0
    assert len(entry.chunk_ids) == 2


def test_worker_marks_failed_on_extract_error(
    pipeline_module, pipeline_worker_cleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    from errorta_extract import ExtractError

    corpus_name = "ecorpus"
    file_id = pipeline_module.new_file_id()
    _seed_entry(corpus_name, file_id)

    def bad_extractor(path: Path) -> list[dict[str, Any]]:
        raise ExtractError("unreadable file")

    monkeypatch.setattr(pipeline_module, "get_extractor", lambda ext: bad_extractor)

    pipeline_module.enqueue(corpus_name, file_id)

    from errorta_corpus.manifest import load_manifest

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        entry = load_manifest(corpus_name).get(file_id)
        if entry is not None and entry.status == "failed":
            break
        time.sleep(0.05)

    entry = load_manifest(corpus_name)[file_id]
    assert entry.status == "failed"
    assert entry.error and "unreadable" in entry.error


def test_aiar_store_bridge_ingests_chunks(
    pipeline_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict[str, Any] = {}

    class FakeChunk:
        def __init__(
            self,
            *,
            source: str,
            title: str,
            chunk_index: int,
            text: str,
            category: str,
            metadata: dict[str, Any],
        ) -> None:
            self.source = source
            self.title = title
            self.chunk_index = chunk_index
            self.text = text
            self.category = category
            self.metadata = metadata

    class FakeStore:
        @staticmethod
        def create_instance(name: str, *, display_name: str | None = None) -> str:
            calls["create_instance"] = (name, display_name)
            return name

        @staticmethod
        def add(chunks: list[FakeChunk], *, instance: str) -> int:
            calls["add"] = (chunks, instance)
            return len(chunks)

        @staticmethod
        def publish_instance(name: str) -> None:
            calls["publish_instance"] = name

    class FakeIngest:
        Chunk = FakeChunk

    monkeypatch.setitem(sys.modules, "aiar.rag.store", FakeStore)
    monkeypatch.setitem(sys.modules, "aiar.rag.ingest", FakeIngest)

    pipeline_module._ingest_chunks_via_aiar_store(
        "welcome",
        "file-1",
        ["file-1:0", "file-1:1"],
        [
            {"text": "first chunk", "meta": {"page": 1, "title": "Intro"}},
            {"text": "second chunk", "meta": {"page": 2}},
        ],
    )

    assert calls["create_instance"] == ("welcome", "welcome")
    stored, instance = calls["add"]
    assert instance == "welcome"
    assert [chunk.text for chunk in stored] == ["first chunk", "second chunk"]
    assert [chunk.chunk_index for chunk in stored] == [0, 1]
    assert stored[0].source == "welcome/file-1"
    assert stored[0].title == "Intro"
    assert stored[0].category == "welcome"
    assert stored[0].metadata["file_id"] == "file-1"
    assert stored[0].metadata["chunk_id"] == "file-1:0"
    assert calls["publish_instance"] == "welcome"


def test_evict_chunks_empty_list_is_noop(pipeline_module) -> None:
    # Should not raise even when AIAR is not installed.
    pipeline_module.evict_chunks("any", "any", [])


def test_evict_chunks_missing_aiar_is_swallowed(pipeline_module) -> None:
    # With real chunk IDs but no AIAR, the function should silently return.
    pipeline_module.evict_chunks("any", "fid", ["fid:0", "fid:1"])
