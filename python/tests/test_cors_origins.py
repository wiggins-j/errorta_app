"""CORS must allow the bundled-app webview origin.

The Tauri v2 .app serves the frontend from `http://tauri.localhost`. If that
origin isn't allowed, every non-GET request (Save / Create / Run) fails its
CORS preflight with 400, which the UI surfaces as a generic "Load failed".
GET works without a preflight, so the bug only shows on mutating requests in
the bundled app (dev uses http://localhost:1420, which was already allowed).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from errorta_app.server import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:1420",       # dev (Vite)
        "http://127.0.0.1:1420",       # dev (Vite, ip form)
        "tauri://localhost",           # Tauri v1 origin
        "http://tauri.localhost",      # Tauri v2 bundled app (macOS/Linux)
        "https://tauri.localhost",     # Tauri v2 bundled app (Windows)
    ],
)
def test_put_preflight_allowed_for_each_webview_origin(client, origin):
    resp = client.options(
        "/council/rooms/demo",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert resp.status_code == 200, (origin, resp.status_code, resp.text)
    assert resp.headers.get("access-control-allow-origin") == origin


def test_unknown_origin_still_rejected(client):
    resp = client.options(
        "/council/rooms/demo",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert resp.status_code == 400
