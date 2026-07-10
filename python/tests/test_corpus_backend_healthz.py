"""F095 BE-4 — resolved corpus backend + /healthz coordination surface."""
from __future__ import annotations

from errorta_app import corpus_catalog


class _Remote:
    def list_instances(self):  # pragma: no cover - not exercised here
        return []


def test_resolve_backend_local_by_default(monkeypatch, tmp_errorta_home) -> None:
    monkeypatch.setattr(
        "errorta_project_grounding.remote_adapter.remote_aiar_config", lambda: None
    )
    b = corpus_catalog.resolve_corpus_backend()
    assert b["kind"] == "local"


def test_resolve_backend_remote_aiar(monkeypatch, tmp_errorta_home) -> None:
    class _Cfg:
        base_url = "http://example-host.example:8766"

    monkeypatch.setattr(
        "errorta_project_grounding.remote_adapter.remote_aiar_config", lambda: _Cfg()
    )
    b = corpus_catalog.resolve_corpus_backend()
    assert b["kind"] == "remote_aiar"
    assert b["detail"]["base_url"] == "http://example-host.example:8766"


def test_healthz_includes_corpus_backend(monkeypatch, tmp_errorta_home) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setattr(
        "errorta_project_grounding.remote_adapter.remote_aiar_config", lambda: None
    )
    from errorta_app.server import app

    body = TestClient(app).get("/healthz").json()
    assert "corpus_backend" in body
    cb = body["corpus_backend"]
    assert cb["kind"] == "local"
    # local backend retrieves locally → coordinated.
    assert cb["retrieval_coordinated"] is True


def test_remote_aiar_backend_is_coordinated(
    monkeypatch, tmp_errorta_home
) -> None:
    from fastapi.testclient import TestClient
    from errorta_aiar_connection.models import AiarRuntime

    class _Cfg:
        base_url = "http://example-host.example:8766"
        token = None

    monkeypatch.setattr(
        "errorta_project_grounding.remote_adapter.remote_aiar_config", lambda: _Cfg()
    )
    monkeypatch.setattr(
        "errorta_aiar_connection.resolve_aiar_runtime",
        lambda: AiarRuntime(
            kind="aiar-service",
            display_name="example-host",
            connected=True,
            base_url="http://example-host.example:8766",
            backend_id="http://example-host.example:8766",
        ),
    )
    from errorta_app.server import app

    cb = TestClient(app).get("/healthz").json()["corpus_backend"]
    assert cb["kind"] == "remote_aiar"
    # F116: corpora AND retrieval both resolve to the same remote AIAR (retrieval
    # queries funnel through aiar_retrieval_target, which prefers remote AIAR), so
    # the catalog and retrieval are coordinated.
    assert cb["retrieval_coordinated"] is True
