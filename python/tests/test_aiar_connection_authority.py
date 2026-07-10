"""F116 — canonical AIAR connection authority tests."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import httpx

from errorta_aiar_connection.config import AiarConnectionConfig
from errorta_aiar_connection.models import AiarCapabilities, AiarRuntime
from errorta_aiar_connection.resolver import resolve_aiar_runtime
from errorta_aiar_connection.status import probe_aiar_service


class _FakeClient:
    def __init__(self, routes: dict[str, httpx.Response]) -> None:
        self._routes = routes

    def __call__(self, *args, **kwargs):  # noqa: ANN002, ANN003 - httpx shim
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        return None

    def get(self, url: str) -> httpx.Response:
        path = urlparse(url).path
        return self._routes.get(path, httpx.Response(404, json={"detail": "missing"}))


def test_probe_aiar_service_reports_runtime_without_token(monkeypatch) -> None:
    fake = _FakeClient(
        {
            "/healthz": httpx.Response(
                200,
                json={
                    "active_model": "qwen3.5:9b",
                    "active_model_ready": True,
                    "rag": {"instance_count": 12, "remote_ingest": True},
                    "pure_retrieve": True,
                },
            ),
            "/capabilities": httpx.Response(
                200,
                json={"features": {"generation": True, "judge": True}},
            ),
            "/services/meta": httpx.Response(
                200,
                json={
                    "active_model": "qwen3.5:9b",
                    "active_model_ready": True,
                    "available_models": ["qwen3.5:9b", "llama3.1:8b"],
                },
            ),
            "/instances": httpx.Response(
                200,
                json={"instances": [{"name": "alpha"}, {"name": "beta"}]},
            ),
        }
    )
    monkeypatch.setattr(httpx, "Client", fake)

    runtime = probe_aiar_service(
        AiarConnectionConfig(
            kind="aiar-service",
            display_name="example-host",
            base_url="http://example-host.local:8766",
            token="secret-token",
        ),
        config_source="test",
    )

    assert runtime.kind == "aiar-service"
    assert runtime.connected is True
    assert runtime.display_name == "example-host"
    assert runtime.active_model == "qwen3.5:9b"
    assert runtime.active_model_ready is True
    assert runtime.corpus_count == 12
    assert runtime.capabilities.answer is True
    assert runtime.capabilities.judge is True
    public = json.dumps(runtime.to_public_dict())
    assert "secret-token" not in public
    assert '"token_configured": true' in public


def test_ambiguous_legacy_sources_fail_loud(monkeypatch, tmp_errorta_home) -> None:
    from errorta_aiar_connection import resolver

    monkeypatch.setattr(
        resolver,
        "_legacy_remote_config",
        lambda: AiarConnectionConfig(
            kind="aiar-service",
            base_url="http://example-host.local:8766",
            token="remote-token",
        ),
    )
    monkeypatch.setattr(
        resolver,
        "_legacy_residency_config",
        lambda: AiarConnectionConfig(
            kind="errorta-sidecar-remote",
            base_url="http://127.0.0.1:9999",
        ),
    )

    runtime = resolve_aiar_runtime()

    assert runtime.kind == "disconnected"
    assert runtime.error_code == "ambiguous_legacy"
    assert runtime.config_source == "ambiguous_legacy"


def test_active_remote_adapter_does_not_probe_network(monkeypatch, tmp_errorta_home) -> None:
    """The routing decision in active_remote_adapter reads config only — it must
    NOT probe the network (a slow/unreachable backend would otherwise stall
    corpus listing / retrieval). Locks the F116-review hot-path fix."""
    from errorta_aiar_connection import resolver
    from errorta_aiar_connection.config import save_canonical
    from errorta_aiar_connection.resolver import resolve_aiar_config
    from errorta_project_grounding.remote_adapter import active_remote_adapter

    save_canonical(
        AiarConnectionConfig(
            kind="aiar-service",
            base_url="http://example-host.local:8766",
            token="secret",
        )
    )

    def _boom(*args, **kwargs):  # any probe is a regression
        raise AssertionError("active_remote_adapter must not probe the network")

    monkeypatch.setattr(resolver, "probe_aiar_service", _boom)
    monkeypatch.setattr(resolver, "probe_remote_sidecar", _boom)
    monkeypatch.setattr(resolver, "probe_local_aiar", _boom)

    config, source = resolve_aiar_config()
    assert source == "canonical"
    assert config is not None and config.kind == "aiar-service"

    adapter = active_remote_adapter()
    assert adapter is not None
    assert adapter._cfg.base_url == "http://example-host.local:8766"


def test_diagnostic_bundle_includes_token_safe_runtime(
    monkeypatch,
    tmp_errorta_home: Path,
    tmp_path: Path,
) -> None:
    from errorta_diagnostics.bundle import build_bundle

    runtime = AiarRuntime(
        kind="aiar-service",
        display_name="example-host",
        connected=True,
        base_url="http://private-aiar.example:8766",
        token="super-secret-token",
        backend_id="http://private-aiar.example:8766",
        active_model="qwen3.5:9b",
        active_model_ready=True,
        corpus_count=12,
        capabilities=AiarCapabilities(answer=True, judge=True, pure_retrieve=True),
        config_source="canonical",
        status_source="healthz",
    )
    monkeypatch.setattr("errorta_aiar_connection.resolve_aiar_runtime", lambda: runtime)

    dest = tmp_path / "diag.zip"
    result = build_bundle(dest)

    assert "aiar-runtime.json" in result["files"]
    with zipfile.ZipFile(dest) as zf:
        payload = json.loads(zf.read("aiar-runtime.json"))
        bundle_text = "\n".join(
            zf.read(name).decode("utf-8", errors="replace") for name in zf.namelist()
        )

    assert payload["runtime_kind"] == "aiar-service"
    assert payload["connected"] is True
    assert payload["backend_id"] == "<redacted-url>"
    assert payload["token_configured"] is True
    assert "super-secret-token" not in bundle_text
    assert "private-aiar.example" not in bundle_text
