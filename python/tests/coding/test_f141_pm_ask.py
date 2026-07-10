"""F141 WS-J — synchronous PM chat (gateway mocked, no real model call)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_TAURI = {"x-errorta-origin": "tauri-ui"}


def _client() -> TestClient:
    from errorta_app.routes import coding as coding_routes
    app = FastAPI()
    app.include_router(coding_routes.router)
    return TestClient(app, headers=_TAURI)


def _project_with_pm(project_id: str):
    from errorta_council.coding.ledger import LedgerStore
    store = LedgerStore(project_id)
    store.create_project(north_star="Ship a thing", definition_of_done="tests green",
                         target="new", repo_path=None)
    store.set_run_config(members=[
        {"member_id": "m-pm", "role": "answerer", "enabled": True,
         "gateway_route_id": "local.qwen", "provider_kind": "local",
         "model": "qwen", "metadata": {"coding_role": "pm"}},
    ])
    return store


def test_pm_ask_404_unknown_project(tmp_errorta_home: Path) -> None:
    r = _client().post("/coding/projects/nope/pm-ask", json={"message": "hi"})
    assert r.status_code == 404


def test_pm_ask_400_empty(tmp_errorta_home: Path) -> None:
    _project_with_pm("pa0")
    r = _client().post("/coding/projects/pa0/pm-ask", json={"message": "  "})
    assert r.status_code == 400


def test_pm_ask_unconfigured_team_is_honest(tmp_errorta_home: Path) -> None:
    from errorta_council.coding.ledger import LedgerStore
    LedgerStore("pa1").create_project(north_star="n", definition_of_done="d",
                                      target="new", repo_path=None)
    r = _client().post("/coding/projects/pa1/pm-ask", json={"message": "status?"})
    assert r.status_code == 200
    body = r.json()
    assert body["answered"] is False
    assert body["reply"]["kind"] == "unconfigured"


def test_pm_ask_returns_model_reply_and_persists_thread(
        tmp_errorta_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _project_with_pm("pa2")
    from errorta_council.coding import runner as runner_mod

    def fake_caller(_gateway):
        def _call(member, prompt):
            assert "You are the PM" in prompt  # Q&A prompt, not a plan
            assert "status?" in prompt
            return "We're on track — 0 tasks so far."
        return _call

    monkeypatch.setattr(runner_mod, "gateway_member_caller", fake_caller)
    r = _client().post("/coding/projects/pa2/pm-ask", json={"message": "status?"})
    assert r.status_code == 200
    body = r.json()
    assert body["answered"] is True
    assert body["reply"]["kind"] == "chat"
    assert "on track" in body["reply"]["message"]

    # both turns persisted, and the second call sees the first (multi-turn).
    thread = _client().get("/coding/projects/pa2/pm-chat").json()["thread"]
    assert [t["role"] for t in thread] == ["user", "pm"]
    assert thread[0]["message"] == "status?"


def test_pm_ask_gateway_failure_is_retryable(
        tmp_errorta_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _project_with_pm("pa3")
    from errorta_council.coding import runner as runner_mod

    def boom_caller(_gateway):
        def _call(member, prompt):
            raise RuntimeError("provider down")
        return _call

    monkeypatch.setattr(runner_mod, "gateway_member_caller", boom_caller)
    r = _client().post("/coding/projects/pa3/pm-ask", json={"message": "hi"})
    assert r.status_code == 200
    body = r.json()
    assert body["answered"] is False
    assert body["error"] == "pm_unreachable"
    # the user turn is still recorded; no fake pm turn was appended.
    thread = _client().get("/coding/projects/pa3/pm-chat").json()["thread"]
    assert [t["role"] for t in thread] == ["user"]
