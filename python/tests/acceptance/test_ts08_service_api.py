"""TS-08 — Service API & Connected Apps: acceptance journey.

Chains the human cases in docs/TEST_CASES.md into one real flow:
pair (TC-08.1) -> non-owner cannot approve (TC-08.9) -> owner approves, token
minted at approval and delivered exactly once (TC-08.2) -> granted-corpus call
200 (TC-08.3) -> non-granted 403 (TC-08.4) -> unauthenticated 401 + open routes
stay open (TC-08.5) -> tokens stored hash-only (TC-08.8) -> owner-only token list
(TC-08.9) -> revoke -> next call 401 (TC-08.6). Plus deny (TC-08.7).
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from errorta_app.auth import pairing, store
from errorta_app.auth.ratelimit import auth_failure_limiter
from errorta_app.paths import auth_tokens_path
from errorta_app.routes import services
from errorta_app.server import app
from errorta_query.models import AnswerResult, QueryResult, Retrieval, Verdict

pytestmark = [pytest.mark.acceptance, pytest.mark.security, pytest.mark.blocking]

TOKEN_RE = re.compile(r"^ert_[0-9a-f]{32}$")
OWNER = {"x-errorta-origin": "tauri-ui"}


class _FakePipeline:
    """Deterministic, offline — the journey tests the AUTH boundary, not the
    model, so the protected pipeline is faked."""

    def query(self, **kw) -> list[QueryResult]:
        return [QueryResult(
            content="c", corpus_id=kw.get("corpus_ids", ["granted"])[0],
            chunk_id="ch", citation_id="ci", score=0.9, source="/x", page_span=(1, 1),
        )]

    # F009-02: the Service API route requires strict retrieval.
    def query_strict(self, **kw) -> list[QueryResult]:
        return self.query(**kw)

    def answer(self, **kw) -> AnswerResult:
        return AnswerResult(
            answer="ok", model=kw.get("model"),
            verdict=Verdict(rating="good", reason="r", failure_tags=[], confidence=0.9),
            retrieval=Retrieval(grounded=True, reground_applied=False, top_k=4, chunks_used=1),
            prompt_signature="sig", aiar=True,
        )


@pytest.fixture
def client(tmp_errorta_home, monkeypatch) -> TestClient:
    store.reset_state_for_tests()
    pairing.reset_state_for_tests()
    auth_failure_limiter.reset()
    # The on-disk corpora ARE the catalog: F009-02 replaced the old
    # `_corpus_catalog` shim with `resolve_service_catalog(list_all_corpora, …)`,
    # and `list_all_corpora` reads ERRORTA_HOME's local corpora (verified local).
    for corpus in ("granted", "ungranted"):
        (tmp_errorta_home / ".errorta" / "corpora" / corpus).mkdir(parents=True)
    monkeypatch.setattr(services, "default_pipeline", lambda: _FakePipeline())
    return TestClient(app)


def _pair(client) -> str:
    body = {
        "app_slug": "sdk", "app_name": "SDK Demo",
        "requested_corpora": ["granted"], "requested_scopes": ["prompt", "meta"],
    }
    resp = client.post("/api/auth/pair", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending"
    return data["session_id"]


def _prompt(corpus: str) -> dict:
    return {"prompt": "q", "corpus": corpus, "model": "m", "judge": True, "top_k": 8}


def test_ts08_service_api_full_journey(client) -> None:
    sid = _pair(client)  # TC-08.1

    # TC-08.1: polling stays pending, no token before consent.
    assert client.get(f"/api/auth/pair-status/{sid}").json().get("token") is None

    approve_body = {"corpora": ["granted"], "scopes": ["prompt", "meta"]}
    # TC-08.9: a non-owner (no Tauri origin) cannot approve.
    assert client.post(f"/api/auth/pair/{sid}/approve", json=approve_body).status_code == 403

    # TC-08.2: owner approves -> token minted AT approval, delivered exactly once.
    assert client.post(
        f"/api/auth/pair/{sid}/approve", json=approve_body, headers=OWNER
    ).status_code == 200
    first = client.get(f"/api/auth/pair-status/{sid}").json()
    token = first["token"]
    assert TOKEN_RE.match(token)
    assert client.get(f"/api/auth/pair-status/{sid}").json().get("token") is None  # consumed

    auth = {"x-errorta-token": token}
    # TC-08.3: granted corpus succeeds.
    assert client.post("/services/prompt", json=_prompt("granted"), headers=auth).status_code == 200
    # TC-08.4: a corpus NOT in the grant is denied before any pipeline call.
    denied = client.post("/services/prompt", json=_prompt("ungranted"), headers=auth)
    assert denied.status_code == 403
    assert denied.json()["detail"] == "token_corpus_denied"
    # TC-08.5: no token -> 401; open routes stay open.
    assert client.post("/services/prompt", json=_prompt("granted")).status_code == 401
    assert client.get("/healthz").status_code == 200

    # TC-08.11: /services/meta is scoped to the token's corpus grant.
    meta = client.get("/services/meta", headers=auth)
    assert meta.status_code == 200, meta.text
    names = [item["name"] for item in meta.json()["corpora"]]
    assert names == ["granted"]

    # TC-08.8: persisted hash-only — the raw token never touches disk.
    raw = auth_tokens_path().read_text(encoding="utf-8")
    assert token not in raw
    assert "token_sha256" in raw
    record = store.load_tokens()[0]
    assert "token" not in record and record["token_sha256"]

    # TC-08.9: token list is owner-only.
    assert client.get("/api/auth/tokens").status_code == 403
    tokens = client.get("/api/auth/tokens", headers=OWNER).json()["tokens"]
    assert len(tokens) == 1
    token_id = tokens[0]["id"]

    # TC-08.6: revoke -> the next call fails 401 token_revoked.
    assert client.delete(f"/api/auth/tokens/{token_id}", headers=OWNER).status_code == 200
    revoked = client.post("/services/prompt", json=_prompt("granted"), headers=auth)
    assert revoked.status_code == 401
    assert revoked.json()["detail"] == "token_revoked"


def test_ts08_owner_can_deny(client) -> None:
    """TC-08.7 — denying a pairing request issues no token."""
    sid = _pair(client)
    assert client.post(f"/api/auth/pair/{sid}/deny", headers=OWNER).status_code == 200
    assert client.get(f"/api/auth/pair-status/{sid}").json()["status"] == "denied"
    assert store.load_tokens() == []


def test_ts08_restart_between_approve_and_poll_reports_token_lost(client) -> None:
    """TC-08.10 — restart after approve never double-issues a token."""
    sid = _pair(client)
    approve_body = {"corpora": ["granted"], "scopes": ["prompt", "meta"]}
    assert client.post(
        f"/api/auth/pair/{sid}/approve", json=approve_body, headers=OWNER
    ).status_code == 200
    issued = store.load_tokens()
    assert len(issued) == 1

    # Simulate a sidecar restart: persisted pairing/token state survives, but
    # the one-time raw token held in memory is gone.
    pairing.reset_state_for_tests()
    lost = client.get(f"/api/auth/pair-status/{sid}").json()
    assert lost["status"] == "accepted"
    assert lost["token_lost"] is True
    assert "token" not in lost

    assert store.load_tokens()[0]["id"] == issued[0]["id"]
    consumed = client.get(f"/api/auth/pair-status/{sid}").json()
    assert consumed["status"] == "consumed"
    assert "token" not in consumed


def test_ts08_meta_requires_meta_scope(client) -> None:
    """TC-08.11 — prompt-only grants cannot read /services/meta."""
    sid = _pair(client)
    assert client.post(
        f"/api/auth/pair/{sid}/approve",
        json={"corpora": ["granted"], "scopes": ["prompt"]},
        headers=OWNER,
    ).status_code == 200
    token = client.get(f"/api/auth/pair-status/{sid}").json()["token"]
    denied = client.get("/services/meta", headers={"x-errorta-token": token})
    assert denied.status_code == 403
    assert denied.json()["detail"] == "token_scope_denied"


def test_ts08_rate_limits_pair_and_auth_failures(client) -> None:
    """TC-08.12 — repeated pair/auth failures lock out the source."""
    bad_pair = {"app_slug": "", "app_name": "SDK Demo", "requested_corpora": []}
    for _ in range(8):
        resp = client.post("/api/auth/pair", json=bad_pair)
        assert resp.status_code == 400
    limited_pair = client.post("/api/auth/pair", json=bad_pair)
    assert limited_pair.status_code == 429
    assert limited_pair.json()["detail"] == "pairing_rate_limited"

    for _ in range(12):
        resp = client.post("/services/prompt", json=_prompt("granted"))
        assert resp.status_code == 401
    limited_auth = client.post("/services/prompt", json=_prompt("granted"))
    assert limited_auth.status_code == 429
    assert limited_auth.json()["detail"] == "auth_rate_limited"
