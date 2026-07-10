"""TS-01 — Knowledge & Corpus: acceptance journey (the hermetic slice).

Walks the corpus lifecycle through the real routes:
upload a supported file -> it's accepted + listed (TC-01.1) -> identical content
is a duplicate (TC-01.3) -> an unsupported type is rejected (TC-01.6) -> delete the
file (TC-01.7) -> delete the corpus path-safely, unknown 404, invalid 400 (TC-01.8).

The extract -> chunk -> embed "Ready" tail needs Ollama/AIAR and is covered by the
`live` / manual layer, not here.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from errorta_app.server import app

pytestmark = [pytest.mark.acceptance, pytest.mark.regression]


@pytest.fixture
def client(tmp_errorta_home) -> TestClient:
    return TestClient(app)


def _results(resp):
    assert resp.status_code == 200, resp.text
    return resp.json()["results"]


def test_ts01_corpus_lifecycle(client) -> None:
    name = "qa-corpus"

    # TC-01.1: upload a supported file -> accepted (corpus auto-created).
    accepted = _results(
        client.post(f"/corpus/{name}/upload", files={"files": ("notes.txt", b"hello world")})
    )[0]
    assert accepted["status"] == "accepted"
    file_id = accepted["file_id"]

    # File is listed.
    listed = client.get(f"/corpus/{name}/files").json()["files"]
    assert any(f["file_id"] == file_id for f in listed)
    assert client.get("/corpora").status_code == 200

    # TC-01.3: identical content (different name) is a SHA-256 duplicate.
    dup = _results(
        client.post(f"/corpus/{name}/upload", files={"files": ("copy.txt", b"hello world")})
    )[0]
    assert dup["status"] == "duplicate"

    # TC-01.6: an unsupported type is rejected.
    bad = _results(
        client.post(f"/corpus/{name}/upload", files={"files": ("blob.bin", b"\x00\x01\x02")})
    )[0]
    assert bad["status"] == "rejected"

    # TC-01.7: delete the file -> gone from the list.
    assert client.delete(f"/corpus/{name}/files/{file_id}").status_code == 200
    assert client.get(f"/corpus/{name}/files").json()["files"] == []

    # TC-01.8: delete the corpus path-safely.
    assert client.delete(f"/corpus/{name}").status_code == 200
    assert client.delete(f"/corpus/{name}").status_code == 404  # now unknown
    assert client.delete("/corpus/bad@name").status_code == 400  # invalid name
