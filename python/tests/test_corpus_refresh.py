"""Tests for errorta_corpus.refresh (F015-BACKEND)."""
from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from errorta_corpus import corpus_dir
from errorta_corpus.manifest import FileEntry, save_manifest
from errorta_corpus import refresh as refresh_mod
from errorta_corpus.refresh import compute_diff


def _make_disk_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _entry_for(disk_path: Path, *, file_id: str, sha: str, ingested_at: str) -> FileEntry:
    st = disk_path.stat()
    return FileEntry(
        file_id=file_id,
        original_path=str(disk_path),
        copied_path=f"/copied/{file_id}",
        sha256=sha,
        size_bytes=st.st_size,
        mime_ext=disk_path.suffix.lower(),
        status="ready",
        ingested_at=ingested_at,
    )


def _far_future_iso() -> str:
    return "2999-01-01T00:00:00+00:00"


def _hash(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()


# ---------------------------------------------------------------------------
# compute_diff
# ---------------------------------------------------------------------------


def test_compute_diff_baseline_no_diff_when_no_snapshots(
    tmp_errorta_home: Path, isolated_manifest_locks: None
) -> None:
    src = tmp_errorta_home / "src"
    f1 = src / "a.txt"
    _make_disk_file(f1, b"hello")
    entry = _entry_for(f1, file_id="f1", sha=_hash(b"hello"), ingested_at=_far_future_iso())
    save_manifest("corpus1", {"f1": entry})

    diff = compute_diff("corpus1")
    assert diff.added == []
    assert diff.removed == []
    assert diff.updated == []
    # snapshot written
    snaps = list((corpus_dir("corpus1") / "refresh-snapshots").iterdir())
    assert len(snaps) == 1
    assert snaps[0].suffix == ".json"


def test_compute_diff_detects_added(
    tmp_errorta_home: Path, isolated_manifest_locks: None
) -> None:
    src = tmp_errorta_home / "src"
    f1 = src / "a.txt"
    _make_disk_file(f1, b"hello")
    entry = _entry_for(f1, file_id="f1", sha=_hash(b"hello"), ingested_at=_far_future_iso())
    save_manifest("corpus1", {"f1": entry})

    # Establish baseline snapshot.
    compute_diff("corpus1")

    # Now add a new file in the same dir.
    f2 = src / "b.txt"
    _make_disk_file(f2, b"world")

    diff = compute_diff("corpus1")
    assert len(diff.added) == 1
    assert diff.added[0].original_path == str(f2)
    assert diff.added[0].sha256 == _hash(b"world")
    assert diff.removed == []
    assert diff.updated == []


def test_compute_diff_detects_removed(
    tmp_errorta_home: Path, isolated_manifest_locks: None
) -> None:
    src = tmp_errorta_home / "src"
    f1 = src / "a.txt"
    _make_disk_file(f1, b"hello")
    entry = _entry_for(f1, file_id="f1", sha=_hash(b"hello"), ingested_at=_far_future_iso())
    save_manifest("corpus1", {"f1": entry})
    compute_diff("corpus1")

    f1.unlink()

    diff = compute_diff("corpus1")
    assert len(diff.removed) == 1
    assert diff.removed[0].file_id == "f1"
    assert diff.added == []
    assert diff.updated == []


def test_compute_diff_detects_updated(
    tmp_errorta_home: Path, isolated_manifest_locks: None
) -> None:
    src = tmp_errorta_home / "src"
    f1 = src / "a.txt"
    _make_disk_file(f1, b"hello")
    entry = _entry_for(
        f1, file_id="f1", sha=_hash(b"hello"), ingested_at="1970-01-01T00:00:00+00:00"
    )
    save_manifest("corpus1", {"f1": entry})
    compute_diff("corpus1")

    # Change content (and therefore size).
    _make_disk_file(f1, b"hello world!!")

    diff = compute_diff("corpus1")
    assert diff.added == []
    assert diff.removed == []
    assert len(diff.updated) == 1
    old, new = diff.updated[0]
    assert old.sha256 == _hash(b"hello")
    assert new.sha256 == _hash(b"hello world!!")
    assert new.size_bytes == len(b"hello world!!")


def test_compute_diff_skips_rehash_when_stable(
    tmp_errorta_home: Path,
    isolated_manifest_locks: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = tmp_errorta_home / "src"
    f1 = src / "a.txt"
    _make_disk_file(f1, b"hello")
    # ingested_at strictly AFTER the file's mtime so mtime_changed is False.
    entry = _entry_for(f1, file_id="f1", sha=_hash(b"hello"), ingested_at=_far_future_iso())
    save_manifest("corpus1", {"f1": entry})

    # First call establishes baseline; this is when added-walk may hash new
    # files. We seed the snapshot first then mock for the second call.
    compute_diff("corpus1")

    sha_mock = MagicMock(return_value="should-not-be-called")
    monkeypatch.setattr(refresh_mod, "sha256_file", sha_mock)

    diff = compute_diff("corpus1")
    assert diff.added == []
    assert diff.removed == []
    assert diff.updated == []
    assert sha_mock.call_count == 0


def test_compute_diff_writes_snapshot_file(
    tmp_errorta_home: Path, isolated_manifest_locks: None
) -> None:
    save_manifest("corpus1", {})
    diff = compute_diff("corpus1")
    snap = corpus_dir("corpus1") / "refresh-snapshots" / f"{diff.snapshot_at}.json"
    assert snap.exists()
    payload = json.loads(snap.read_text())
    assert payload["name"] == "corpus1"
    assert payload["snapshot_at"] == diff.snapshot_at
    assert "files" in payload


def test_compute_diff_bogus_since_falls_back_to_newest(
    tmp_errorta_home: Path, isolated_manifest_locks: None
) -> None:
    src = tmp_errorta_home / "src"
    f1 = src / "a.txt"
    _make_disk_file(f1, b"hello")
    entry = _entry_for(f1, file_id="f1", sha=_hash(b"hello"), ingested_at=_far_future_iso())
    save_manifest("corpus1", {"f1": entry})

    # Create two snapshots manually.
    snaps_dir = corpus_dir("corpus1") / "refresh-snapshots"
    snaps_dir.mkdir(parents=True, exist_ok=True)
    older = snaps_dir / "2024-01-01T00:00:00.000000Z.json"
    newer = snaps_dir / "2025-01-01T00:00:00.000000Z.json"
    older.write_text(json.dumps({"name": "corpus1", "snapshot_at": "old", "files": {}}))
    newer.write_text(
        json.dumps(
            {
                "name": "corpus1",
                "snapshot_at": "new",
                "files": {
                    "f1": {
                        "file_id": "f1",
                        "original_path": str(f1),
                        "copied_path": "/copied/f1",
                        "sha256": _hash(b"hello"),
                        "size_bytes": len(b"hello"),
                        "mime_ext": ".txt",
                        "status": "ready",
                        "error": None,
                        "chunk_count": 0,
                        "chunk_ids": [],
                        "token_count": 0,
                        "ingested_at": _far_future_iso(),
                        "progress": 0.0,
                    }
                },
            }
        )
    )

    # Bogus since → should pick the newer snapshot (which has f1) so no diff.
    diff = compute_diff("corpus1", last_snapshot_at="nonexistent-id")
    assert diff.added == []
    assert diff.removed == []
    assert diff.updated == []


def test_compute_diff_named_snapshot_used_when_present(
    tmp_errorta_home: Path, isolated_manifest_locks: None
) -> None:
    src = tmp_errorta_home / "src"
    f1 = src / "a.txt"
    _make_disk_file(f1, b"hello")
    entry = _entry_for(f1, file_id="f1", sha=_hash(b"hello"), ingested_at=_far_future_iso())
    save_manifest("corpus1", {"f1": entry})

    snaps_dir = corpus_dir("corpus1") / "refresh-snapshots"
    snaps_dir.mkdir(parents=True, exist_ok=True)
    named_id = "named-snapshot-001"
    # This snapshot has a DIFFERENT file ("ghost.txt") that does not exist
    # on disk → should be reported as removed when we pick this snapshot.
    (snaps_dir / f"{named_id}.json").write_text(
        json.dumps(
            {
                "name": "corpus1",
                "snapshot_at": named_id,
                "files": {
                    "ghost": {
                        "file_id": "ghost",
                        "original_path": str(src / "ghost.txt"),
                        "copied_path": "/copied/ghost",
                        "sha256": "deadbeef",
                        "size_bytes": 0,
                        "mime_ext": ".txt",
                        "status": "ready",
                        "error": None,
                        "chunk_count": 0,
                        "chunk_ids": [],
                        "token_count": 0,
                        "ingested_at": _far_future_iso(),
                        "progress": 0.0,
                    }
                },
            }
        )
    )

    diff = compute_diff("corpus1", last_snapshot_at=named_id)
    assert len(diff.removed) == 1
    assert diff.removed[0].file_id == "ghost"


# ---------------------------------------------------------------------------
# HTTP endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_errorta_home: Path, isolated_manifest_locks: None) -> TestClient:
    from errorta_app.server import app

    return TestClient(app)


def test_refresh_preview_404_for_unknown_corpus(client: TestClient) -> None:
    r = client.get("/corpus/nope/refresh-preview")
    assert r.status_code == 404


def test_refresh_preview_200_for_known_corpus(
    tmp_errorta_home: Path, isolated_manifest_locks: None, client: TestClient
) -> None:
    src = tmp_errorta_home / "src"
    f1 = src / "a.txt"
    _make_disk_file(f1, b"hello")
    entry = _entry_for(f1, file_id="f1", sha=_hash(b"hello"), ingested_at=_far_future_iso())
    save_manifest("corpus1", {"f1": entry})

    r = client.get("/corpus/corpus1/refresh-preview")
    assert r.status_code == 200
    body = r.json()
    assert body["corpus"] == "corpus1"
    assert "added" in body and "removed" in body and "updated" in body
    assert "snapshot_at" in body
    assert body["partial"] is False


def test_refresh_preview_module_docstring_notes_preview_only() -> None:
    doc = (refresh_mod.__doc__ or "").lower()
    assert "preview" in doc
    assert "apply" in doc
