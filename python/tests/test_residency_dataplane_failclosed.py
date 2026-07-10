"""F086 Slice E — data-plane routes fail-closed under remote residency.

Under remote residency a route that materializes corpora/briefs on LOCAL disk
must refuse rather than silently write to the laptop while the judge runs
remotely. Local mode is unaffected.
"""
from __future__ import annotations

from pathlib import Path

import errorta_app.routes._residency_proxy as rp
from fastapi.testclient import TestClient

from errorta_app.server import app


def _remote(monkeypatch) -> None:
    monkeypatch.setattr(rp, "active_remote_base", lambda: ("http://127.0.0.1:9999", {}))


def _local(monkeypatch) -> None:
    monkeypatch.setattr(rp, "active_remote_base", lambda: None)


def test_corpus_upload_refused_in_remote_mode(tmp_errorta_home: Path, monkeypatch) -> None:
    _remote(monkeypatch)
    client = TestClient(app)
    r = client.post(
        "/corpus/demo/upload",
        files={"files": ("a.txt", b"hi", "text/plain")},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "residency_unsupported_path"
    # no local corpus materialized
    assert not (tmp_errorta_home / "corpora" / "demo").exists()


def test_corpus_upload_works_in_local_mode(tmp_errorta_home: Path, monkeypatch) -> None:
    _local(monkeypatch)
    client = TestClient(app)
    r = client.post(
        "/corpus/demo/upload",
        files={"files": ("a.txt", b"hi", "text/plain")},
    )
    assert r.status_code == 200, r.text


def test_export_import_refused_in_remote_mode(tmp_errorta_home: Path, monkeypatch) -> None:
    _remote(monkeypatch)
    client = TestClient(app)
    r = client.post(
        "/export/import",
        files={"tarball": ("b.tar.gz", b"x", "application/gzip")},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "residency_unsupported_path"


def test_brief_import_refused_in_remote_mode(tmp_errorta_home: Path, monkeypatch) -> None:
    _remote(monkeypatch)
    client = TestClient(app)
    r = client.post(
        "/briefs/import-bundle",
        files={"tarball": ("b.tar.gz", b"x", "application/gzip")},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "residency_unsupported_path"


def test_brief_run_refused_in_remote_mode(tmp_errorta_home: Path, monkeypatch) -> None:
    _remote(monkeypatch)
    client = TestClient(app)
    r = client.post("/briefs/ghost-brief/run")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "residency_unsupported_path"
