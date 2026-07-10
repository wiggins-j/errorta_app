"""F088 enablement Slice 2 — adapter-aware bootstrap (corpus on remote AIAR).

When a remote AIAR is configured, start_project_bootstrap ingests through the
adapter (ensure_instance -> ingest_file -> publish) instead of copying into the
local errorta_corpus store. Fail-closed: any ingest failure marks the job +
binding failed and does NOT publish.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from errorta_council.coding.ledger import LedgerStore
from errorta_project_grounding import bootstrap as bs
from errorta_project_grounding.adapter import GroundingRecordRef, ProjectGroundingError
from errorta_project_grounding.corpus_binding import load_binding


class _FakeRemote:
    def __init__(
        self,
        *,
        fail_on: str | None = None,
        ensure_fail: bool = False,
        chunks_added: int = 3,
        duplicates: int = 0,
    ):
        self.calls: list[tuple] = []
        self.fail_on = fail_on
        self.ensure_fail = ensure_fail
        self.chunks_added = chunks_added
        self.duplicates = duplicates
        self.published = False

    def ensure_instance(self, corpus_id, **kw):
        self.calls.append(("ensure", corpus_id))
        if self.ensure_fail:
            raise ProjectGroundingError("no instance")
        return {"instance": corpus_id}

    def ingest_file(self, *, corpus_id, path, metadata):
        self.calls.append(("ingest", metadata.get("source"), str(path)))
        if self.fail_on and metadata.get("source") == self.fail_on:
            raise ProjectGroundingError("ingest failed")
        return GroundingRecordRef(corpus_id=corpus_id, record_id="job-1",
                                  metadata={"chunks_added": self.chunks_added,
                                            "duplicates": self.duplicates})

    def publish(self, corpus_id):
        self.calls.append(("publish", corpus_id))
        self.published = True
        return {"instance": corpus_id, "published": True}

    def instance_health(self, corpus_id):
        return {"published": True, "chunk_count": 6}


def _repo(tmp: Path) -> Path:
    # supported extract formats (.py is NOT a supported corpus extension, so it
    # would be skipped in planning and never ingested)
    root = tmp / "src"
    root.mkdir()
    (root / "a.md").write_text("# alpha\n", encoding="utf-8")
    (root / "b.txt").write_text("bravo text\n", encoding="utf-8")
    return root


def _store(tmp: Path, pid: str) -> LedgerStore:
    s = LedgerStore(pid, root=tmp)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    return s


def _use_remote(monkeypatch, fake) -> None:
    monkeypatch.setattr(bs, "_active_remote_adapter", lambda: fake)


# --- happy path: ingest through the adapter, nothing copied locally ----------


def test_remote_bootstrap_ingests_via_adapter(tmp_path, monkeypatch) -> None:
    fake = _FakeRemote()
    _use_remote(monkeypatch, fake)
    # any local copy would be a bug on the remote path
    monkeypatch.setattr(bs, "_copy_into_corpus",
                        lambda *a, **k: pytest.fail("local copy on remote path"))
    s = _store(tmp_path, "rb1")
    job = bs.start_project_bootstrap(s, corpus_id="rb1-corpus", source_root=_repo(tmp_path))

    assert job.status == "done"
    assert job.adapter_source == "remote"
    assert job.documents_ingested == 2 and job.chunks_added == 6
    assert fake.published is True
    assert ("ensure", "rb1-corpus") in fake.calls
    # each file ingested with its corpus-relative path as source
    sources = sorted(c[1] for c in fake.calls if c[0] == "ingest")
    assert sources == ["a.md", "b.txt"]
    # no absolute path leaked in source
    binding = load_binding(s)
    assert binding.adapter_source == "remote" and binding.health_state == "ready"


# --- fail-closed: a failed ingest fails the job + binding, no publish --------


def test_remote_bootstrap_fails_closed_on_ingest_error(tmp_path, monkeypatch) -> None:
    fake = _FakeRemote(fail_on="a.md")
    _use_remote(monkeypatch, fake)
    s = _store(tmp_path, "rb2")
    job = bs.start_project_bootstrap(s, corpus_id="rb2-corpus", source_root=_repo(tmp_path))

    assert job.status == "failed"
    assert any("a.md" in e for e in job.errors)
    assert fake.published is False  # never publish a partially-failed corpus
    assert load_binding(s).health_state == "failed"


def test_remote_bootstrap_fails_closed_on_ensure_instance_error(tmp_path, monkeypatch) -> None:
    fake = _FakeRemote(ensure_fail=True)
    _use_remote(monkeypatch, fake)
    s = _store(tmp_path, "rb3")
    job = bs.start_project_bootstrap(s, corpus_id="rb3-corpus", source_root=_repo(tmp_path))

    assert job.status == "failed"
    assert any("ensure_instance" in e for e in job.errors)
    assert not any(c[0] == "ingest" for c in fake.calls)  # never tried to ingest
    assert load_binding(s).health_state == "failed"


def test_remote_bootstrap_fails_closed_when_no_files_are_eligible(tmp_path, monkeypatch) -> None:
    fake = _FakeRemote()
    _use_remote(monkeypatch, fake)
    empty = tmp_path / "empty"
    empty.mkdir()
    s = _store(tmp_path, "rb4")
    job = bs.start_project_bootstrap(s, corpus_id="rb4-corpus", source_root=empty)

    assert job.status == "failed"
    assert any("no files eligible" in e for e in job.errors)
    assert fake.calls == []
    assert load_binding(s).health_state == "failed"


def test_remote_bootstrap_fails_closed_when_ingest_stores_nothing(tmp_path, monkeypatch) -> None:
    fake = _FakeRemote(chunks_added=0, duplicates=0)
    _use_remote(monkeypatch, fake)
    s = _store(tmp_path, "rb5")
    job = bs.start_project_bootstrap(s, corpus_id="rb5-corpus", source_root=_repo(tmp_path))

    assert job.status == "failed"
    assert any("stored no chunks" in e for e in job.errors)
    assert fake.published is False
    assert load_binding(s).health_state == "failed"


# --- detection is gated on config (no env -> local path) --------------------


def test_no_remote_adapter_when_unconfigured(monkeypatch, tmp_errorta_home) -> None:
    monkeypatch.delenv("ERRORTA_AIAR_REMOTE_URL", raising=False)
    assert bs._active_remote_adapter() is None
