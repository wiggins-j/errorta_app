"""F048 — sidecar/runner lifecycle diagnostics."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from errorta_diagnostics import lifecycle as L


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))


def test_config_signature_is_stable_for_identical_inputs():
    inputs = {"sidecar_version": "1", "residency_mode": "local", "log_level": "info"}
    assert L.config_signature(inputs) == L.config_signature(dict(inputs))


def test_config_signature_changes_when_a_relevant_setting_changes():
    base = {"sidecar_version": "1", "residency_mode": "local"}
    changed = {"sidecar_version": "1", "residency_mode": "cloud"}
    assert L.config_signature(base) != L.config_signature(changed)


def test_config_signature_changes_when_remote_host_changes():
    # Two SSH targets in the SAME residency mode must produce different
    # signatures (regression: _remote_host_id used to read non-existent fields
    # and always returned None, so host switches were invisible).
    base = {"residency_mode": "ssh", "remote_host_id": "host-a"}
    changed = {"residency_mode": "ssh", "remote_host_id": "host-b"}
    assert L.config_signature(base) != L.config_signature(changed)


def test_sidecar_lifecycle_masks_host_bearing_inputs():
    sc = L.sidecar_lifecycle()
    # Host-bearing inputs are echoed as presence booleans, never raw values, so
    # the bundle's lifecycle.json carries no private hostname.
    assert isinstance(sc["signature_inputs"]["ollama_host"], bool)
    assert isinstance(sc["signature_inputs"]["remote_host_id"], bool)


def test_bundle_lifecycle_does_not_leak_private_host(tmp_path, monkeypatch):
    from errorta_diagnostics import lifecycle as Lmod
    from errorta_diagnostics.bundle import build_bundle

    monkeypatch.setattr(
        Lmod, "_ollama_host", lambda: "http://gpu-box.private.tailnet.ts.net:11434"
    )

    class _Buf:
        def tail(self, n):
            return []

        def text(self):
            return ""

    dest = tmp_path / "bundle.zip"
    build_bundle(dest, log_buffer=_Buf())
    import zipfile

    with zipfile.ZipFile(dest) as zf:
        for name in zf.namelist():
            assert b"gpu-box.private.tailnet.ts.net" not in zf.read(name), name


def test_config_signature_excludes_volatile_fields():
    # The signature is computed only from collect_signature_inputs(); pid /
    # timestamps are NOT inputs, so two calls in the same process match.
    assert L.config_signature() == L.config_signature()
    sc = L.sidecar_lifecycle()
    assert "pid" in sc  # surfaced for display...
    assert "pid" not in sc["signature_inputs"]  # ...but never in the signature


def test_redacted_log_tail_caps_and_redacts(monkeypatch):
    class _Buf:
        def tail(self, n):
            secret = "Authorization: Bearer sk-ant-SECRETLEAK1234567890"
            return [f"line {i}" for i in range(n)] + [secret]

    out = L.redacted_log_tail(_Buf(), lines=5, max_chars=10_000)
    joined = "\n".join(out["lines"])
    assert "sk-ant-SECRETLEAK1234567890" not in joined
    assert out["redaction_counts"]["tokens"] >= 1


def test_redacted_log_tail_truncates_to_max_chars():
    class _Buf:
        def tail(self, n):
            return ["x" * 50_000]

    out = L.redacted_log_tail(_Buf(), lines=1, max_chars=1000)
    assert out["truncated"] is True
    assert sum(len(line) for line in out["lines"]) <= 1000


def test_runner_lifecycle_record_validates_status():
    rec = L.runner_lifecycle_record(
        run_id="run-1", runner_id="r-1", status="failed",
        exit_code=2, created_at="2026-06-14T00:00:00Z",
    )
    assert rec["format_version"] == L.RUNNER_LIFECYCLE_FORMAT_VERSION
    assert rec["status"] == "failed" and rec["exit_code"] == 2
    with pytest.raises(ValueError):
        L.runner_lifecycle_record(
            run_id="r", runner_id="r", status="nope", created_at="t"
        )


def test_runner_lifecycle_write_is_atomic_and_path_safe(tmp_path):
    rec = L.runner_lifecycle_record(
        run_id="run-1", runner_id="r-1", status="running",
        created_at="2026-06-14T00:00:00Z",
    )
    path = L.write_runner_lifecycle(tmp_path, rec)
    assert path.exists()
    assert json.loads(path.read_text())["status"] == "running"
    bad = dict(rec)
    bad["run_id"] = "../escape"
    with pytest.raises(ValueError):
        L.write_runner_lifecycle(tmp_path, bad)


def test_lifecycle_route_returns_signature_and_redacted_tail():
    from errorta_app.server import app

    # Seed a fake log buffer with a secret to prove the route redacts the tail.
    class _Buf:
        def tail(self, n):
            return ["Bearer sk-ant-ROUTELEAK0987654321"]

        def text(self):
            return ""

    app.state.log_buffer = _Buf()
    client = TestClient(app)
    resp = client.get("/diagnostics/lifecycle")
    assert resp.status_code == 200
    body = resp.json()
    assert body["component"] == "sidecar"
    assert body["config_signature"].startswith("cfg-")
    assert "pid" in body
    tail = body.get("recent_log_tail", {})
    assert "sk-ant-ROUTELEAK0987654321" not in json.dumps(tail)


def test_diagnostic_bundle_includes_lifecycle(tmp_path):
    from errorta_diagnostics.bundle import build_bundle

    class _Buf:
        def tail(self, n):
            return ["boot line", "Bearer sk-ant-BUNDLELEAK1122334455"]

        def text(self):
            return "boot line\nBearer sk-ant-BUNDLELEAK1122334455"

    dest = tmp_path / "bundle.zip"
    result = build_bundle(dest, log_buffer=_Buf())
    assert "lifecycle.json" in result["files"]
    import zipfile

    with zipfile.ZipFile(dest) as zf:
        lifecycle_raw = zf.read("lifecycle.json").decode("utf-8")
    assert "config_signature" in lifecycle_raw
    # No raw secret survives anywhere in the bundle.
    with zipfile.ZipFile(dest) as zf:
        for name in zf.namelist():
            assert b"sk-ant-BUNDLELEAK1122334455" not in zf.read(name), name
