"""TS-10 — System, Setup & Diagnostics: acceptance journey (hermetic slice).

Exercises the operational surface: shell status / processes / port (TC-10.9) ->
app settings + debug log level (TC-10.12) -> diagnostics log-tail + redacted
export bundle (TC-10.13) -> export plan (TC-10.14). Native installs/updater/USB/
tray stay manual per the plan.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from errorta_app.server import app
from tests.test_export_import import _build_fake_corpus, _pack_tarball
from tests.test_export_route import _parse_sse

pytestmark = [pytest.mark.acceptance, pytest.mark.regression]


@pytest.fixture
def client(tmp_errorta_home) -> TestClient:
    return TestClient(app)


def test_ts10_system_journey(client, tmp_errorta_home, tmp_path) -> None:
    # TC-10.9: shell ops surface real values.
    assert client.get("/shell/status").status_code == 200
    assert client.get("/shell/processes").status_code == 200
    assert client.get("/shell/sidecar/port").status_code == 200

    # TC-10.12: app settings read + debug log level set.
    settings = client.get("/settings")
    assert settings.status_code == 200
    assert "log_level" in settings.json()
    lvl = client.put("/settings/log-level", json={"level": "debug"})
    assert lvl.status_code == 200
    assert lvl.json()["log_level"] == "debug"

    # TC-10.13: diagnostics log-tail + a redacted export bundle.
    assert client.get("/diagnostics/log-tail?lines=5").status_code == 200
    export = client.post(
        "/diagnostics/export",
        json={"dest_path": str(tmp_path / "diag"), "user_note": "qa run"},
    )
    assert export.status_code == 200
    out = export.json()
    assert out["sha256"] and out["path"]
    assert "redaction_manifest" in out  # redaction applied to the bundle

    # TC-10.14: export plan/run + import roundtrip; only real USB hardware is manual.
    errorta_home = tmp_errorta_home / ".errorta"
    _build_fake_corpus(
        errorta_home,
        "ts10-corpus",
        [("alpha.txt", b"alpha bytes"), ("beta.md", b"beta bytes")],
    )
    target = tmp_path / "usb-out"
    plan = client.post(
        "/export/plan",
        json={"target_dir": str(target), "corpora_list": ["ts10-corpus"]},
    )
    assert plan.status_code == 200, plan.text
    assert plan.json()["files_count"] == 2

    with client.stream(
        "POST",
        "/export/run",
        json={"target_dir": str(target), "corpora_list": ["ts10-corpus"]},
    ) as resp:
        assert resp.status_code == 200
        body_text = "".join(chunk for chunk in resp.iter_text())
    events = _parse_sse(body_text)
    assert events[-1]["event"] == "done"
    manifest_path = Path(events[-1]["summary"]["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["file_count"] == 2
    assert all(item["sha256"] for item in manifest["files"].values())

    tarball = _pack_tarball(target, tmp_path / "bundle.tar.gz")
    shutil.rmtree(errorta_home / "corpora" / "ts10-corpus")
    with tarball.open("rb") as fh:
        imported = client.post(
            "/export/import",
            files={"tarball": ("bundle.tar.gz", fh, "application/gzip")},
        )
    assert imported.status_code == 200, imported.text
    assert imported.json()["corpora_imported"] == ["ts10-corpus"]
    imported_manifest = json.loads(
        (errorta_home / "corpora" / "ts10-corpus" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert imported_manifest["name"] == "ts10-corpus"
    assert len(imported_manifest["files"]) == 2
