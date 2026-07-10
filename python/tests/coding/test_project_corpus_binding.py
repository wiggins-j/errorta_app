from pathlib import Path

import pytest

from errorta_corpus.manifest import FileEntry, upsert_entry
from errorta_council.coding.ledger import LedgerStore
from errorta_project_grounding.corpus_binding import (
    CorpusBindingError,
    ProjectCorpusBinding,
    load_binding,
    save_binding,
)


def _store(tmp_path: Path) -> LedgerStore:
    store = LedgerStore("p", root=tmp_path)
    store.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    return store


def test_default_binding_is_missing(tmp_path: Path) -> None:
    binding = load_binding(_store(tmp_path))

    assert binding.mode == "none"
    assert binding.health_state == "missing"


def test_existing_corpus_binding_reports_ready(tmp_path: Path, tmp_errorta_home: Path) -> None:
    store = _store(tmp_path)
    upsert_entry(
        "main",
        FileEntry(
            file_id="f1",
            original_path="a.md",
            copied_path="a.md",
            sha256="s",
            size_bytes=1,
            mime_ext=".md",
            status="ready",
        ),
    )

    saved = save_binding(store, ProjectCorpusBinding(project_id="p", mode="existing", corpus_id="main"))

    assert saved.health_state == "ready"
    assert load_binding(store).corpus_id == "main"


def test_existing_binding_requires_corpus_id(tmp_path: Path) -> None:
    with pytest.raises(CorpusBindingError, match="corpus_id"):
        save_binding(_store(tmp_path), ProjectCorpusBinding(project_id="p", mode="existing"))


def test_clear_binding_resets_to_none(tmp_path: Path) -> None:
    store = _store(tmp_path)
    saved = save_binding(store, ProjectCorpusBinding(project_id="p", mode="none", corpus_id="x"))

    assert saved.corpus_id is None
    assert saved.health_state == "missing"
