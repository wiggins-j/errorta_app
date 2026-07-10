"""Feedback: bundle prep passes live corpus roots; multipart send; preview→submit."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_alpha import client as alpha_client
from errorta_alpha import device, feedback
from errorta_app.paths import corpus_dir
from errorta_app.routes import alpha as alpha_routes
from errorta_app.routes.alpha import router

_TAURI = {"x-errorta-origin": "tauri-ui"}


# ---- bundle prep ------------------------------------------------------------

def test_prepare_bundle_passes_live_corpus_roots(alpha_home, monkeypatch):
    corpus_dir("welcome")  # creates ~/.errorta/corpora/welcome
    captured = {}

    def fake_build_bundle(dest, *, user_note, log_buffer, corpus_roots):
        captured["corpus_roots"] = corpus_roots
        captured["user_note"] = user_note
        from pathlib import Path

        Path(dest).write_bytes(b"PK\x03\x04 fake zip")
        return {"path": str(dest), "sha256": "abc", "files": ["log.txt"], "redaction_manifest": {}}

    monkeypatch.setattr(feedback, "build_bundle", fake_build_bundle)
    result = feedback.prepare_feedback_bundle(user_note="it broke")

    # The corpus dir (and its parent) are passed so their path prefixes redact.
    assert any("welcome" in r for r in captured["corpus_roots"])
    assert captured["user_note"] == "it broke"
    assert result["sha256"] == "abc"


# ---- client send ------------------------------------------------------------

def test_send_feedback_multipart_success(alpha_home, monkeypatch, tmp_path):
    bundle = tmp_path / "b.zip"
    bundle.write_bytes(b"PK\x03\x04zipbytes")
    device.get_or_create_device_id()
    calls = {}

    def fake_multipart(path, fields, files):
        calls["path"] = path
        calls["fields"] = fields
        calls["files"] = files
        return 201, {"ticket_id": "tkt_123"}

    monkeypatch.setattr(alpha_client, "_post_multipart", fake_multipart)
    res = alpha_client.send_feedback(kind="crash", message="died", bundle_path=str(bundle))

    assert res.ok and res.ticket_id == "tkt_123"
    assert calls["path"] == "/v1/feedback"
    assert calls["fields"]["kind"] == "crash"
    assert calls["fields"]["message"] == "died"
    assert "device_id" in calls["fields"]
    assert calls["files"]["bundle"][0] == "b.zip"


def test_send_feedback_offline_is_soft_failure(alpha_home, monkeypatch):
    def boom(path, fields, files):
        raise RuntimeError("offline")

    monkeypatch.setattr(alpha_client, "_post_multipart", boom)
    res = alpha_client.send_feedback(kind="bug", message="x")
    assert res.ok is False and res.error == "offline"


def test_send_feedback_works_without_device_id(alpha_home, monkeypatch):
    # No device.json -> anonymous report (a locked/unactivated tester can send).
    captured = {}

    def fake_multipart(path, fields, files):
        captured["fields"] = fields
        return 201, {"ticket_id": "tkt_anon"}

    monkeypatch.setattr(alpha_client, "_post_multipart", fake_multipart)
    res = alpha_client.send_feedback(kind="suggestion", message="add dark mode")
    assert res.ok
    assert "device_id" not in captured["fields"]


# ---- routes -----------------------------------------------------------------

@pytest.fixture
def client(alpha_home, monkeypatch):
    # Keep the preview fast + hermetic: fake the bundle build.
    def fake_prepare(*, user_note, log_buffer=None):
        from errorta_app.paths import errorta_home

        p = errorta_home() / "feedback" / "prep.zip"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"PK\x03\x04")
        return {
            "path": str(p),
            "sha256": "sha",
            "files": ["log.txt"],
            "redaction_manifest": {"ips": 2},
        }

    monkeypatch.setattr(feedback, "prepare_feedback_bundle", fake_prepare)
    alpha_routes._PREPARED_FEEDBACK.clear()
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_preview_requires_origin(client):
    r = client.post("/alpha/feedback/preview", json={"kind": "bug", "message": "hi"})
    assert r.status_code == 403


def test_preview_then_submit_flow(client, monkeypatch):
    # Preview shows exactly what will be sent.
    r = client.post(
        "/alpha/feedback/preview", json={"kind": "bug", "message": "hi"}, headers=_TAURI
    )
    assert r.status_code == 200
    body = r.json()
    assert body["bundle"]["sha256"] == "sha"
    assert body["bundle"]["redaction"] == {"ips": 2}
    prepared_id = body["prepared_id"]

    sent = {}
    monkeypatch.setattr(
        alpha_client,
        "send_feedback",
        lambda **kw: (sent.update(kw) or alpha_client.FeedbackResult(ok=True, ticket_id="tkt_9")),
    )
    r2 = client.post("/alpha/feedback/submit", json={"prepared_id": prepared_id}, headers=_TAURI)
    assert r2.status_code == 200
    assert r2.json()["ticket_id"] == "tkt_9"
    assert sent["kind"] == "bug" and sent["message"] == "hi"

    # The prepared bundle is consumed — a second submit 404s.
    r3 = client.post("/alpha/feedback/submit", json={"prepared_id": prepared_id}, headers=_TAURI)
    assert r3.status_code == 404


def test_submit_unknown_id_is_404(client):
    r = client.post("/alpha/feedback/submit", json={"prepared_id": "nope"}, headers=_TAURI)
    assert r.status_code == 404


def test_feedback_routes_are_inert_when_gate_off(client, monkeypatch):
    """A keyless production build (gate off) must never build a bundle or phone
    home, even from a Tauri-origin localhost POST."""
    from errorta_alpha import config as alpha_config

    monkeypatch.setattr(alpha_config, "gate_enabled", lambda: False)
    built = {"n": 0}
    monkeypatch.setattr(
        feedback, "prepare_feedback_bundle", lambda **kw: built.__setitem__("n", built["n"] + 1)
    )

    r = client.post(
        "/alpha/feedback/preview", json={"kind": "bug", "message": "hi"}, headers=_TAURI
    )
    assert r.status_code == 409
    assert built["n"] == 0  # bundle build never ran

    r2 = client.post("/alpha/feedback/submit", json={"prepared_id": "x"}, headers=_TAURI)
    assert r2.status_code == 409
