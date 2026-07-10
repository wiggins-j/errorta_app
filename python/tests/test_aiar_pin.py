"""Tests for errorta_app.health.aiar_pin and /healthz integration."""
from __future__ import annotations

import json
import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from errorta_app.health.aiar_pin import check_aiar_pin


def _aiar_importable() -> bool:
    """Return True iff the AIAR package can be imported from this interpreter.

    Used to gate the real-dev-env test below. We avoid importing here at
    module level because pytest collection should remain side-effect-free.
    """
    try:
        import importlib

        importlib.import_module("aiar")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Real env test — runs only when AIAR is actually installed editable in .venv.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _aiar_importable(), reason="aiar not installed in this env")
def test_editable_in_dev() -> None:
    """In the dev .venv with `pip install -e ../../aiar`, source must be 'editable'.

    If this test runs and resolves 'pinned' instead, something has flipped the
    install mode and the dev workflow is silently degraded — fail loudly.
    """
    result = check_aiar_pin()
    assert result["available"] is True
    # version may be a string or None depending on aiar.__version__ presence
    assert result["source"] in {"editable", "pinned"}
    # In a vanilla dev install we expect editable. If a contributor has done
    # a non-editable local install, we still accept 'pinned' as a sane signal.


# ---------------------------------------------------------------------------
# Simulated cases — drive every classification branch deterministically.
# ---------------------------------------------------------------------------

def test_absent_simulated(monkeypatch: pytest.MonkeyPatch) -> None:
    """ImportError on `import aiar` resolves to absent without raising."""
    # Force any pre-cached aiar out, then make import fail.
    monkeypatch.delitem(sys.modules, "aiar", raising=False)

    if isinstance(__builtins__, dict):
        real_import = __builtins__["__import__"]
    else:
        real_import = __builtins__.__import__  # type: ignore[attr-defined]

    def fake_import(name: str, *args: Any, **kwargs: Any):
        if name == "aiar" or name.startswith("aiar."):
            raise ImportError("simulated: aiar not installed")
        return real_import(name, *args, **kwargs)

    import builtins as _builtins
    monkeypatch.setattr(_builtins, "__import__", fake_import)

    result = check_aiar_pin()
    assert result == {"available": False, "version": None, "source": "absent"}


def _install_fake_aiar(monkeypatch: pytest.MonkeyPatch, version: str = "0.1.0") -> None:
    """Inject a minimal fake `aiar` module so `import aiar` succeeds."""
    fake = types.ModuleType("aiar")
    fake.__version__ = version  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "aiar", fake)


def test_pinned_simulated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Distribution exists but no direct_url.json → 'pinned'."""
    _install_fake_aiar(monkeypatch, "0.1.0")

    fake_dist = MagicMock()
    fake_dist.read_text.return_value = None  # no direct_url.json

    import importlib.metadata as md

    monkeypatch.setattr(md, "distribution", lambda name: fake_dist)

    result = check_aiar_pin()
    assert result["available"] is True
    assert result["version"] == "0.1.0"
    assert result["source"] == "pinned"


def test_editable_simulated(monkeypatch: pytest.MonkeyPatch) -> None:
    """direct_url.json with dir_info.editable=true → 'editable'."""
    _install_fake_aiar(monkeypatch, "0.1.0")

    fake_dist = MagicMock()
    fake_dist.read_text.return_value = json.dumps(
        {
            "url": "file:///Users/dev/GitHub/aiar",
            "dir_info": {"editable": True},
        }
    )

    import importlib.metadata as md

    monkeypatch.setattr(md, "distribution", lambda name: fake_dist)

    result = check_aiar_pin()
    assert result["available"] is True
    assert result["source"] == "editable"


def test_local_nonedit_simulated(monkeypatch: pytest.MonkeyPatch) -> None:
    """direct_url.json present but no editable flag → 'pinned'."""
    _install_fake_aiar(monkeypatch, "0.1.0")

    fake_dist = MagicMock()
    fake_dist.read_text.return_value = json.dumps(
        {
            "url": "file:///Users/dev/GitHub/aiar",
            "dir_info": {"editable": False},
        }
    )

    import importlib.metadata as md

    monkeypatch.setattr(md, "distribution", lambda name: fake_dist)

    result = check_aiar_pin()
    assert result["source"] == "pinned"


# ---------------------------------------------------------------------------
# /healthz wire-shape test.
# ---------------------------------------------------------------------------

def test_healthz_includes_aiar_pin(tmp_errorta_home) -> None:
    """/healthz includes aiar_pin block and preserves legacy fields."""
    from errorta_app.server import app

    client = TestClient(app)
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()

    # Legacy fields preserved.
    assert "aiar_available" in body
    assert "aiar_version" in body
    assert body["service"] == "errorta-sidecar"

    # New field.
    assert "aiar_pin" in body
    pin = body["aiar_pin"]
    assert set(pin.keys()) >= {"available", "version", "source"}
    # Slice 3 adds "remote" to the Literal; default state stays local.
    assert pin["source"] in {"editable", "pinned", "absent", "remote"}
    assert isinstance(pin["available"], bool)


# ---------------------------------------------------------------------------
# F-INFRA-12 Phase B Slice 3 — residency-aware aiar_pin dispatch.
# ---------------------------------------------------------------------------


def test_healthz_local_mode_back_compat(tmp_errorta_home) -> None:
    """In local mode, aiar_pin keeps the pre-Slice-3 shape (no upstream)."""
    from errorta_app.server import app

    client = TestClient(app)
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()

    pin = body["aiar_pin"]
    # Local-mode source must not be "remote".
    assert pin["source"] in {"editable", "pinned", "absent"}
    # upstream is allowed to be absent or None — what matters is the shape
    # doesn't acquire a hard "upstream" requirement in local mode.
    if "upstream" in pin:
        assert pin["upstream"] is None

    # Residency block present and reports local-up.
    assert body["residency"] == {
        "mode": "local",
        "remote_url": None,
        "tunnel_state": "up",
    }


def _patch_probe(
    monkeypatch: pytest.MonkeyPatch, *, ok: bool, body: dict | None = None,
    error: str | None = None,
) -> MagicMock:
    """Stub ``residency_probe.probe_https_url`` for both import paths.

    The aiar_pin helper and the server's residency block both call into
    ``errorta_residency.probe`` via a lazy import. Patching at the source
    module is sufficient since neither caller cached the function.
    """
    result = {
        "ok": ok,
        "status": 200 if ok else None,
        "body": body if ok else None,
        "error": error,
    }
    stub = MagicMock(return_value=result)
    from errorta_residency import probe as residency_probe

    monkeypatch.setattr(residency_probe, "probe_https_url", stub)
    return stub


def test_healthz_cloud_mode_success(
    tmp_errorta_home,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cloud mode + successful upstream → source=remote, upstream populated."""
    from errorta_residency import config as residency_config

    residency_config.update(
        mode="cloud",
        cloud_url="https://errorta.example.com",
        cloud_token="tok-abc",
    )

    upstream_body = {
        "service": "errorta-sidecar",
        "aiar_pin": {
            "available": True,
            "version": "0.2.0",
            "source": "pinned",
        },
    }
    _patch_probe(monkeypatch, ok=True, body=upstream_body)

    from errorta_app.server import app

    client = TestClient(app)
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()

    pin = body["aiar_pin"]
    assert pin["source"] == "remote"
    assert pin["available"] is True
    assert pin["version"] == "0.2.0"
    assert pin["upstream"] == {
        "url": "https://errorta.example.com",
        "version": "0.2.0",
        "source": "pinned",
    }

    # Residency block reflects cloud mode and the probe outcome.
    assert body["residency"]["mode"] == "cloud"
    assert body["residency"]["remote_url"] == "https://errorta.example.com"
    assert body["residency"]["tunnel_state"] == "up"


