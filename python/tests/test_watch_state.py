"""Tests for errorta_watch.state."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from errorta_watch import state as S
from errorta_watch.state import ManifestEntry, WatchState


def test_save_and_load_roundtrip(tmp_errorta_home: Path) -> None:
    st = WatchState(
        corpus="c1",
        watched_path="/tmp/watch",
        started_at="2026-06-07T00:00:00+00:00",
        deletion_policy="remove",
        type_filter=["pdf"],
        extra_ignores=["build"],
        manifest={
            "/tmp/watch/a.pdf": ManifestEntry(
                mtime=1234.0, size=42, sha256="abc", file_id="f1"
            )
        },
    )
    S.save_state(st)
    loaded = S.load_state("c1")
    assert loaded is not None
    assert loaded.corpus == "c1"
    assert loaded.watched_path == "/tmp/watch"
    assert loaded.type_filter == ["pdf"]
    assert "/tmp/watch/a.pdf" in loaded.manifest
    assert loaded.manifest["/tmp/watch/a.pdf"].file_id == "f1"


def test_load_state_missing_returns_none(tmp_errorta_home: Path) -> None:
    assert S.load_state("nonexistent") is None


def test_load_state_corrupt_returns_none(tmp_errorta_home: Path) -> None:
    p = S.state_path("badcorpus")
    p.write_text("{not json")
    assert S.load_state("badcorpus") is None


def test_load_state_missing_required_keys_returns_none(tmp_errorta_home: Path) -> None:
    p = S.state_path("partial")
    p.write_text(json.dumps({"corpus": "partial"}))  # missing watched_path/started_at
    assert S.load_state("partial") is None


def test_save_state_atomic_no_tmp_files(tmp_errorta_home: Path) -> None:
    st = WatchState(corpus="atom", watched_path="/tmp/a", started_at="now")
    S.save_state(st)
    d = S.corpus_dir("atom")
    leftovers = [p for p in d.glob(".watch-*")]
    assert leftovers == []
    assert (d / "watch.json").exists()


def test_list_persisted_corpora_returns_only_with_watch_json(tmp_errorta_home: Path) -> None:
    S.save_state(WatchState(corpus="alpha", watched_path="/a", started_at="t"))
    S.save_state(WatchState(corpus="beta", watched_path="/b", started_at="t"))
    # Make a corpus dir without watch.json — should not be listed.
    (S.corpus_dir("gamma")).mkdir(parents=True, exist_ok=True)
    listed = set(S.list_persisted_corpora())
    assert "alpha" in listed
    assert "beta" in listed
    assert "gamma" not in listed


def test_errorta_home_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    custom = tmp_path / "custom-home"
    monkeypatch.setenv("ERRORTA_HOME", str(custom))
    base = S.errorta_home()
    assert base == custom
    assert base.exists()


def test_manifest_entry_default_values() -> None:
    e = ManifestEntry(mtime=1.0, size=10)
    assert e.sha256 == ""
    assert e.xxhash == ""
    assert e.file_id == ""
    assert e.chunk_ids == []
    assert e.source_missing is False


def test_save_state_uses_os_replace(
    tmp_errorta_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Atomicity relies on os.replace — verify it's actually invoked."""
    calls: list[tuple[str, str]] = []
    real_replace = S.os.replace

    def spy(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(S.os, "replace", spy)
    S.save_state(WatchState(corpus="atomic2", watched_path="/x", started_at="t"))
    assert len(calls) == 1
    assert calls[0][1].endswith("watch.json")


def test_save_state_simulated_interrupt_preserves_original(
    tmp_errorta_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the rename step fails mid-save, the existing file must stay intact."""
    good = WatchState(corpus="durable", watched_path="/x", started_at="t1")
    S.save_state(good)
    p = S.state_path("durable")
    original_bytes = p.read_bytes()

    bad = WatchState(
        corpus="durable",
        watched_path="/x",
        started_at="t1",
        last_error="should-not-persist",
    )

    def boom(_src, _dst):
        raise OSError("simulated interrupt")

    monkeypatch.setattr(S.os, "replace", boom)
    with pytest.raises(OSError):
        S.save_state(bad)

    # Original file unchanged + no tmp leftovers.
    assert p.read_bytes() == original_bytes
    leftovers = list(p.parent.glob(".watch-*"))
    assert leftovers == []
