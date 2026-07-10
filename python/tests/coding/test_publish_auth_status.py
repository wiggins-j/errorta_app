from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_TAURI = {"x-errorta-origin": "tauri-ui"}
_GH_TOKEN_SHAPED = "ghp_0123456789abcdefghij0123456789abcd"


def _client(*, headers: dict[str, str] | None = None) -> TestClient:
    from errorta_app.routes import coding as coding_routes

    app = FastAPI()
    app.include_router(coding_routes.router)
    return TestClient(app, headers=headers)


def _project(project_id: str):
    from errorta_council.coding.ledger import LedgerStore

    store = LedgerStore(project_id)
    store.create_project(
        north_star="n", definition_of_done="d", target="new", repo_path=None)
    return store


def test_auth_status_reports_gh_present_and_login(
    tmp_errorta_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from errorta_tools.runner import github_secrets, publish

    _project("auth-present")
    monkeypatch.setattr(publish, "get_gh_binary", lambda: "/usr/local/bin/gh")
    monkeypatch.setattr(
        publish, "gh_auth_status",
        lambda: {"gh_present": True, "login": "octocat"})
    monkeypatch.setattr(github_secrets, "has_token", lambda: False)

    resp = _client(headers=_TAURI).get("/coding/projects/auth-present/publish/auth-status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"gh_present": True, "login": "octocat", "token_in_keychain": False}


def test_auth_status_requires_tauri_origin(
    tmp_errorta_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from errorta_tools.runner import github_secrets, publish

    _project("auth-origin")
    monkeypatch.setattr(publish, "gh_auth_status", lambda: {"gh_present": True, "login": "octocat"})
    monkeypatch.setattr(github_secrets, "has_token", lambda: True)

    resp = _client().get("/coding/projects/auth-origin/publish/auth-status")
    assert resp.status_code == 403
    assert "octocat" not in resp.text


def test_auth_status_gh_absent(
    tmp_errorta_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from errorta_tools.runner import github_secrets, publish

    _project("auth-absent")
    monkeypatch.setattr(publish, "get_gh_binary", lambda: None)
    monkeypatch.setattr(
        publish, "gh_auth_status",
        lambda: {"gh_present": False, "login": None})
    monkeypatch.setattr(github_secrets, "has_token", lambda: False)

    resp = _client(headers=_TAURI).get("/coding/projects/auth-absent/publish/auth-status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["gh_present"] is False
    assert body["login"] is None


def test_auth_status_token_value_never_appears_in_response(
    tmp_errorta_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with a token in the (mocked) keychain, only a boolean is returned."""
    from errorta_tools.runner import github_secrets, publish

    _project("auth-token")
    monkeypatch.setattr(
        publish, "gh_auth_status",
        lambda: {"gh_present": True, "login": "octocat"})
    monkeypatch.setattr(github_secrets, "keychain_get", lambda: _GH_TOKEN_SHAPED)
    monkeypatch.setattr(
        github_secrets, "has_token", lambda: github_secrets.keychain_get() is not None)

    resp = _client(headers=_TAURI).get("/coding/projects/auth-token/publish/auth-status")
    assert resp.status_code == 200, resp.text
    body_in = resp.json()
    assert body_in["token_in_keychain"] is True
    # The raw token must never be serialized into the HTTP response.
    assert _GH_TOKEN_SHAPED not in resp.text


def test_gh_auth_status_returns_no_token_when_gh_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real (unmocked) gh_auth_status must degrade to presence False / login
    None and never raise / never return a token when gh is absent."""
    from errorta_tools.runner import publish

    monkeypatch.setattr(publish, "get_gh_binary", lambda: None)
    status = publish.gh_auth_status()
    assert status == {"gh_present": False, "login": None}
    assert "token" not in status


def test_ledger_redacts_token_shaped_event_fields(tmp_errorta_home: Path) -> None:
    """A token-shaped string in a free-text event field is redacted on write."""
    from errorta_council.coding.publish_ledger import PublishLedger

    ledger = PublishLedger("ledger-redact")
    target = ledger.upsert_target(kind="manual_export")
    ledger.append_event(
        target_id=target.target_id, kind="existing_repo_pr", state="failed",
        error=f"push rejected, leaked {_GH_TOKEN_SHAPED} in remote URL")

    events = ledger.list_events()
    assert events, "event was not persisted"
    err = events[-1].error or ""
    assert _GH_TOKEN_SHAPED not in err
    assert "<token-redacted>" in err
    # And the raw token is not present anywhere on disk in the events file.
    raw = ledger._events_path.read_text(encoding="utf-8")
    assert _GH_TOKEN_SHAPED not in raw


def test_has_token_degrades_without_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """When keyring is unavailable, has_token / keychain_get degrade to False /
    None rather than raising."""
    from errorta_tools.runner import github_secrets

    monkeypatch.setattr(github_secrets, "_keyring", lambda: None)
    assert github_secrets.keychain_get() is None
    assert github_secrets.has_token() is False
