"""F074 — desktop auto-surface: the mobile-activity tracker + route."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from errorta_app import server as server_mod
from errorta_mobile import activity


@pytest.fixture(autouse=True)
def _reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    activity.reset()
    yield
    activity.reset()


def test_tracker_records_latest_with_monotonic_seq() -> None:
    assert activity.latest() == {"run_id": None, "seq": 0}
    activity.record("r1", "message")
    first = activity.latest()
    assert first["run_id"] == "r1" and first["kind"] == "message" and first["seq"] == 1
    activity.record("r2", "start")
    second = activity.latest()
    assert second["run_id"] == "r2" and second["seq"] == 2
    # Empty run_id is ignored (no bump).
    activity.record("", "message")
    assert activity.latest()["seq"] == 2


def test_route_returns_latest() -> None:
    client = TestClient(server_mod.app)
    seeded = client.get("/council/mobile-activity")
    assert seeded.status_code == 200
    assert seeded.json() == {"run_id": None, "seq": 0}

    activity.record("run-xyz", "message")
    after = client.get("/council/mobile-activity").json()
    assert after["run_id"] == "run-xyz"
    assert after["kind"] == "message"
    assert after["seq"] == 1
