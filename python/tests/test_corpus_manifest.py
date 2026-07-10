"""Tests for errorta_corpus.manifest."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

from errorta_corpus import manifest as m
from errorta_corpus.manifest import FileEntry


def _entry(file_id: str, sha: str = "abc123", original: str = "/src/a.pdf") -> FileEntry:
    return FileEntry(
        file_id=file_id,
        original_path=original,
        copied_path=f"/copied/{file_id}.pdf",
        sha256=sha,
        size_bytes=100,
        mime_ext=".pdf",
    )


def test_reserve_or_get_duplicate_concurrent_single_winner(
    tmp_errorta_home: Path, isolated_manifest_locks: None
) -> None:
    name = "corpus1"
    sha = "deadbeef"
    results: list[tuple] = []

    def attempt(i: int) -> tuple:
        entry = _entry(f"f{i}", sha=sha)
        return m.reserve_or_get_duplicate(name, sha, entry, overwrite=False)

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(attempt, i) for i in range(20)]
        for fut in as_completed(futures):
            results.append(fut.result())

    inserted = [r for r in results if r[0] is not None]
    duplicates = [r for r in results if r[0] is None]
    assert len(inserted) == 1
    assert len(duplicates) == 19
    files = m.load_manifest(name)
    assert len(files) == 1


def test_reserve_or_get_duplicate_overwrite_replaces(
    tmp_errorta_home: Path, isolated_manifest_locks: None
) -> None:
    name = "corpus2"
    sha = "shasame"
    first = _entry("f-old", sha=sha)
    m.reserve_or_get_duplicate(name, sha, first, overwrite=False)

    second = _entry("f-new", sha=sha, original="/src/new.pdf")
    inserted, existing = m.reserve_or_get_duplicate(name, sha, second, overwrite=True)

    assert inserted is second
    assert existing is not None and existing.file_id == "f-old"
    files = m.load_manifest(name)
    assert "f-old" not in files
    assert "f-new" in files


def test_reserve_or_get_duplicate_no_overwrite_returns_existing(
    tmp_errorta_home: Path, isolated_manifest_locks: None
) -> None:
    name = "corpus2b"
    sha = "abcabc"
    m.reserve_or_get_duplicate(name, sha, _entry("f1", sha=sha), overwrite=False)
    inserted, existing = m.reserve_or_get_duplicate(
        name, sha, _entry("f2", sha=sha), overwrite=False
    )
    assert inserted is None
    assert existing is not None and existing.file_id == "f1"


def test_upsert_entry_round_trips_through_disk(
    tmp_errorta_home: Path, isolated_manifest_locks: None
) -> None:
    name = "corpus3"
    entry = _entry("f1", sha="s1")
    entry.chunk_ids = ["c1", "c2"]
    entry.token_count = 42
    m.upsert_entry(name, entry)

    raw = json.loads(m.manifest_path(name).read_text())
    assert raw["name"] == name
    assert raw["files"]["f1"]["sha256"] == "s1"

    loaded = m.load_manifest(name)
    assert "f1" in loaded
    assert loaded["f1"].chunk_ids == ["c1", "c2"]
    assert loaded["f1"].token_count == 42


def test_update_status_modifies_single_entry_only(
    tmp_errorta_home: Path, isolated_manifest_locks: None
) -> None:
    name = "corpus4"
    m.upsert_entry(name, _entry("f1", sha="s1"))
    m.upsert_entry(name, _entry("f2", sha="s2"))
    m.upsert_entry(name, _entry("f3", sha="s3"))

    updated = m.update_status(
        name,
        "f2",
        status="ready",
        chunk_count=7,
        progress=1.0,
        ingested_at="2026-06-07T00:00:00Z",
    )
    assert updated is not None
    assert updated.status == "ready"

    files = m.load_manifest(name)
    assert files["f1"].status == "queued"
    assert files["f2"].status == "ready"
    assert files["f2"].chunk_count == 7
    assert files["f2"].progress == 1.0
    assert files["f3"].status == "queued"


def test_update_status_unknown_file_returns_none(
    tmp_errorta_home: Path, isolated_manifest_locks: None
) -> None:
    name = "corpus4b"
    m.upsert_entry(name, _entry("f1"))
    assert m.update_status(name, "missing", status="ready") is None


def test_remove_entry_deletes_target_and_keeps_valid_json(
    tmp_errorta_home: Path, isolated_manifest_locks: None
) -> None:
    name = "corpus5"
    m.upsert_entry(name, _entry("f1", sha="s1"))
    m.upsert_entry(name, _entry("f2", sha="s2"))

    removed = m.remove_entry(name, "f1")
    assert removed is not None and removed.file_id == "f1"

    raw = json.loads(m.manifest_path(name).read_text())
    assert "f1" not in raw["files"]
    assert "f2" in raw["files"]

    # Removing missing returns None but keeps file valid.
    assert m.remove_entry(name, "ghost") is None
    raw2 = json.loads(m.manifest_path(name).read_text())
    assert list(raw2["files"].keys()) == ["f2"]


def test_lock_for_returns_same_object(
    tmp_errorta_home: Path, isolated_manifest_locks: None
) -> None:
    a = m._lock_for("corpusA")
    b = m._lock_for("corpusA")
    c = m._lock_for("corpusB")
    assert a is b
    assert a is not c


def test_find_by_sha256(tmp_errorta_home: Path, isolated_manifest_locks: None) -> None:
    name = "corpus6"
    m.upsert_entry(name, _entry("f1", sha="aaa"))
    m.upsert_entry(name, _entry("f2", sha="bbb"))
    hit = m.find_by_sha256(name, "bbb")
    assert hit is not None and hit.file_id == "f2"
    assert m.find_by_sha256(name, "zzz") is None


def test_load_manifest_missing_file_returns_empty(
    tmp_errorta_home: Path, isolated_manifest_locks: None
) -> None:
    assert m.load_manifest("never-written") == {}


def test_load_manifest_corrupt_json_returns_empty(
    tmp_errorta_home: Path, isolated_manifest_locks: None
) -> None:
    name = "corpus7"
    # Force directory creation, then write garbage.
    p = m.manifest_path(name)
    p.write_text("{not json")
    assert m.load_manifest(name) == {}
