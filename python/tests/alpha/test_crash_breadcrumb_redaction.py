"""The crash-breadcrumb privacy guarantee (spec §9 blocker fix).

A breadcrumb is module + line + exception class ONLY — never a file path, a raw
stack frame, a local value, or a corpus filename. redact.py does NOT strip
corpus filenames, so a raw traceback would leak document names; the breadcrumb
sidesteps that by never carrying free text.
"""
from __future__ import annotations

import re

from errorta_alpha import config, feedback, telemetry


def test_breadcrumb_is_class_plus_dotted_module_line(monkeypatch):
    # Trigger a real exception raised from inside an errorta_alpha module.
    monkeypatch.setenv("ERRORTA_ALPHA_PUBKEY", "AAAA")  # 3 bytes -> ValueError
    try:
        config.license_public_key_raw()
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        bc = feedback.build_crash_breadcrumb(exc)

    assert re.fullmatch(r"ValueError@errorta_alpha\.config:\d+", bc), bc
    # The three hard promises:
    assert "/" not in bc  # no filesystem path
    assert "\\" not in bc
    assert ".py" not in bc  # no source filename


def test_breadcrumb_falls_back_to_class_when_no_app_frame():
    try:
        raise KeyError("some-user-key-that-must-not-leak")
    except KeyError as exc:
        bc = feedback.build_crash_breadcrumb(exc)
    # No errorta frame in this test module -> class name only. Crucially, the
    # KeyError's *argument* (which could be user content) is NOT in the breadcrumb.
    assert bc == "KeyError"
    assert "user-key" not in bc


def test_record_crash_breadcrumb_refuses_path_shaped_descriptor(alpha_home):
    # Defensive backstop: even a mis-built descriptor containing a path is dropped.
    assert telemetry.record_crash_breadcrumb("ValueError@/Users/example/corpus/secret.pdf:12") is False
    assert telemetry.snapshot_queue() == []
    # A clean descriptor is accepted as a crash_breadcrumb extra.
    assert telemetry.record_crash_breadcrumb("ValueError@errorta_council.engine:412") is True
    q = telemetry.snapshot_queue()
    assert q[0]["event"] == "crash_breadcrumb"
    assert q[0]["bucket"] == "ValueError@errorta_council.engine:412"


def test_record_crash_is_inert_when_gate_off(tmp_path, monkeypatch):
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    monkeypatch.delenv("ERRORTA_ALPHA_GATE", raising=False)
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        feedback.record_crash(exc)  # must not raise, must not write
    from errorta_app.paths import alpha_telemetry_path

    assert not alpha_telemetry_path().exists()
