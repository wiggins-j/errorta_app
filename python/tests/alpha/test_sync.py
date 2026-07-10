"""sync() orchestration: floor cleared only when the heartbeat lands; extras
drained only on a 202; everything preserved when offline."""
from __future__ import annotations

import time

import pytest

from errorta_alpha import client as alpha_client
from errorta_alpha import device, telemetry
from errorta_alpha import license as license_store
from errorta_alpha.license import LicenseRecord


@pytest.fixture
def seeded(monkeypatch, alpha_keys, alpha_home):
    now = int(time.time())
    did = device.get_or_create_device_id()
    tok = alpha_keys.mint(device_id=did, grace_until=now + 14 * 86400)
    license_store.store(LicenseRecord(token=tok, grace_until=now + 14 * 86400,
                                      last_heartbeat=now - 7200, max_seen_epoch=now))
    box = {"hb": (200, None), "metrics": (202, {})}

    def fake_post(path, body):
        if path == "/v1/heartbeat":
            code = box["hb"][0]
            ok = {"status": "active", "token": tok, "grace_days": 14}
            return code, (ok if code == 200 else {})
        return box["metrics"]

    monkeypatch.setattr(alpha_client, "_post_json", fake_post)
    return box, now, tok


def test_active_sync_clears_floor_and_drains_extras(seeded):
    box, now, tok = seeded
    telemetry.record_launch()
    telemetry.record_feature_used("judge_run")

    alpha_client.sync(now=now)

    assert telemetry.snapshot_floor() == {}  # floor cleared after 200
    assert telemetry.snapshot_queue() == []  # extras drained after 202


def test_offline_heartbeat_preserves_floor_and_queue(seeded, monkeypatch):
    box, now, tok = seeded

    def boom(path, body):
        raise RuntimeError("offline")

    monkeypatch.setattr(alpha_client, "_post_json", boom)
    telemetry.record_launch()
    telemetry.record_feature_used("judge_run")

    alpha_client.sync(now=now)

    assert telemetry.snapshot_floor().get("launches") == 1  # kept
    assert len(telemetry.snapshot_queue()) == 1  # kept


def test_metrics_5xx_keeps_queue_but_floor_still_cleared(seeded):
    box, now, tok = seeded
    box["metrics"] = (500, {})
    telemetry.record_launch()
    telemetry.record_feature_used("judge_run")

    alpha_client.sync(now=now)

    assert telemetry.snapshot_floor() == {}  # heartbeat still 200 -> floor cleared
    assert len(telemetry.snapshot_queue()) == 1  # metrics failed -> queue kept
