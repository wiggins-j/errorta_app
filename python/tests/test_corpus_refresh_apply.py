"""Tests for errorta_corpus.refresh.apply_diff and the refresh-apply route."""
from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from errorta_corpus import corpus_dir
from errorta_corpus.manifest import FileEntry, load_manifest, save_manifest
from errorta_corpus.refresh import (
    RefreshDiff,
    apply_diff,
    apply_result_to_dict,
    diff_from_dict,
    diff_to_dict,
)
from errorta_corpus import refresh as refresh_mod
from errorta_corpus import pipeline as pipeline_mod


def _hash(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@pytest.fixture
def stub_pipeline(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace pipeline.enqueue/evict_chunks with capturing stubs so apply
    is hermetic (no background thread, no AIAR import)."""
    calls = {"enqueue": [], "evict": []}

    def fake_enqueue(corpus: str, file_id: str) -> None:
        calls["enqueue"].append((corpus, file_id))

    def fake_evict(corpus: str, file_id: str, chunk_ids: list[str]) -> None:
        calls["evict"].append((corpus, file_id, list(chunk_ids)))

    monkeypatch.setattr(pipeline_mod, "enqueue", fake_enqueue)
    monkeypatch.setattr(pipeline_mod, "evict_chunks", fake_evict)
    return calls


@pytest.fixture(autouse=True)
def clear_apply_locks() -> Iterator[None]:
    refresh_mod._APPLY_LOCKS.clear()
    yield
    refresh_mod._APPLY_LOCKS.clear()


def _entry(disk_path: Path, *, file_id: str, sha: str) -> FileEntry:
    st = disk_path.stat()
    return FileEntry(
        file_id=file_id,
        original_path=str(disk_path),
        copied_path=str(disk_path),
        sha256=sha,
        size_bytes=st.st_size,
        mime_ext=disk_path.suffix.lower(),
        status="ready",
        chunk_ids=[f"{file_id}:0"],
        chunk_count=1,
    )


# ---------------------------------------------------------------------------
# apply_diff
# ---------------------------------------------------------------------------


def test_apply_diff_added_copies_and_enqueues(
    tmp_errorta_home: Path, isolated_manifest_locks: None, stub_pipeline: dict
) -> None:
    src_dir = tmp_errorta_home / "src"
    src_dir.mkdir()
    f = src_dir / "new.txt"
    f.write_bytes(b"new content")
    added_entry = FileEntry(
        file_id="",
        original_path=str(f),
        copied_path="",
        sha256=_hash(b"new content"),
        size_bytes=len(b"new content"),
        mime_ext=".txt",
        status="candidate",
    )
    diff = RefreshDiff(added=[added_entry])

    save_manifest("corpus1", {})
    result = apply_diff("corpus1", diff)

    assert len(result.ingested) == 1
    assert result.errors == []
    files = load_manifest("corpus1")
    assert len(files) == 1
    entry = next(iter(files.values()))
    assert entry.status == "queued"
    assert Path(entry.copied_path).exists()
    assert Path(entry.copied_path).read_bytes() == b"new content"
    assert stub_pipeline["enqueue"] == [("corpus1", entry.file_id)]


def test_apply_diff_removed_soft_deletes_entry(
    tmp_errorta_home: Path, isolated_manifest_locks: None, stub_pipeline: dict
) -> None:
    src = tmp_errorta_home / "x.txt"
    src.write_bytes(b"hello")
    entry = _entry(src, file_id="fid1", sha=_hash(b"hello"))
    save_manifest("corpus1", {"fid1": entry})

    diff = RefreshDiff(removed=[entry])
    result = apply_diff("corpus1", diff)

    assert result.removed == ["fid1"]
    files = load_manifest("corpus1")
    # Soft-delete: entry remains for audit.
    assert "fid1" in files
    assert files["fid1"].status == "removed"
    assert files["fid1"].copied_path == ""
    assert stub_pipeline["evict"] == [("corpus1", "fid1", ["fid1:0"])]


def test_apply_diff_updated_evicts_copies_enqueues(
    tmp_errorta_home: Path, isolated_manifest_locks: None, stub_pipeline: dict
) -> None:
    src = tmp_errorta_home / "x.txt"
    src.write_bytes(b"v2 content")
    old = _entry(src, file_id="fid1", sha="oldsha")
    new = _entry(src, file_id="fid1", sha=_hash(b"v2 content"))
    save_manifest("corpus1", {"fid1": old})

    diff = RefreshDiff(updated=[(old, new)])
    result = apply_diff("corpus1", diff)

    assert result.updated == ["fid1"]
    files = load_manifest("corpus1")
    assert files["fid1"].sha256 == _hash(b"v2 content")
    assert files["fid1"].status == "queued"
    assert Path(files["fid1"].copied_path).exists()
    assert stub_pipeline["evict"] == [("corpus1", "fid1", ["fid1:0"])]
    assert stub_pipeline["enqueue"] == [("corpus1", "fid1")]


def test_apply_diff_mixed_batch_with_per_file_errors(
    tmp_errorta_home: Path, isolated_manifest_locks: None, stub_pipeline: dict
) -> None:
    src_dir = tmp_errorta_home / "src"
    src_dir.mkdir()
    good = src_dir / "good.txt"
    good.write_bytes(b"good")
    missing_path = src_dir / "missing.txt"  # never created

    good_added = FileEntry(
        file_id="",
        original_path=str(good),
        copied_path="",
        sha256=_hash(b"good"),
        size_bytes=4,
        mime_ext=".txt",
        status="candidate",
    )
    bad_added = FileEntry(
        file_id="",
        original_path=str(missing_path),
        copied_path="",
        sha256="bogus",
        size_bytes=0,
        mime_ext=".txt",
        status="candidate",
    )
    diff = RefreshDiff(added=[good_added, bad_added])

    save_manifest("corpus1", {})
    result = apply_diff("corpus1", diff)

    assert len(result.ingested) == 1
    assert len(result.errors) == 1
    err_path, err_msg = result.errors[0]
    assert "missing.txt" in err_path
    assert "missing" in err_msg.lower()


def test_apply_diff_per_corpus_lock_serializes_same_corpus(
    tmp_errorta_home: Path, isolated_manifest_locks: None, stub_pipeline: dict
) -> None:
    save_manifest("corpus1", {})
    lock = refresh_mod._apply_lock_for("corpus1")
    # Acquire the lock in the test thread; the apply_diff call from another
    # thread must block until we release it.
    acquired_at: list[float] = []
    done_at: list[float] = []

    def runner() -> None:
        apply_diff("corpus1", RefreshDiff())
        done_at.append(time.monotonic())

    with lock:
        t = threading.Thread(target=runner, daemon=True)
        t.start()
        time.sleep(0.1)
        # Thread is blocked because we own the lock.
        assert t.is_alive()
        acquired_at.append(time.monotonic())
    t.join(timeout=2.0)
    assert not t.is_alive()
    # Verify it ran AFTER we released the lock.
    assert done_at and done_at[0] >= acquired_at[0]


def test_apply_diff_different_corpora_use_distinct_locks(
    tmp_errorta_home: Path, isolated_manifest_locks: None, stub_pipeline: dict
) -> None:
    save_manifest("corpus1", {})
    save_manifest("corpus2", {})
    lock1 = refresh_mod._apply_lock_for("corpus1")
    lock2 = refresh_mod._apply_lock_for("corpus2")
    assert lock1 is not lock2

    done = threading.Event()

    def runner() -> None:
        apply_diff("corpus2", RefreshDiff())
        done.set()

    with lock1:
        t = threading.Thread(target=runner, daemon=True)
        t.start()
        # corpus2 apply should complete despite corpus1 lock being held.
        assert done.wait(timeout=2.0)
    t.join(timeout=1.0)


def test_apply_result_to_dict_shape() -> None:
    from errorta_corpus.refresh import ApplyResult

    r = ApplyResult(
        ingested=["a"], removed=["b"], updated=["c"], errors=[("p", "m")]
    )
    out = apply_result_to_dict(r)
    assert out == {
        "ingested": ["a"],
        "removed": ["b"],
        "updated": ["c"],
        "errors": [{"path": "p", "message": "m"}],
    }


def test_diff_from_dict_roundtrips() -> None:
    e = FileEntry(
        file_id="x",
        original_path="/p",
        copied_path="/c",
        sha256="s",
        size_bytes=1,
        mime_ext=".txt",
    )
    diff = RefreshDiff(added=[e], removed=[e], updated=[(e, e)], snapshot_at="t")
    d = diff_to_dict(diff)
    back = diff_from_dict(d)
    assert len(back.added) == 1
    assert len(back.removed) == 1
    assert len(back.updated) == 1
    assert back.snapshot_at == "t"


def test_file_entry_status_accepts_removed() -> None:
    e = FileEntry(
        file_id="x",
        original_path="/p",
        copied_path="",
        sha256="s",
        size_bytes=0,
        mime_ext=".txt",
        status="removed",
    )
    assert e.status == "removed"


# ---------------------------------------------------------------------------
# HTTP endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def client(
    tmp_errorta_home: Path,
    isolated_manifest_locks: None,
    stub_pipeline: dict,
) -> TestClient:
    from errorta_app.server import app

    return TestClient(app)


def test_refresh_apply_404_for_unknown_corpus(client: TestClient) -> None:
    r = client.post("/corpus/nope/refresh-apply")
    assert r.status_code == 404


def test_refresh_apply_400_for_malformed_body(
    tmp_errorta_home: Path, client: TestClient
) -> None:
    save_manifest("corpus1", {})
    r = client.post(
        "/corpus/corpus1/refresh-apply",
        content=b"not-json-{[",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_refresh_apply_200_with_empty_body_recomputes_diff(
    tmp_errorta_home: Path, client: TestClient
) -> None:
    save_manifest("corpus1", {})
    r = client.post("/corpus/corpus1/refresh-apply")
    assert r.status_code == 200
    body = r.json()
    assert body["corpus"] == "corpus1"
    assert "ingested" in body
    assert "removed" in body
    assert "updated" in body
    assert "errors" in body


def test_refresh_apply_200_with_explicit_diff_body(
    tmp_errorta_home: Path, client: TestClient
) -> None:
    src_dir = tmp_errorta_home / "src"
    src_dir.mkdir()
    f = src_dir / "doc.txt"
    f.write_bytes(b"data")
    save_manifest("corpus1", {})

    diff_payload = {
        "added": [
            {
                "file_id": "",
                "original_path": str(f),
                "copied_path": "",
                "sha256": _hash(b"data"),
                "size_bytes": 4,
                "mime_ext": ".txt",
                "status": "candidate",
                "error": None,
                "chunk_count": 0,
                "chunk_ids": [],
                "token_count": 0,
                "ingested_at": None,
                "progress": 0.0,
            }
        ],
        "removed": [],
        "updated": [],
        "snapshot_at": "t",
        "partial": False,
    }
    r = client.post("/corpus/corpus1/refresh-apply", json=diff_payload)
    assert r.status_code == 200
    body = r.json()
    assert len(body["ingested"]) == 1
