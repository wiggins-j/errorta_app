"""PROMOTE-E2E — end-to-end wiring from BriefRunner into the F004 corpus.

Hermetic — no network, no real ingest worker. The pipeline ``enqueue`` is
monkeypatched to a no-op recorder so the worker thread never starts.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterator

import pytest

from errorta_briefs import BriefConfig, SourceSpec
from errorta_briefs.connector import SourceConnector, SourceDoc
from errorta_briefs.lifecycle import BriefState
from errorta_briefs.runner import (
    CONNECTOR_REGISTRY,
    BriefRunner,
    load_collect_state,
    load_run_extras,
    reset_active_run,
)
from errorta_corpus import corpus_dir
from errorta_corpus.manifest import FileEntry, load_manifest, upsert_entry
from errorta_corpus import pipeline as corpus_pipeline


CORPUS = "promote-e2e-corpus"


def _doc(canonical_id: str, ext: str = ".pdf") -> SourceDoc:
    return SourceDoc(
        canonical_id=canonical_id,
        title=f"Doc {canonical_id}",
        source_url=f"https://example.test/{canonical_id}",
        publication_date="2026-01-01",
        sensitivity_class="Public",
        redistribution_allowed=True,
        license="CC-BY",
        extra={"file_ext": ext, "source_url": f"https://example.test/{canonical_id}"},
    )


class FakeConnector(SourceConnector):
    pages: list[list[SourceDoc]] = []
    payloads: dict[str, bytes] = {}

    def __init__(self, config: dict) -> None:
        self.config = config

    def search(self, page: int) -> Iterator[SourceDoc]:
        if page < len(self.pages):
            for d in self.pages[page]:
                yield d

    def fetch(self, doc: SourceDoc) -> bytes:
        return self.payloads.get(doc.canonical_id, b"payload:" + doc.canonical_id.encode())

    def canonical_id(self, doc: SourceDoc) -> str:
        return doc.canonical_id

    def metadata(self, doc: SourceDoc) -> dict:
        return {"canonical_id": doc.canonical_id}

    def status(self) -> dict:
        return {"ok": True}


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    reset_active_run()
    CONNECTOR_REGISTRY.clear()
    FakeConnector.pages = []
    FakeConnector.payloads = {}
    # Stop the corpus ingest worker from actually running.
    yield
    reset_active_run()
    CONNECTOR_REGISTRY.clear()


@pytest.fixture
def stub_enqueue(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Replace pipeline.enqueue with a recorder so no worker thread starts.

    Also rebinds the symbol the runner imported into its module so the
    in-function ``from ... import enqueue`` lookup sees the stub.
    """
    calls: list[tuple[str, str]] = []

    def _stub(corpus_name: str, file_id: str) -> None:
        calls.append((corpus_name, file_id))

    monkeypatch.setattr(corpus_pipeline, "enqueue", _stub)
    return calls


def _config() -> BriefConfig:
    return BriefConfig(
        project="Promote E2E",
        corpus=CORPUS,
        sources=[SourceSpec(name="fake", config={})],
        sensitivity="Public",
        refresh="manual",
    )


def test_two_docs_promote_into_corpus_manifest(
    tmp_errorta_home: Path, stub_enqueue: list[tuple[str, str]]
) -> None:
    CONNECTOR_REGISTRY["fake"] = FakeConnector
    p1 = b"%PDF-1.4 doc-1 contents"
    p2 = b"%PDF-1.4 doc-2 contents"
    FakeConnector.pages = [[_doc("doc-1"), _doc("doc-2")], []]
    FakeConnector.payloads = {"doc-1": p1, "doc-2": p2}

    runner = BriefRunner(sleep=lambda s: None)
    corpus_root_dir = tmp_errorta_home / ".errorta" / "corpora"
    corpus_root_dir.mkdir(parents=True, exist_ok=True)
    runner.submit("e2e", _config(), corpus_root_dir)
    assert runner.wait(timeout=5.0)

    files = load_manifest(CORPUS)
    assert len(files) == 2
    sha1 = hashlib.sha256(p1).hexdigest()
    sha2 = hashlib.sha256(p2).hexdigest()
    shas = {e.sha256 for e in files.values()}
    assert shas == {sha1, sha2}

    for e in files.values():
        assert e.status == "queued"
        assert e.mime_ext == ".pdf"
        assert Path(e.copied_path).exists()
        assert Path(e.copied_path).read_bytes() in (p1, p2)

    # enqueue called once per newly-ingested doc.
    assert len(stub_enqueue) == 2
    assert {fid for _, fid in stub_enqueue} == set(files.keys())

    # CollectState reflects 2 ingested docs.
    brief_dir = corpus_root_dir / CORPUS
    cs = load_collect_state(brief_dir)
    assert cs is not None
    assert cs.state == BriefState.COMPLETED
    assert cs.per_source["fake"].docs_ingested_to_corpus == 2
    # Backwards-compat alias.
    assert cs.per_source["fake"].docs_collected == 2

    extras = load_run_extras(brief_dir)
    assert len(extras.per_source["fake"].corpus_file_ids) == 2
    assert set(extras.per_source["fake"].corpus_file_ids) == set(files.keys())


