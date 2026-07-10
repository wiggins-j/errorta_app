"""F008e — /briefs router smoke tests via fastapi.testclient.

Hermetic: HOME is redirected to tmp via ``tmp_errorta_home``. No network is
touched — connectors are not registered, which is fine for these route-level
smoke tests (the runner happily runs to FAILED state with an unknown
connector, but the routes return their declared shapes either way).
"""
from __future__ import annotations

import textwrap
import threading
import time
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from errorta_briefs.connector import SourceConnector, SourceDoc
from errorta_briefs.runner import CONNECTOR_REGISTRY, reset_active_run


BRIEF_MD = textwrap.dedent(
    """\
    ---
    project: Test Project
    corpus: test-corpus
    sensitivity: Public
    refresh: manual
    sources:
      - name: fake
        config: {}
    ---

    Body prose.
    """
)


@pytest.fixture(autouse=True)
def _reset_runner_singletons() -> Iterator[None]:
    reset_active_run()
    CONNECTOR_REGISTRY.clear()
    yield
    reset_active_run()
    CONNECTOR_REGISTRY.clear()


@pytest.fixture
def client(tmp_errorta_home: Path) -> Iterator[TestClient]:
    # Import after HOME has been redirected so corpus_root() resolves under tmp.
    from errorta_app.server import app

    with TestClient(app) as c:
        yield c


def test_healthz_reports_briefs(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body.get("briefs") is True


def test_create_then_get_then_list(client: TestClient) -> None:
    r = client.post("/briefs", json={"markdown": BRIEF_MD})
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["brief_id"] == "test-corpus"
    assert out["state"] == "DRAFT"

    r2 = client.get("/briefs/test-corpus")
    assert r2.status_code == 200
    body = r2.json()
    assert body["manifest"]["brief_id"] == "test-corpus"
    assert body["config"]["corpus"] == "test-corpus"

    r3 = client.get("/briefs")
    assert r3.status_code == 200
    listing = r3.json()
    assert any(b["brief_id"] == "test-corpus" for b in listing)


def test_create_rejects_invalid_markdown(client: TestClient) -> None:
    r = client.post("/briefs", json={"markdown": "no front matter here"})
    assert r.status_code == 400


def test_validate_reports_unknown_connector(client: TestClient) -> None:
    client.post("/briefs", json={"markdown": BRIEF_MD})
    r = client.post("/briefs/test-corpus/validate")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "fake" in body["connectors"]
    assert body["connectors"]["fake"]["ok"] is False


def test_start_alias_returns_run_id(client: TestClient) -> None:
    """Acceptance: POST /briefs/{name}/start returns {run_id} and transitions to RUNNING."""

    class _BlockedConnector(SourceConnector):
        gate = threading.Event()

        def __init__(self, config: dict) -> None:
            pass

        def search(self, page: int) -> Iterator[SourceDoc]:
            _BlockedConnector.gate.wait(timeout=2.0)
            return iter([])

        def fetch(self, doc: SourceDoc) -> bytes:  # pragma: no cover
            return b""

        def canonical_id(self, doc: SourceDoc) -> str:  # pragma: no cover
            return doc.canonical_id

        def metadata(self, doc: SourceDoc) -> dict:  # pragma: no cover
            return {}

        def status(self) -> dict:
            return {"ok": True}

    CONNECTOR_REGISTRY["fake"] = _BlockedConnector
    client.post("/briefs", json={"markdown": BRIEF_MD})
    r = client.post("/briefs/test-corpus/start")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "run_id" in body and body["run_id"]
    assert body["state"] == "RUNNING"

    # /status returns a JSON snapshot with a state field.
    rs = client.get("/briefs/test-corpus/status")
    assert rs.status_code == 200
    snap = rs.json()
    assert "state" in snap
    assert "per_source" in snap

    # Drain so the singleton lock releases cleanly.
    _BlockedConnector.gate.set()
    # Allow the run thread to settle.
    for _ in range(20):
        if client.get("/briefs/test-corpus/status").json()["state"] in {
            "COMPLETED",
            "FAILED",
        }:
            break
        time.sleep(0.05)


def test_run_endpoint_also_returns_run_id(client: TestClient) -> None:
    client.post("/briefs", json={"markdown": BRIEF_MD})
    r = client.post("/briefs/test-corpus/run")
    assert r.status_code == 200
    assert "run_id" in r.json()


def test_pause_without_active_run_is_409(client: TestClient) -> None:
    client.post("/briefs", json={"markdown": BRIEF_MD})
    r = client.post("/briefs/test-corpus/pause")
    assert r.status_code == 409


def test_update_brief_rewrites_markdown(client: TestClient) -> None:
    client.post("/briefs", json={"markdown": BRIEF_MD})
    new_md = BRIEF_MD.replace("Test Project", "Updated Project")
    r = client.put("/briefs/test-corpus", json={"markdown": new_md})
    assert r.status_code == 200, r.text
    g = client.get("/briefs/test-corpus").json()
    assert g["config"]["project"] == "Updated Project"


def test_delete_brief_404s_on_unknown(client: TestClient) -> None:
    r = client.delete("/briefs/does-not-exist")
    assert r.status_code == 404


def test_delete_brief_succeeds(client: TestClient) -> None:
    client.post("/briefs", json={"markdown": BRIEF_MD})
    r = client.delete("/briefs/test-corpus")
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    # Subsequent get is 404.
    assert client.get("/briefs/test-corpus").status_code == 404


def test_all_ten_endpoints_registered() -> None:
    """Spec §8: 10 endpoints under /briefs are registered."""
    from errorta_app.server import app

    schema = app.openapi()
    paths_methods = {
        (path, method.upper())
        for path, methods in schema.get("paths", {}).items()
        if path.startswith("/briefs")
        for method in methods
    }
    expected = {
        ("/briefs", "GET"),
        ("/briefs", "POST"),
        ("/briefs/{brief_id}", "GET"),
        ("/briefs/{brief_id}", "PUT"),
        ("/briefs/{brief_id}", "DELETE"),
        ("/briefs/{brief_id}/validate", "POST"),
        ("/briefs/{brief_id}/run", "POST"),
        ("/briefs/{brief_id}/refresh", "POST"),
        ("/briefs/{brief_id}/pause", "POST"),
        ("/briefs/{brief_id}/status", "GET"),
    }
    missing = expected - paths_methods
    assert not missing, f"missing endpoints: {missing}"
