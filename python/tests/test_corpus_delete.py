"""F114 — delete a whole corpus (function + DELETE /corpus/{name})."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_corpus import InvalidCorpusName, corpus_dir, corpus_root, delete_corpus


def _seed_corpus(name: str) -> Path:
    """Create a corpus dir with a manifest + a copied file on disk."""
    d = corpus_dir(name)
    (d / "manifest.json").write_text('{"name": "%s", "files": {}}' % name)
    (d / "files" / "a.txt").write_text("hello")
    return d


def _client() -> TestClient:
    from errorta_app.routes import corpus as corpus_routes

    app = FastAPI()
    app.include_router(corpus_routes.router)
    return TestClient(app)


# ---- delete_corpus() ----------------------------------------------------


def test_delete_corpus_removes_dir_and_manifest(tmp_errorta_home: Path) -> None:
    d = _seed_corpus("alpha")
    assert d.is_dir()

    assert delete_corpus("alpha") is True
    assert not d.exists()
    assert not (corpus_root() / "alpha").exists()


def test_delete_corpus_unknown_is_false(tmp_errorta_home: Path) -> None:
    assert not (corpus_root() / "ghost").exists()
    assert delete_corpus("ghost") is False


def test_delete_corpus_leaves_other_corpora_intact(tmp_errorta_home: Path) -> None:
    _seed_corpus("alpha")
    keep = _seed_corpus("beta")

    delete_corpus("alpha")

    assert not (corpus_root() / "alpha").exists()
    assert keep.is_dir()
    assert (keep / "files" / "a.txt").read_text() == "hello"


@pytest.mark.parametrize(
    "bad",
    ["../escape", "a/b", "..", ".", "", "a/../b", "with space", "a\x00b"],
)
def test_delete_corpus_rejects_invalid_or_traversal(
    tmp_errorta_home: Path, bad: str
) -> None:
    with pytest.raises(InvalidCorpusName):
        delete_corpus(bad)


def test_delete_corpus_traversal_does_not_touch_sibling(tmp_errorta_home: Path) -> None:
    # A sibling outside the corpus root must survive a traversal attempt.
    outside = corpus_root().parent / "outside.txt"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("keep me")

    with pytest.raises(InvalidCorpusName):
        delete_corpus("../outside.txt")

    assert outside.read_text() == "keep me"


# ---- DELETE /corpus/{name} ---------------------------------------------


def test_route_delete_corpus_ok(tmp_errorta_home: Path) -> None:
    d = _seed_corpus("docs")
    resp = _client().delete("/corpus/docs")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "corpus": "docs"}
    assert not d.exists()


def test_route_delete_corpus_unknown_404(tmp_errorta_home: Path) -> None:
    resp = _client().delete("/corpus/missing")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "corpus not found"


def test_route_delete_corpus_invalid_name_400(tmp_errorta_home: Path) -> None:
    # A single-segment name with a disallowed char is rejected before any
    # filesystem touch.
    resp = _client().delete("/corpus/bad@name")
    assert resp.status_code == 400
    assert "invalid corpus name" in resp.json()["detail"]


def test_route_delete_corpus_leaves_others(tmp_errorta_home: Path) -> None:
    _seed_corpus("alpha")
    keep = _seed_corpus("beta")

    resp = _client().delete("/corpus/alpha")
    assert resp.status_code == 200

    assert not (corpus_root() / "alpha").exists()
    assert keep.is_dir()
