"""F095 — GET /corpora endpoint + convergence with the coding grounding lister."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _Remote:
    def __init__(self, instances):
        self._instances = instances

    def list_instances(self):
        return self._instances


def _client() -> TestClient:
    from errorta_app.routes import coding as coding_routes
    from errorta_app.routes import corpora as corpora_routes
    app = FastAPI()
    app.include_router(corpora_routes.router)
    app.include_router(coding_routes.router)
    return TestClient(app)


def _patch_remote(monkeypatch, adapter):
    monkeypatch.setattr(
        "errorta_project_grounding.remote_adapter.active_remote_adapter",
        lambda: adapter,
    )


def test_corpora_endpoint_remote(monkeypatch, tmp_errorta_home) -> None:
    _patch_remote(monkeypatch, _Remote([
        {"name": "discord-personas", "chunk_count": 3737, "published": True},
    ]))
    body = _client().get("/corpora").json()
    assert body["source"] == "remote"
    c0 = body["corpora"][0]
    assert c0["name"] == "discord-personas" and c0["source"] == "remote"
    assert c0["ready_count"] == 3737 and c0["status"] == "ready"
    assert c0["unit"] == "chunks"
    assert c0["capabilities"]["list_files"] is False


def test_corpora_endpoint_local_empty(monkeypatch, tmp_errorta_home) -> None:
    _patch_remote(monkeypatch, None)
    body = _client().get("/corpora").json()
    assert body == {"corpora": [], "source": "local"}  # empty tmp home


def test_corpora_and_grounding_corpora_return_same_list(monkeypatch, tmp_errorta_home) -> None:
    # The F095 lock test: the unified catalog feeds BOTH endpoints identically.
    _patch_remote(monkeypatch, _Remote([
        {"name": "a", "chunk_count": 5, "published": True},
        {"name": "b", "chunk_count": 0, "published": False},
    ]))
    c = _client()
    unified = c.get("/corpora").json()
    grounding = c.get("/coding/grounding/corpora").json()
    assert unified == grounding
    assert [x["name"] for x in unified["corpora"]] == ["a", "b"]


def test_local_file_route_refuses_remote_aiar_catalog(monkeypatch, tmp_errorta_home) -> None:
    _patch_remote(monkeypatch, _Remote([
        {"name": "discord-personas", "chunk_count": 3737, "published": True},
    ]))
    app = FastAPI()
    from errorta_app.routes import corpus as corpus_routes

    app.include_router(corpus_routes.router)
    r = TestClient(app).get("/corpus/discord-personas/files")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "unsupported_corpus_capability"
    assert r.json()["detail"]["capability"] == "list_files"


def _corpus_client() -> TestClient:
    from errorta_app.routes import corpus as corpus_routes

    app = FastAPI()
    app.include_router(corpus_routes.router)
    return TestClient(app)


# Every local-only mutation/file route must refuse (409) while the catalog is
# remote-backed — not just GET /files. Locks the guard against a future
# refactor that drops _require_local_catalog from any single route.
@pytest.mark.parametrize(
    ("method", "path", "capability", "kwargs"),
    [
        ("GET", "/corpus/discord-personas/files", "list_files", {}),
        # /upload declares `files=File(...)`; send one so FastAPI body
        # validation doesn't 422 before the in-function guard runs.
        (
            "POST",
            "/corpus/discord-personas/upload",
            "upload_files",
            {"files": {"files": ("a.txt", b"x")}},
        ),
        ("DELETE", "/corpus/discord-personas/files/f1", "upload_files", {}),
        ("DELETE", "/corpus/discord-personas", "upload_files", {}),
        ("POST", "/corpus/discord-personas/files/f1/reingest", "upload_files", {}),
        ("POST", "/corpus/discord-personas/reingest", "upload_files", {}),
        ("GET", "/corpus/discord-personas/refresh-preview", "refresh_preview", {}),
        ("POST", "/corpus/discord-personas/refresh-apply", "refresh_preview", {}),
    ],
)
def test_all_local_corpus_routes_refuse_remote_catalog(
    monkeypatch, tmp_errorta_home, method, path, capability, kwargs
) -> None:
    _patch_remote(monkeypatch, _Remote([
        {"name": "discord-personas", "chunk_count": 3737, "published": True},
    ]))
    client = _corpus_client()
    r = client.request(method, path, **kwargs)
    assert r.status_code == 409, f"{method} {path} should refuse remote catalog"
    detail = r.json()["detail"]
    assert detail["code"] == "unsupported_corpus_capability"
    assert detail["capability"] == capability
