"""Telemetry store: consent, allowlist, floor, bounded extras queue (spec §9/§11)."""
from __future__ import annotations

import stat

from errorta_alpha import telemetry
from errorta_app.paths import alpha_telemetry_path


def test_gate_off_makes_recording_inert(tmp_path, monkeypatch):
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    monkeypatch.delenv("ERRORTA_ALPHA_GATE", raising=False)
    telemetry.record_launch()
    assert telemetry.record_feature_used("judge_run") is False
    telemetry.set_extras_enabled(False)
    # Nothing was written — a gate-off (production) build never phones telemetry.
    assert not alpha_telemetry_path().exists()


def test_floor_launch_counts(alpha_home):
    telemetry.record_launch()
    telemetry.record_launch()
    assert telemetry.snapshot_floor().get("launches") == 2


def test_feature_allowlist_drops_unknown_names(alpha_home):
    assert telemetry.record_feature_used("judge_run") is True
    # An attempt to smuggle content/path through a metric name is dropped.
    assert telemetry.record_feature_used("secret prompt /Users/example/corpus/x.pdf") is False
    q = telemetry.snapshot_queue()
    assert [e["name"] for e in q] == ["judge_run"]


def test_perf_requires_allowlisted_op_and_bucket(alpha_home):
    assert telemetry.record_perf("judge_verdict", "1-5s") is True
    assert telemetry.record_perf("judge_verdict", "3.271s") is False  # raw timing rejected
    assert telemetry.record_perf("exfiltrate", "1-5s") is False
    assert [e["bucket"] for e in telemetry.snapshot_queue()] == ["1-5s"]


def test_extras_opt_out_stops_queueing(alpha_home):
    telemetry.set_extras_enabled(False)
    assert telemetry.extras_enabled() is False
    assert telemetry.record_feature_used("judge_run") is False
    assert telemetry.snapshot_queue() == []
    # Floor (Tier-1) is NOT gated by the extras opt-out.
    telemetry.record_launch()
    assert telemetry.snapshot_floor().get("launches") == 1


def test_queue_cap_drops_oldest_and_flags_overflow(alpha_home, monkeypatch):
    monkeypatch.setattr(telemetry, "EXTRAS_QUEUE_CAP", 5)
    for _ in range(8):
        telemetry.record_feature_used("judge_run")
    q = telemetry.snapshot_queue()
    assert len(q) == 5  # capped
    assert telemetry.snapshot_floor().get("queue_overflow", 0) >= 1


def test_clear_floor_subtracts_only_sent(alpha_home):
    telemetry.record_launch()
    telemetry.record_launch()  # launches = 2
    telemetry.clear_floor({"launches": 2})
    assert telemetry.snapshot_floor().get("launches") is None
    telemetry.record_launch()  # launches = 1
    telemetry.record_launch()  # launches = 2
    telemetry.clear_floor({"launches": 1})  # a stale send of 1
    assert telemetry.snapshot_floor().get("launches") == 1  # 1 preserved


def test_drop_queue_prefix(alpha_home):
    for _ in range(3):
        telemetry.record_feature_used("judge_run")
    telemetry.drop_queue_prefix(2)
    assert len(telemetry.snapshot_queue()) == 1


def test_inspector_snapshot_shape(alpha_home):
    telemetry.record_launch()
    telemetry.record_feature_used("judge_run")
    snap = telemetry.inspector_snapshot()
    assert snap["extras_enabled"] is True
    assert snap["floor"].get("launches") == 1
    assert snap["queue_len"] == 1
    assert snap["queue"][0]["name"] == "judge_run"


def test_telemetry_file_is_owner_only(alpha_home):
    telemetry.record_launch()
    mode = stat.S_IMODE(alpha_telemetry_path().stat().st_mode)
    assert mode == 0o600


def test_load_drops_off_allowlist_events_from_a_tampered_store(alpha_home):
    """Defense in depth: a corrupted/tampered telemetry.json can never resurrect
    a non-allowlisted name (a smuggled prompt/path) onto the send queue."""
    from errorta_alpha.storage import write_json_0600

    write_json_0600(
        alpha_telemetry_path(),
        {
            "extras_enabled": True,
            "floor": {"launches": 1},
            "queue": [
                {"event": "feature_used", "name": "judge_run", "count": 1},
                {"event": "feature_used", "name": "/Users/example/secret.pdf", "count": 1},
                {"event": "perf_timing", "name": "judge_verdict", "bucket": "1-5s", "count": 1},
                {"event": "perf_timing", "name": "judge_verdict", "bucket": "3.271s", "count": 1},
                {"event": "exfiltrate", "name": "judge_run", "count": 1},
            ],
        },
    )
    q = telemetry.snapshot_queue()
    assert [e.get("name") for e in q] == ["judge_run", "judge_verdict"]


def test_load_drops_path_shaped_crash_breadcrumb_from_a_tampered_store(alpha_home):
    """slice 7: the crash_breadcrumb bucket is the one free-text field that rides
    the wire. The load path must mirror the producer's path refusal so a tampered
    store can't resurrect a filename/path onto /v1/metrics."""
    from errorta_alpha.storage import write_json_0600

    write_json_0600(
        alpha_telemetry_path(),
        {
            "extras_enabled": True,
            "floor": {},
            "queue": [
                {"event": "crash_breadcrumb", "bucket": "ValueError@errorta_council.engine:412",
                 "count": 1},
                {"event": "crash_breadcrumb", "bucket": "/Users/example/corpus/secret.pdf:12",
                 "count": 1},
                {"event": "crash_breadcrumb", "bucket": "C:\\Users\\me\\secret.pdf", "count": 1},
            ],
        },
    )
    q = telemetry.snapshot_queue()
    assert [e.get("bucket") for e in q] == ["ValueError@errorta_council.engine:412"]
