"""Invariant 2 — no content ever leaves the machine.

Capture EVERY outbound /v1/heartbeat and /v1/metrics body during a full sync and
assert it carries only enum names, integer counts, bucket labels, version,
platform, and the device UUID — never a prompt, answer, filename, corpus name,
or filesystem path. This is the marquee privacy gate for F-DIST-01.
"""
from __future__ import annotations

import json
import re
import time

import pytest

from errorta_alpha import client as alpha_client
from errorta_alpha import device, telemetry
from errorta_alpha import license as license_store
from errorta_alpha.license import LicenseRecord

# A path-shaped string must never appear in any outbound body.
_PATH_RE = re.compile(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
# Field names allowed to appear as JSON keys/values in an outbound body.
_ALLOWED_KEYS = {
    "device_id", "app_version", "platform", "floor", "events",
    "launches", "crash_free_sessions", "queue_overflow",
    "event", "name", "count", "bucket",
}
_ALLOWED_EVENT_NAMES = telemetry.FEATURE_NAMES | telemetry.PERF_OPS
_ALLOWED_EVENT_TYPES = {"feature_used", "perf_timing", "crash_breadcrumb"}


@pytest.fixture
def capture(monkeypatch, alpha_keys, alpha_home):
    """Seed an active license + capture outbound bodies from a mocked transport."""
    now = int(time.time())
    did = device.get_or_create_device_id()
    tok = alpha_keys.mint(device_id=did, grace_until=now + 14 * 86400)
    license_store.store(LicenseRecord(token=tok, grace_until=now + 14 * 86400,
                                      last_heartbeat=now - 7200, max_seen_epoch=now))
    bodies: list[tuple[str, dict]] = []

    def fake_post(path, body):
        bodies.append((path, body))
        if path == "/v1/heartbeat":
            return 200, {"status": "active", "token": tok, "grace_days": 14}
        return 202, {}

    monkeypatch.setattr(alpha_client, "_post_json", fake_post)
    return bodies, now


def _assert_clean(body: dict) -> None:
    # Structural: only allowlisted keys anywhere in the payload.
    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                assert k in _ALLOWED_KEYS, f"unexpected key in outbound body: {k}"
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)
    walk(body)
    # Textual: the serialized body has no path-shaped substring.
    blob = json.dumps(body)
    assert not _PATH_RE.search(blob), f"path-shaped text in outbound body: {blob}"


def test_sync_bodies_carry_no_content(capture):
    bodies, now = capture
    # A prompt/path someone might try to leak — dropped because it isn't an
    # allowlisted event name, so it can never reach the wire.
    assert telemetry.record_feature_used("SELECT * FROM /Users/example/corpus/secret.pdf") is False
    # Legitimate, allowlisted events.
    telemetry.record_feature_used("judge_run")
    telemetry.record_feature_used("corpus_ingest")
    telemetry.record_perf("judge_verdict", "1-5s")
    telemetry.record_launch()

    alpha_client.sync(now=now)

    assert bodies, "sync should have sent at least a heartbeat"
    for path, body in bodies:
        _assert_clean(body)
        if path == "/v1/metrics":
            for e in body["events"]:
                assert e["event"] in _ALLOWED_EVENT_TYPES
                assert e["name"] in _ALLOWED_EVENT_NAMES
                assert isinstance(e["count"], int)


def test_extras_off_sends_only_floor(capture):
    bodies, now = capture
    telemetry.set_extras_enabled(False)
    telemetry.record_feature_used("judge_run")  # dropped (extras off)
    telemetry.record_launch()

    alpha_client.sync(now=now)

    paths = [p for p, _ in bodies]
    assert "/v1/heartbeat" in paths
    assert "/v1/metrics" not in paths  # nothing extra to send