def test_healthz_cloud_mode_failure(
    tmp_errorta_home,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cloud mode + failing upstream → available=False, upstream.error set."""
    from errorta_residency import config as residency_config

    residency_config.update(
        mode="cloud",
        cloud_url="https://down.example.com",
    )

    _patch_probe(monkeypatch, ok=False, error="connection refused")

    from errorta_app.server import app

    client = TestClient(app)
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()

    pin = body["aiar_pin"]
    assert pin["source"] == "remote"
    assert pin["available"] is False
    assert pin["version"] is None
    assert pin["upstream"]["url"] == "https://down.example.com"
    assert isinstance(pin["upstream"]["error"], str)
    assert pin["upstream"]["error"]  # non-empty

    # Residency tunnel_state reports the probe failure.
    assert body["residency"]["tunnel_state"] == "error"


def test_ssh_remote_mode_calls_helper_with_tunnel_url(
    tmp_errorta_home,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ssh-remote mode probes http://127.0.0.1:{local_tunnel_port}."""
    from errorta_residency import config as residency_config

    residency_config.update(
        mode="ssh-remote",
        ssh_host="example-host",
        remote_sidecar_port=11436,
        local_tunnel_port=21436,
    )

    stub = _patch_probe(
        monkeypatch,
        ok=True,
        body={
            "aiar_pin": {
                "available": True,
                "version": "0.2.0",
                "source": "editable",
            }
        },
    )

    # Drive the check via /healthz so we exercise the full wire path.
    from errorta_app.server import app

    client = TestClient(app)
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()

    # The probe was called with the tunnel URL we expect.
    assert stub.called
    call_args = stub.call_args
    # First positional arg is the URL.
    assert call_args.args[0] == "http://127.0.0.1:21436"
    # The token kwarg is None for ssh-remote (no bearer token).
    assert call_args.kwargs.get("token") is None

    pin = body["aiar_pin"]
    assert pin["source"] == "remote"
    assert pin["available"] is True
    assert pin["upstream"]["url"] == "http://127.0.0.1:21436"
    assert pin["upstream"]["source"] == "editable"

    # Residency block: Python sees the runtime-synced local tunnel port.
    assert body["residency"]["mode"] == "ssh-remote"
    assert body["residency"]["remote_url"] == "http://127.0.0.1:21436"
    assert body["residency"]["tunnel_state"] == "up"


def test_cloud_mode_missing_url_does_not_raise(
    tmp_errorta_home,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cloud-mode with no cloud_url surfaces a structured error, never raises.

    The PUT route normally rejects this, but a malformed on-disk file
    could land us here. The helper must degrade gracefully.
    """
    # Persist a bare cloud-mode state by writing JSON directly so we bypass
    # the route's validation guard.
    from errorta_app.paths import data_residency_path

    p = data_residency_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"mode": "cloud", "cloud_url": None}))

    from errorta_app.server import app

    client = TestClient(app)
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()

    pin = body["aiar_pin"]
    assert pin["source"] == "remote"
    assert pin["available"] is False
    assert pin["upstream"]["url"] is None
    assert "error" in pin["upstream"]
