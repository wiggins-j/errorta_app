"""F095 — the single residency-aware corpus catalog."""
from __future__ import annotations

import pytest

from errorta_app import corpus_catalog


class _Remote:
    def __init__(self, instances):
        self._instances = instances

    def list_instances(self):
        return self._instances


def _patch_remote(monkeypatch, adapter):
    monkeypatch.setattr(
        "errorta_project_grounding.remote_adapter.active_remote_adapter",
        lambda: adapter,
    )


def test_catalog_lists_remote_instances_normalized(monkeypatch) -> None:
    _patch_remote(monkeypatch, _Remote([
        {"name": "discord-personas", "chunk_count": 3737, "published": True},
        {"name": "fresh", "chunk_count": 0, "published": True},
        {"name": "still-indexing", "chunk_count": 5, "published": False},
    ]))
    out = corpus_catalog.list_all_corpora()
    assert out["source"] == "remote"
    by = {c["name"]: c for c in out["corpora"]}
    assert by["discord-personas"]["source"] == "remote"
    assert by["discord-personas"]["ready_count"] == 3737
    assert by["discord-personas"]["file_count"] == 3737  # chunks surfaced as the unit
    assert by["discord-personas"]["status"] == "ready"
    assert by["discord-personas"]["unit"] == "chunks"
    assert by["discord-personas"]["capabilities"]["list_files"] is False
    assert by["discord-personas"]["capabilities"]["upload_files"] is False
    assert by["discord-personas"]["capabilities"]["folder_watch"] is False
    assert by["discord-personas"]["capabilities"]["refresh_preview"] is False
    assert by["discord-personas"]["capabilities"]["remote_ingest"] is False
    assert by["fresh"]["status"] == "empty"
    assert by["still-indexing"]["status"] == "indexing"
    assert by["still-indexing"]["ready_count"] == 0


def test_catalog_remote_failure_is_empty_not_5xx(monkeypatch) -> None:
    # list_instances already fail-safes to [] on transport error.
    _patch_remote(monkeypatch, _Remote([]))
    out = corpus_catalog.list_all_corpora()
    assert out == {"corpora": [], "source": "remote"}


def test_catalog_lists_local_corpora_normalized(monkeypatch, tmp_errorta_home) -> None:
    from errorta_corpus.listing import CorpusSummary
    _patch_remote(monkeypatch, None)
    monkeypatch.setattr(
        "errorta_corpus.listing.list_corpora",
        lambda: [
            CorpusSummary(name="aerospace-mini", file_count=10, ready_count=10),
            CorpusSummary(name="wip", file_count=5, ready_count=2),
            CorpusSummary(name="fresh", file_count=0, ready_count=0),
        ],
    )
    out = corpus_catalog.list_all_corpora()
    assert out["source"] == "local"
    by = {c["name"]: c for c in out["corpora"]}
    assert by["aerospace-mini"]["status"] == "ready" and by["aerospace-mini"]["source"] == "local"
    assert by["aerospace-mini"]["unit"] == "files"
    assert by["aerospace-mini"]["capabilities"]["list_files"] is True
    assert by["aerospace-mini"]["capabilities"]["upload_files"] is True
    assert by["aerospace-mini"]["capabilities"]["folder_watch"] is True
    assert by["aerospace-mini"]["capabilities"]["refresh_preview"] is True
    assert by["aerospace-mini"]["capabilities"]["remote_ingest"] is False
    assert by["wip"]["status"] == "indexing"
    assert by["fresh"]["status"] == "empty"


def test_normalize_helpers() -> None:
    assert corpus_catalog._status_local(0, 0) == "empty"
    assert corpus_catalog._status_local(3, 3) == "ready"
    assert corpus_catalog._status_local(3, 1) == "indexing"
    r = corpus_catalog._normalize_remote({"display_name": "x", "chunk_count": "7", "published": True})
    assert r["name"] == "x" and r["ready_count"] == 7 and r["status"] == "ready"
