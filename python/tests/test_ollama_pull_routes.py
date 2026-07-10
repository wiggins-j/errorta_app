"""F110 — /ollama/models + /ollama/pull route layer (SSE, validation, residency)."""
from __future__ import annotations

import json
from typing import List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_app.routes import ollama as ollama_routes
from errorta_ollama import pull as pull_module


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(ollama_routes.router)
    return TestClient(app)


def _parse_sse(body: str) -> List[dict]:
    """Parse SSE body into a list of JSON payloads (skips the bare hello)."""
    out: List[dict] = []
    for frame in body.split("\n\n"):
        frame = frame.strip()
        if not frame:
            continue
        data_line = next(
            (ln for ln in frame.splitlines() if ln.startswith("data: ")), None
        )
        if not data_line:
            continue
        payload = data_line[len("data: ") :]
        if payload.strip() == "{}":
            continue  # hello
        out.append(json.loads(payload))
    return out


# --------------------------------------------------------------------------- #
# GET /ollama/models
# --------------------------------------------------------------------------- #


def test_models_lists_and_checks(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        pull_module, "installed_models", lambda: ["llama3.2:latest", "qwen2.5:7b"]
    )
    r = client.get("/ollama/models", params={"model": "llama3.2"})
    assert r.status_code == 200
    body = r.json()
    assert body["models"] == ["llama3.2:latest", "qwen2.5:7b"]
    assert body["queried"] == "llama3.2"
    assert body["installed"] is True


def test_models_rejects_bad_name(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pull_module, "installed_models", lambda: [])
    r = client.get("/ollama/models", params={"model": "--evil"})
    assert r.status_code == 400


def test_models_proxy_encodes_model_query(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, str] = {}

    def fake_proxy(method: str, path: str, **_kwargs):
        captured["method"] = method
        captured["path"] = path
        return {
            "models": ["registry.example.com/ns/model:tag"],
            "queried": "registry.example.com/ns/model:tag",
            "installed": True,
        }

    monkeypatch.setattr(ollama_routes, "proxy_json_if_remote", fake_proxy)

    r = client.get(
        "/ollama/models",
        params={"model": "registry.example.com/ns/model:tag"},
    )
    assert r.status_code == 200
    assert captured == {
        "method": "GET",
        "path": "/ollama/models?model=registry.example.com%2Fns%2Fmodel%3Atag",
    }


# --------------------------------------------------------------------------- #
# POST /ollama/pull (SSE)
# --------------------------------------------------------------------------- #


def test_pull_streams_progress_and_done(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pull_module, "is_model_installed", lambda m: False)

    def fake_pull(model, *, on_progress=None, timeout=3600.0):
        if on_progress:
            on_progress(pull_module.PullProgress(status="pulling manifest"))
            on_progress(pull_module.PullProgress(status="pulling 50%", percent=50.0))
        return pull_module.PullResult(
            succeeded=True, model=model, message=f"Pulled {model}."
        )

    monkeypatch.setattr(pull_module, "pull_model", fake_pull)

    r = client.post("/ollama/pull", json={"model": "llama3.2:3b"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(r.text)
    kinds = [e["event"] for e in events]
    assert "progress" in kinds
    assert kinds[-1] == "done"
    done = events[-1]
    assert done["model"] == "llama3.2:3b"
    # A progress frame carried a parsed percent.
    assert any(e.get("percent") == 50.0 for e in events if e["event"] == "progress")


def test_pull_failure_emits_error_frame(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pull_module, "is_model_installed", lambda m: False)

    def fake_pull(model, *, on_progress=None, timeout=3600.0):
        return pull_module.PullResult(
            succeeded=False, model=model, message="boom", error="model not found"
        )

    monkeypatch.setattr(pull_module, "pull_model", fake_pull)
    r = client.post("/ollama/pull", json={"model": "bogus:404"})
    assert r.status_code == 200
    events = _parse_sse(r.text)
    assert events[-1]["event"] == "error"
    assert "model not found" in events[-1]["error"]


def test_pull_rejects_bad_name_with_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_pull(*_a, **_k):
        raise AssertionError("pull_model must not run for an invalid name")

    monkeypatch.setattr(pull_module, "pull_model", no_pull)
    r = client.post("/ollama/pull", json={"model": "; rm -rf /"})
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# residency refusal (local-dataplane write under remote residency)
# --------------------------------------------------------------------------- #


def test_pull_refused_under_remote_residency(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_errorta_home
) -> None:
    from errorta_residency import config as residency_config

    residency_config.update(
        mode="ssh-remote",
        ssh_host="example-host",
        remote_sidecar_port=8770,
        local_tunnel_port=18770,
    )

    def no_pull(*_a, **_k):
        raise AssertionError("pull must be refused before reaching pull_model")

    monkeypatch.setattr(pull_module, "pull_model", no_pull)
    r = client.post("/ollama/pull", json={"model": "llama3.2:3b"})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "residency_unsupported_path"
    assert detail["path"] == "/ollama/pull"