def test_pre_existing_sha_is_skipped(
    tmp_errorta_home: Path, stub_enqueue: list[tuple[str, str]]
) -> None:
    CONNECTOR_REGISTRY["fake"] = FakeConnector
    p_existing = b"already in corpus"
    sha_existing = hashlib.sha256(p_existing).hexdigest()

    # Seed manifest with a matching entry before the run.
    corpus_dir(CORPUS)
    existing_path = (
        tmp_errorta_home / ".errorta" / "corpora" / CORPUS / "files" / "preexisting.pdf"
    )
    existing_path.parent.mkdir(parents=True, exist_ok=True)
    existing_path.write_bytes(p_existing)
    upsert_entry(
        CORPUS,
        FileEntry(
            file_id="pre-existing-id",
            original_path="seed",
            copied_path=str(existing_path),
            sha256=sha_existing,
            size_bytes=len(p_existing),
            mime_ext=".pdf",
            status="ready",
        ),
    )

    p_new = b"brand new content"
    FakeConnector.pages = [[_doc("dup-doc"), _doc("new-doc")], []]
    FakeConnector.payloads = {"dup-doc": p_existing, "new-doc": p_new}

    runner = BriefRunner(sleep=lambda s: None)
    corpus_root_dir = tmp_errorta_home / ".errorta" / "corpora"
    runner.submit("e2e", _config(), corpus_root_dir)
    assert runner.wait(timeout=5.0)

    files = load_manifest(CORPUS)
    # Pre-existing + one new entry only — duplicate fetch did not insert.
    assert len(files) == 2
    new_entries = [e for e in files.values() if e.file_id != "pre-existing-id"]
    assert len(new_entries) == 1
    assert new_entries[0].sha256 == hashlib.sha256(p_new).hexdigest()

    # enqueue only fires for the one new doc.
    assert len(stub_enqueue) == 1
    assert stub_enqueue[0][1] == new_entries[0].file_id

    brief_dir = corpus_root_dir / CORPUS
    cs = load_collect_state(brief_dir)
    assert cs is not None
    assert cs.per_source["fake"].docs_ingested_to_corpus == 1


def test_manifest_upsert_failure_unlinks_copied_file(
    tmp_errorta_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    CONNECTOR_REGISTRY["fake"] = FakeConnector
    payload = b"will-be-rolled-back"
    FakeConnector.pages = [[_doc("doomed")], []]
    FakeConnector.payloads = {"doomed": payload}

    # Patch upsert_entry to blow up — runner must unlink the copied file.
    from errorta_corpus import manifest as manifest_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated manifest upsert failure")

    monkeypatch.setattr(manifest_mod, "upsert_entry", _boom)
    # Also prevent the worker from really enqueueing (should not be reached).
    enqueue_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        corpus_pipeline,
        "enqueue",
        lambda c, f: enqueue_calls.append((c, f)),
    )

    runner = BriefRunner(sleep=lambda s: None)
    corpus_root_dir = tmp_errorta_home / ".errorta" / "corpora"
    runner.submit("e2e", _config(), corpus_root_dir)
    assert runner.wait(timeout=5.0)

    # Manifest stays empty (no rows written).
    files = load_manifest(CORPUS)
    assert files == {}

    # No orphan files left under corpus files/ dir.
    files_dir = corpus_root_dir / CORPUS / "files"
    leftovers = list(files_dir.glob("*")) if files_dir.exists() else []
    assert leftovers == [], f"orphan files left behind: {leftovers}"

    # enqueue never called because upsert raised first.
    assert enqueue_calls == []

    # Source ended in failed state per the FatalError contract.
    brief_dir = corpus_root_dir / CORPUS
    cs = load_collect_state(brief_dir)
    assert cs is not None
    assert cs.per_source["fake"].state == "failed"
    assert any(f.error_class == "FatalError" for f in cs.failures)
