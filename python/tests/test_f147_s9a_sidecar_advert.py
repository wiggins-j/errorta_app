"""F147 S9a — sidecar advertisement (${ERRORTA_HOME}/sidecar.json) + /healthz.

Covers the boot-time discovery file (written 0600, read back, removed only by its
owner) and the additive /healthz identity fields (pid/port/started_by), including
the full lifespan write-on-boot / remove-on-shutdown cycle.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from errorta_app import sidecar_advert as adv


def test_write_read_remove_roundtrip(
    tmp_errorta_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ERRORTA_STARTED_BY", "cli")
    path = adv.write_advertisement(port=8770, commit="abc123")
    assert path is not None and path.name == "sidecar.json"

    data = json.loads(path.read_text("utf-8"))
    assert data["port"] == 8770
    assert data["pid"] == os.getpid()
    assert data["commit"] == "abc123"
    assert data["started_by"] == "cli"
    assert data["started_at"]  # present, iso-ish

    # 0600 — the file reveals a loopback port + our pid.
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    got = adv.read_advertisement()
    assert got is not None and got["port"] == 8770


def test_started_by_defaults_to_unknown(
    tmp_errorta_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ERRORTA_STARTED_BY", raising=False)
    adv.write_advertisement(port=1234)
    assert (adv.read_advertisement() or {})["started_by"] == "unknown"


def test_remove_only_if_owned(tmp_errorta_home: Path) -> None:
    adv.write_advertisement(port=8770)
    # A different pid must NOT remove our advertisement.
    assert adv.remove_advertisement(only_if_pid=1) is False
    assert adv.read_advertisement() is not None
    # Our own pid removes it.
    assert adv.remove_advertisement() is True
    assert adv.read_advertisement() is None


def test_remove_when_absent_is_falsey(tmp_errorta_home: Path) -> None:
    assert adv.remove_advertisement() is False


def test_read_corrupt_returns_none(tmp_errorta_home: Path) -> None:
    adv.sidecar_json_path().write_text("{not json", encoding="utf-8")
    assert adv.read_advertisement() is None


def test_lifespan_writes_and_removes_advert_and_healthz_fields(
    tmp_errorta_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ERRORTA_STARTED_BY", "cli")
    from fastapi.testclient import TestClient

    from errorta_app.server import app

    # Absent before boot.
    assert adv.read_advertisement() is None
    with TestClient(app) as c:
        # Advertisement is live during the server lifespan.
        live = adv.read_advertisement()
        assert live is not None and live["started_by"] == "cli"
        assert live["pid"] == os.getpid()

        body = c.get("/healthz").json()
        # Additive identity fields.
        assert body["pid"] == os.getpid()
        assert body["port"] == live["port"]
        assert body["started_by"] == "cli"
        # Existing fields still present (additive, not replaced).
        assert body["service"] == "errorta-sidecar"
        assert body["council"] is True
    # Removed on graceful shutdown (we still owned it).
    assert adv.read_advertisement() is None
