from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from errorta_app import settings
from errorta_app.server import app


def test_get_settings_returns_persisted_settings(tmp_errorta_home) -> None:
    settings.save({"log_level": "debug"})

    with TestClient(app) as client:
        response = client.get("/settings")

    assert response.status_code == 200
    assert response.json() == {"log_level": "debug"}


def test_put_log_level_persists_and_applies_debug(tmp_errorta_home) -> None:
    root = logging.getLogger()
    uvicorn = logging.getLogger("uvicorn")
    old_root_level = root.level
    old_uvicorn_level = uvicorn.level
    try:
        with TestClient(app) as client:
            response = client.put("/settings/log-level", json={"level": "debug"})
            assert root.level == logging.DEBUG
            assert uvicorn.level == logging.DEBUG

        assert response.status_code == 200
        assert response.json() == {"log_level": "debug"}
        assert settings.load() == {"log_level": "debug"}
    finally:
        root.setLevel(old_root_level)
        uvicorn.setLevel(old_uvicorn_level)


def test_debug_toggle_allows_debug_records_into_log_tail(tmp_errorta_home) -> None:
    logger = logging.getLogger("f032.debug")
    target_loggers = [
        logging.getLogger(),
        logging.getLogger("uvicorn"),
        logging.getLogger("uvicorn.error"),
        logging.getLogger("uvicorn.access"),
    ]
    old_logger_levels = [(target, target.level) for target in target_loggers]
    old_handler_levels = [
        (handler, handler.level)
        for target in target_loggers
        for handler in target.handlers
    ]
    try:
        with TestClient(app) as client:
            app.state.log_buffer.clear()
            logger.debug("F032_DEBUG_BEFORE")
            before = client.get("/diagnostics/log-tail?lines=20").json()["lines"]

            response = client.put("/settings/log-level", json={"level": "debug"})
            assert response.status_code == 200
            logger.debug("F032_DEBUG_AFTER")
            after = client.get("/diagnostics/log-tail?lines=20").json()["lines"]

        assert all("F032_DEBUG_BEFORE" not in line for line in before)
        assert any("F032_DEBUG_AFTER" in line for line in after)
    finally:
        for target, level in old_logger_levels:
            target.setLevel(level)
        for handler, level in old_handler_levels:
            handler.setLevel(level)


def test_put_log_level_rejects_invalid_level(tmp_errorta_home) -> None:
    with TestClient(app) as client:
        response = client.put("/settings/log-level", json={"level": "trace"})

    assert response.status_code == 422


def test_model_family_settings_require_ui_origin_and_persist(tmp_errorta_home) -> None:
    with TestClient(app) as client:
        assert client.get("/settings/model-families").status_code == 403
        response = client.put(
            "/settings/model-families",
            headers={"x-errorta-origin": "tauri-ui"},
            json={"families": ["local"]},
        )
        read = client.get(
            "/settings/model-families",
            headers={"x-errorta-origin": "tauri-ui"},
        )
    assert response.status_code == 200
    assert response.json()["allowlist"] == ["local"]
    assert read.json()["effective"] == ["local"]


def test_tools_settings_persist_searxng_url(tmp_errorta_home, monkeypatch) -> None:
    monkeypatch.delenv("ERRORTA_SEARXNG_URL", raising=False)
    headers = {"x-errorta-origin": "tauri-ui"}
    with TestClient(app) as client:
        response = client.put(
            "/settings/tools",
            headers=headers,
            json={"searxng_url": "https://search.example.com"},
        )
        assert response.status_code == 200
        assert response.json() == {
            "searxng_url": "https://search.example.com",
            "configured": True,
            "env_configured": False,
        }

        read = client.get("/settings/tools", headers=headers)

    assert read.status_code == 200
    assert read.json()["searxng_url"] == "https://search.example.com"
    assert settings.load()["tools"] == {
        "searxng_url": "https://search.example.com"
    }


def test_tools_settings_reject_non_tauri_writes(tmp_errorta_home) -> None:
    with TestClient(app) as client:
        response = client.put(
            "/settings/tools",
            json={"searxng_url": "https://search.example.com"},
        )

    assert response.status_code == 403


def test_tools_settings_reject_invalid_searxng_url(tmp_errorta_home) -> None:
    with TestClient(app) as client:
        response = client.put(
            "/settings/tools",
            headers={"x-errorta-origin": "tauri-ui"},
            json={"searxng_url": "not-a-url"},
        )

    assert response.status_code == 422
