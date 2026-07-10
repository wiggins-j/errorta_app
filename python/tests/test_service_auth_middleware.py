"""F009-01 Service API token guard tests."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from errorta_app.auth import store
from errorta_app.auth.middleware import require_service_token
from errorta_app.auth.ratelimit import auth_failure_limiter


def _client() -> TestClient:
    app = FastAPI()

    @app.get("/protected/{corpus}")
    def protected(corpus: str, request: Request) -> dict:
        record = require_service_token(
            request,
            corpus=corpus,
            required_scope="prompt",
        )
        return {"token_id": record["id"], "last_used_at": record.get("last_used_at")}

    @app.get("/meta")
    def meta(request: Request) -> dict:
        record = require_service_token(request, required_scope="meta")
        return {"token_id": record["id"]}

    return TestClient(app)


def _issue_token(
    *,
    corpora: list[str] | None = None,
    scopes: list[str] | None = None,
) -> tuple[str, dict]:
    raw = store.mint_token()
    record = store.create_token(
        raw_token=raw,
        app_slug="demo-app",
        app_name="Demo App",
        corpora=corpora or ["welcome"],
        scopes=scopes or ["prompt", "meta"],
    )
    return raw, record


def test_missing_service_token_returns_token_missing(tmp_errorta_home):
    store.reset_state_for_tests()
    auth_failure_limiter.reset()
    resp = _client().get("/protected/welcome")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "token_missing"


def test_unknown_service_token_returns_token_revoked(tmp_errorta_home):
    store.reset_state_for_tests()
    auth_failure_limiter.reset()
    resp = _client().get("/protected/welcome", headers={"X-Errorta-Token": "ert_missing"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "token_revoked"


def test_revoked_service_token_returns_token_revoked(tmp_errorta_home):
    store.reset_state_for_tests()
    auth_failure_limiter.reset()
    raw, record = _issue_token()
    store.revoke_token(record["id"])

    resp = _client().get("/protected/welcome", headers={"X-Errorta-Token": raw})

    assert resp.status_code == 401
    assert resp.json()["detail"] == "token_revoked"


def test_service_token_denies_unapproved_corpus(tmp_errorta_home):
    store.reset_state_for_tests()
    auth_failure_limiter.reset()
    raw, _record = _issue_token(corpora=["welcome"])

    resp = _client().get("/protected/other", headers={"X-Errorta-Token": raw})

    assert resp.status_code == 403
    assert resp.json()["detail"] == "token_corpus_denied"


def test_service_token_denies_missing_scope(tmp_errorta_home):
    store.reset_state_for_tests()
    auth_failure_limiter.reset()
    raw, _record = _issue_token(scopes=["meta"])

    resp = _client().get("/protected/welcome", headers={"X-Errorta-Token": raw})

    assert resp.status_code == 403
    assert resp.json()["detail"] == "token_scope_denied"


def test_service_token_allows_granted_corpus_and_updates_last_used(tmp_errorta_home):
    store.reset_state_for_tests()
    auth_failure_limiter.reset()
    raw, record = _issue_token()

    resp = _client().get("/protected/welcome", headers={"X-Errorta-Token": raw})

    assert resp.status_code == 200
    assert resp.json()["token_id"] == record["id"]
    assert resp.json()["last_used_at"] is not None
    assert store.load_tokens()[0]["last_used_at"] is not None


def test_meta_scope_does_not_require_corpus_grant(tmp_errorta_home):
    store.reset_state_for_tests()
    auth_failure_limiter.reset()
    raw, record = _issue_token(corpora=[], scopes=["meta"])

    resp = _client().get("/meta", headers={"X-Errorta-Token": raw})

    assert resp.status_code == 200
    assert resp.json()["token_id"] == record["id"]
