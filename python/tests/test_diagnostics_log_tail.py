from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from errorta_app.server import app


def test_log_tail_returns_recent_buffer_lines(tmp_errorta_home) -> None:
    logger = logging.getLogger("f032.tail")

    with TestClient(app) as client:
        app.state.log_buffer.clear()
        for i in range(12):
            logger.warning("F032_TAIL_%02d", i)

        response = client.get("/diagnostics/log-tail?lines=5")

    assert response.status_code == 200
    lines = response.json()["lines"]
    assert len(lines) == 5
    assert [line.rsplit(" ", 1)[-1] for line in lines] == [
        "F032_TAIL_07",
        "F032_TAIL_08",
        "F032_TAIL_09",
        "F032_TAIL_10",
        "F032_TAIL_11",
    ]


def test_log_tail_caps_requested_lines(tmp_errorta_home) -> None:
    logger = logging.getLogger("f032.tail.cap")

    with TestClient(app) as client:
        app.state.log_buffer.clear()
        logger.warning("F032_TAIL_CAP")

        response = client.get("/diagnostics/log-tail?lines=999999")

    assert response.status_code == 200
    assert len(response.json()["lines"]) == 1
