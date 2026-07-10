"""F034-9 — gateway discovery + provider-keys route tests."""
from __future__ import annotations

import stat as _stat

import pytest
from fastapi.testclient import TestClient

from errorta_app.server import app
from errorta_model_gateway.providers.async_base import (
    TestConnectionResult as _ConnResult,
)

_TAURI = {"x-errorta-origin": "tauri-ui"}


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# ----------------------------------------------------------------------
# /gateway/providers
# ----------------------------------------------------------------------


def test_get_gateway_providers_lists_all_registered(client: TestClient) -> None:
    resp = client.get("/gateway/providers")
    assert resp.status_code == 200
    body = resp.json()
    providers = body["providers"]
    classes = sorted(p["provider_class"] for p in providers)
    # All five built-ins should be present.
    assert "anthropic" in classes
    assert "openai" in classes
    assert "google" in classes
    assert "local" in classes
    assert "custom" in classes


def test_get_gateway_providers_marks_local_configured(client: TestClient) -> None:
    """Local + fake never need keys; always 'configured: true'."""
    resp = client.get("/gateway/providers")
    body = resp.json()
    local = next(p for p in body["providers"] if p["provider_class"] == "local")
    assert local["configured"] is True


def test_gateway_providers_marks_anthropic_unconfigured_until_key_set(
    client: TestClient,
) -> None:
    resp = client.get("/gateway/providers")
    body = resp.json()
    anth = next(p for p in body["providers"] if p["provider_class"] == "anthropic")
    assert anth["configured"] is False

    client.put(
        "/provider-keys/anthropic",
        json={"api_key": "sk-ant-test"},
        headers=_TAURI,
    )
    resp = client.get("/gateway/providers")
    body = resp.json()
    anth = next(p for p in body["providers"] if p["provider_class"] == "anthropic")
    assert anth["configured"] is True


def test_subscription_cli_configured_tracks_binary_availability(
    client: TestClient, monkeypatch
) -> None:
    """claude_cli / codex_cli / cursor_cli need no API key — they're 'configured' (and so
    selectable in the room editor) exactly when their binary is installed."""
    from errorta_model_gateway.providers import async_claude_cli, async_codex_cli, async_cursor_cli

    # Binary absent -> greyed out (not configured), no key required.
    monkeypatch.setattr(async_claude_cli, "is_available", lambda **k: False)
    monkeypatch.setattr(async_codex_cli, "is_available", lambda **k: False)
    monkeypatch.setattr(async_cursor_cli, "is_available", lambda **k: False)
    body = client.get("/gateway/providers").json()
    cc = next(p for p in body["providers"] if p["provider_class"] == "claude_cli")
    cx = next(p for p in body["providers"] if p["provider_class"] == "codex_cli")
    cu = next(p for p in body["providers"] if p["provider_class"] == "cursor_cli")
    assert cc["configured"] is False
    assert cx["configured"] is False
    assert cu["configured"] is False

    # Binary present -> configured (selectable), still no key on file.
    monkeypatch.setattr(async_claude_cli, "is_available", lambda **k: True)
    monkeypatch.setattr(async_codex_cli, "is_available", lambda **k: True)
    monkeypatch.setattr(async_cursor_cli, "is_available", lambda **k: True)
    body = client.get("/gateway/providers").json()
    cc = next(p for p in body["providers"] if p["provider_class"] == "claude_cli")
    cx = next(p for p in body["providers"] if p["provider_class"] == "codex_cli")
    cu = next(p for p in body["providers"] if p["provider_class"] == "cursor_cli")
    assert cc["configured"] is True
    assert cx["configured"] is True
    assert cu["configured"] is True


# ----------------------------------------------------------------------
# /gateway/routes
# ----------------------------------------------------------------------


def test_get_gateway_routes_returns_combined_catalog(client: TestClient) -> None:
    resp = client.get("/gateway/routes")
    assert resp.status_code == 200
    body = resp.json()
    routes = body["routes"]
    # At least one anthropic route + one openai route in the catalog.
    assert any(r["route_id"] == "anthropic.claude-sonnet-4-6" for r in routes)
    assert any(r["route_id"] == "openai.gpt-4o" for r in routes)


def test_model_availability_returns_reasoned_route_projection(
    client: TestClient, monkeypatch
) -> None:
    from errorta_council.coding.model_availability import RouteAvailability

    monkeypatch.setattr(
        "errorta_council.coding.model_availability.resolve_route_availability",
        lambda routes: {
            route: RouteAvailability(route, route.split(".", 1)[0], False, "family_disabled")
            for route in routes
        },
    )
    response = client.get("/gateway/model-availability")
    assert response.status_code == 200
    assert response.json()["routes"]
    assert {item["reason"] for item in response.json()["routes"]} == {"family_disabled"}


def test_get_gateway_routes_filtered_by_provider(client: TestClient) -> None:
    resp = client.get("/gateway/routes?provider=anthropic")
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider_class"] == "anthropic"
    assert all(r["route_id"].startswith("anthropic.") for r in body["routes"])


def test_get_gateway_routes_unknown_provider_returns_404(client: TestClient) -> None:
    resp = client.get("/gateway/routes?provider=not-real")
    assert resp.status_code == 404


# ----------------------------------------------------------------------
# /provider-keys (GET)
# ----------------------------------------------------------------------


def test_get_provider_keys_returns_masked_summary(client: TestClient) -> None:
    resp = client.get("/provider-keys")
    assert resp.status_code == 200
    body = resp.json()
    # Empty file → defaults.
    assert body["anthropic"]["configured"] is False
    assert body["openai"]["configured"] is False
    assert body["google"]["configured"] is False
    assert body["custom"] == []


def test_get_provider_keys_never_returns_raw_keys(client: TestClient) -> None:
    """Marquee invariant — even after upsert, GET masks."""
    client.put(
        "/provider-keys/anthropic",
        json={"api_key": "sk-ant-DO-NOT-LEAK-1234"},
        headers=_TAURI,
    )
    resp = client.get("/provider-keys")
    body_text = resp.text
    assert "DO-NOT-LEAK" not in body_text, "raw key leaked in /provider-keys GET"
    body = resp.json()
    assert body["anthropic"]["configured"] is True
    assert body["anthropic"]["key_preview"] == "…1234"


# ----------------------------------------------------------------------
# /provider-keys/{anthropic|openai|google} (PUT/DELETE)
# ----------------------------------------------------------------------


def test_put_anthropic_key_persists(client: TestClient) -> None:
    resp = client.put(
        "/provider-keys/anthropic",
        json={"api_key": "sk-ant-test-1234"},
        headers=_TAURI,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["anthropic"]["configured"] is True
    # Round-trip via GET.
    resp2 = client.get("/provider-keys")
    assert resp2.json()["anthropic"]["key_preview"] == "…1234"


def test_put_openai_key(client: TestClient) -> None:
    resp = client.put(
        "/provider-keys/openai",
        json={"api_key": "sk-openai-abcd"},
        headers=_TAURI,
    )
    assert resp.status_code == 200
    assert resp.json()["openai"]["configured"] is True


def test_put_google_key(client: TestClient) -> None:
    resp = client.put(
        "/provider-keys/google",
        json={"api_key": "goog-key-xyz"},
        headers=_TAURI,
    )
    assert resp.status_code == 200
    assert resp.json()["google"]["configured"] is True


def test_put_unknown_fixed_provider_returns_422(client: TestClient) -> None:
    resp = client.put(
        "/provider-keys/totally-fake",
        json={"api_key": "x"},
        headers=_TAURI,
    )
    assert resp.status_code == 422


def test_put_empty_key_returns_422(client: TestClient) -> None:
    resp = client.put(
        "/provider-keys/anthropic",
        json={"api_key": ""},
        headers=_TAURI,
    )
    assert resp.status_code == 422


def test_delete_fixed_clears_key(client: TestClient) -> None:
    client.put("/provider-keys/anthropic", json={"api_key": "sk-x"}, headers=_TAURI)
    resp = client.delete("/provider-keys/anthropic", headers=_TAURI)
    assert resp.status_code == 200
    assert resp.json()["anthropic"]["configured"] is False


def test_provider_key_mutations_require_tauri_origin(client: TestClient) -> None:
    assert (
        client.put("/provider-keys/anthropic", json={"api_key": "sk-ant-test"}).status_code
        == 403
    )
    assert client.delete("/provider-keys/anthropic").status_code == 403
    assert (
        client.put(
            "/provider-keys/custom",
            json={
                "alias": "lmstudio",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "lm-secret",
                "api_style": "openai_chat_completions",
            },
        ).status_code
        == 403
    )
    assert client.delete("/provider-keys/custom?alias=lmstudio").status_code == 403


# ----------------------------------------------------------------------
# /provider-keys/custom
# ----------------------------------------------------------------------


def test_put_custom_entry_round_trip(client: TestClient) -> None:
    resp = client.put("/provider-keys/custom", json={
        "alias": "lmstudio",
        "base_url": "http://127.0.0.1:1234/v1",
        "api_key": "lm-secret",
        "api_style": "openai_chat_completions",
    }, headers=_TAURI)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["custom"]) == 1
    c = body["custom"][0]
    assert c["alias"] == "lmstudio"
    assert c["configured"] is True
    assert c["api_style"] == "openai_chat_completions"


def test_put_custom_replaces_by_alias(client: TestClient) -> None:
    client.put("/provider-keys/custom", json={
        "alias": "lmstudio", "base_url": "http://old/v1",
        "api_key": "old", "api_style": "openai_chat_completions",
    }, headers=_TAURI)
    client.put("/provider-keys/custom", json={
        "alias": "lmstudio", "base_url": "http://new/v1",
        "api_key": "new", "api_style": "openai_chat_completions",
    }, headers=_TAURI)
    body = client.get("/provider-keys").json()
    assert len(body["custom"]) == 1
    assert body["custom"][0]["base_url"] == "http://new/v1"


def test_put_custom_unknown_api_style_returns_422(client: TestClient) -> None:
    resp = client.put("/provider-keys/custom", json={
        "alias": "x", "base_url": "http://x/v1",
        "api_key": "k", "api_style": "not-a-style",
    }, headers=_TAURI)
    assert resp.status_code == 422


def test_delete_custom_clears_alias(client: TestClient) -> None:
    client.put("/provider-keys/custom", json={
        "alias": "lmstudio", "base_url": "http://x/v1",
        "api_key": "k", "api_style": "openai_chat_completions",
    }, headers=_TAURI)
    resp = client.delete("/provider-keys/custom?alias=lmstudio", headers=_TAURI)
    assert resp.status_code == 200
    assert resp.json()["custom"] == []


# ----------------------------------------------------------------------
# F040-01 — CLI status / binary override / login metadata
# ----------------------------------------------------------------------


def _exec_file(tmp_path, name: str) -> str:
    p = tmp_path / name
    p.write_text("#!/bin/sh\necho stub\n", encoding="utf-8")
    p.chmod(p.stat().st_mode | _stat.S_IXUSR | _stat.S_IXGRP | _stat.S_IXOTH)
    return str(p)


def _patch_cheap_detect(monkeypatch, *, installed: bool = True) -> None:
    """Force resolve_details to a deterministic cheap result for all 3 CLIs.

    Crucially this NEVER calls cli_version or test_connection — proving the
    status path runs no billable probe.
    """
    from errorta_model_gateway.providers import (
        async_claude_cli,
        async_codex_cli,
        async_cursor_cli,
    )

    def _details(provider):
        def _impl(self, *, override_path=None):
            if not installed:
                return {
                    "provider": provider, "state": "not_installed", "found": False,
                    "path": "", "name_used": "", "source": "", "version": "",
                    "login": "", "detail": "",
                }
            return {
                "provider": provider, "state": "installed", "found": True,
                "path": override_path or f"/x/{provider}", "name_used": provider,
                "source": "override_settings" if override_path else "path",
                "version": "9.9.9", "login": "", "detail": "",
            }
        return _impl

    monkeypatch.setattr(
        async_claude_cli.ClaudeCliHandler, "resolve_details", _details("claude_cli")
    )
    monkeypatch.setattr(
        async_codex_cli.CodexCliHandler, "resolve_details", _details("codex_cli")
    )
    monkeypatch.setattr(
        async_cursor_cli.CursorCliHandler, "resolve_details", _details("cursor_cli")
    )


def _ban_billable_probe(monkeypatch) -> None:
    """Make test_connection raise if ever invoked — the auto/detect paths must
    never run the billable probe."""
    from errorta_model_gateway.providers import (
        async_claude_cli,
        async_codex_cli,
        async_cursor_cli,
    )

    async def _boom(self, *, api_key=None):
        raise AssertionError("billable test_connection was invoked on a cheap path")

    for cls in (
        async_claude_cli.ClaudeCliHandler,
        async_codex_cli.CodexCliHandler,
        async_cursor_cli.CursorCliHandler,
    ):
        monkeypatch.setattr(cls, "test_connection", _boom)


def test_cli_status_is_cheap_no_billable_probe(client, monkeypatch) -> None:
    _patch_cheap_detect(monkeypatch, installed=True)
    _ban_billable_probe(monkeypatch)
    resp = client.get("/gateway/providers/claude_cli/cli-status", headers=_TAURI)
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "installed"
    assert body["found"] is True
    assert body["version"] == "9.9.9"
    # never probed -> connected is null
    assert body["connected"] is None
    assert body["verified_at"] is None


def test_cli_status_requires_tauri_origin(client) -> None:
    assert client.get("/gateway/providers/claude_cli/cli-status").status_code == 403


def test_cli_status_unknown_provider_404(client) -> None:
    resp = client.get("/gateway/providers/not-a-cli/cli-status", headers=_TAURI)
    assert resp.status_code == 404


def test_providers_list_exposes_connected_null_until_probed(client, monkeypatch) -> None:
    _patch_cheap_detect(monkeypatch, installed=True)
    _ban_billable_probe(monkeypatch)
    body = client.get("/gateway/providers").json()
    cc = next(p for p in body["providers"] if p["provider_class"] == "claude_cli")
    assert cc["configured"] is True
    assert cc["connected"] is None  # never probed


def test_test_route_caches_probe_then_surfaced(client, monkeypatch) -> None:
    _patch_cheap_detect(monkeypatch, installed=True)
    from errorta_model_gateway.providers import async_claude_cli

    async def _ok(self, *, api_key=None):
        return _ConnResult(True, "subscription CLI ready", 7)

    monkeypatch.setattr(async_claude_cli.ClaudeCliHandler, "test_connection", _ok)

    # Explicit Test -> runs probe, caches connected=True.
    r = client.post("/provider-keys/claude_cli/test", headers=_TAURI)
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # cli-status now reflects the cached probe.
    status = client.get("/gateway/providers/claude_cli/cli-status", headers=_TAURI).json()
    assert status["connected"] is True
    assert status["verified_at"] is not None

    # /gateway/providers also surfaces the cached connected flag.
    body = client.get("/gateway/providers").json()
    cc = next(p for p in body["providers"] if p["provider_class"] == "claude_cli")
    assert cc["connected"] is True


def test_test_connection_routes_require_tauri_origin(client) -> None:
    assert client.post("/provider-keys/local/test").status_code == 403
    assert client.post("/provider-keys/custom/test?alias=lmstudio").status_code == 403


def test_put_cli_binary_requires_tauri_origin(client, tmp_path) -> None:
    binary = _exec_file(tmp_path, "claude")
    resp = client.put("/provider-keys/claude_cli/cli-binary", json={"path": binary})
    assert resp.status_code == 403


def test_put_cli_binary_validates_and_persists(client, monkeypatch, tmp_path) -> None:
    _patch_cheap_detect(monkeypatch, installed=True)
    _ban_billable_probe(monkeypatch)
    binary = _exec_file(tmp_path, "claude")
    resp = client.put(
        "/provider-keys/claude_cli/cli-binary", json={"path": binary}, headers=_TAURI
    )
    assert resp.status_code == 200
    # Persisted into settings.
    from errorta_app import settings as _settings

    assert _settings.get_cli_binary("claude_cli") == binary

    # DELETE clears it (Tauri-guarded).
    assert client.delete("/provider-keys/claude_cli/cli-binary").status_code == 403
    d = client.delete("/provider-keys/claude_cli/cli-binary", headers=_TAURI)
    assert d.status_code == 200
    assert _settings.get_cli_binary("claude_cli") is None


def test_put_cli_binary_rejects_non_executable(client, tmp_path) -> None:
    plain = tmp_path / "plain"
    plain.write_text("x", encoding="utf-8")
    plain.chmod(0o644)
    resp = client.put(
        "/provider-keys/claude_cli/cli-binary",
        json={"path": str(plain)},
        headers=_TAURI,
    )
    assert resp.status_code == 422


def test_put_cli_binary_rejects_missing_file(client, tmp_path) -> None:
    resp = client.put(
        "/provider-keys/claude_cli/cli-binary",
        json={"path": str(tmp_path / "nope")},
        headers=_TAURI,
    )
    assert resp.status_code == 422


def test_put_cli_binary_rejects_relative_path(client) -> None:
    resp = client.put(
        "/provider-keys/claude_cli/cli-binary",
        json={"path": "relative/claude"},
        headers=_TAURI,
    )
    assert resp.status_code == 422


def test_put_cli_binary_unknown_provider_404(client, tmp_path) -> None:
    binary = _exec_file(tmp_path, "x")
    resp = client.put(
        "/provider-keys/not-a-cli/cli-binary", json={"path": binary}, headers=_TAURI
    )
    assert resp.status_code == 404


def test_login_command_shape(client, monkeypatch) -> None:
    # claude: subcommand is `setup-token` (NOT `login`, which isn't a real
    # subcommand); when the binary resolves, the bare name is replaced with the
    # absolute path so the copied command runs even when ~/.local/bin is off the
    # user's shell PATH.
    from errorta_model_gateway.providers import async_claude_cli, async_codex_cli

    monkeypatch.setattr(
        async_claude_cli, "resolve_claude_binary", lambda: "/Users/x/.local/bin/claude"
    )
    body = client.get("/provider-keys/claude_cli/login-command", headers=_TAURI).json()
    assert body["login_argv"] == ["/Users/x/.local/bin/claude", "setup-token"]
    assert body["install_url"].startswith("http")
    assert isinstance(body["install_command"], str) and body["install_command"]

    # claude: when the binary can't be resolved, fall back to the bare name but
    # KEEP the corrected `setup-token` subcommand.
    monkeypatch.setattr(async_claude_cli, "resolve_claude_binary", lambda: None)
    body2 = client.get("/provider-keys/claude_cli/login-command", headers=_TAURI).json()
    assert body2["login_argv"] == ["claude", "setup-token"]

    # codex: `login` is valid; the resolved absolute path is substituted too.
    monkeypatch.setattr(
        async_codex_cli, "resolve_codex_binary", lambda: "/Applications/Codex.app/codex"
    )
    cx = client.get("/provider-keys/codex_cli/login-command", headers=_TAURI).json()
    assert cx["login_argv"] == ["/Applications/Codex.app/codex", "login"]

    # cursor: with nothing resolvable, fall back to the static ["agent","login"].
    from errorta_model_gateway.providers import async_cursor_cli

    monkeypatch.setattr(
        async_cursor_cli, "resolve_cursor_command_detailed", lambda **_: None
    )
    cu = client.get("/provider-keys/cursor_cli/login-command", headers=_TAURI).json()
    assert cu["login_argv"] == ["agent", "login"]


def test_login_command_claude_and_codex_honor_saved_binary_override(
    client, monkeypatch, tmp_path
) -> None:
    """Claude/Codex copy commands should match the Settings-selected binary."""
    from errorta_app import settings as _settings
    from errorta_model_gateway.providers import async_claude_cli, async_codex_cli

    claude_override = _exec_file(tmp_path, "claude-override")
    codex_override = _exec_file(tmp_path, "codex-override")
    _settings.set_cli_binary("claude_cli", claude_override)
    _settings.set_cli_binary("codex_cli", codex_override)
    monkeypatch.setattr(
        async_claude_cli, "resolve_claude_binary", lambda: "/ignored/claude"
    )
    monkeypatch.setattr(
        async_codex_cli, "resolve_codex_binary", lambda: "/ignored/codex"
    )

    cc = client.get("/provider-keys/claude_cli/login-command", headers=_TAURI).json()
    cx = client.get("/provider-keys/codex_cli/login-command", headers=_TAURI).json()

    assert cc["login_argv"] == [claude_override, "setup-token"]
    assert cx["login_argv"] == [codex_override, "login"]


def test_login_command_cursor_launcher_argv(client, monkeypatch) -> None:
    """The app-bundle ``cursor`` launcher → ``cursor agent login`` copy-fallback."""
    from errorta_model_gateway.providers import async_cursor_cli

    launcher = "/Applications/Cursor.app/Contents/Resources/app/bin/cursor"
    monkeypatch.setattr(
        async_cursor_cli,
        "resolve_cursor_command_detailed",
        lambda **_: (async_cursor_cli.CursorCommand([launcher, "agent"], launcher), "path"),
    )
    cu = client.get("/provider-keys/cursor_cli/login-command", headers=_TAURI).json()
    assert cu["login_argv"] == [launcher, "agent", "login"]


def test_login_command_cursor_direct_argv(client, monkeypatch) -> None:
    """A direct ``agent`` install → ``<path> login`` copy-fallback."""
    from errorta_model_gateway.providers import async_cursor_cli

    path = "/opt/homebrew/bin/agent"
    monkeypatch.setattr(
        async_cursor_cli,
        "resolve_cursor_command_detailed",
        lambda **_: (async_cursor_cli.CursorCommand([path], path), "path"),
    )
    cu = client.get("/provider-keys/cursor_cli/login-command", headers=_TAURI).json()
    assert cu["login_argv"] == [path, "login"]


def test_login_command_cursor_honors_saved_binary_override(client, tmp_path) -> None:
    """The copy-fallback command must match the user's explicit binary override."""
    from errorta_app import settings as _settings

    override = _exec_file(tmp_path, "cursor")
    _settings.set_cli_binary("cursor_cli", override)

    cu = client.get("/provider-keys/cursor_cli/login-command", headers=_TAURI).json()
    assert cu["login_argv"] == [override, "agent", "login"]


def test_login_command_requires_tauri_origin(client) -> None:
    assert client.get("/provider-keys/claude_cli/login-command").status_code == 403


def test_login_command_unknown_provider_404(client) -> None:
    assert (
        client.get("/provider-keys/not-a-cli/login-command", headers=_TAURI).status_code
        == 404
    )


def test_no_token_shaped_string_in_cli_responses(client, monkeypatch) -> None:
    """A token-shaped string from probe output must never appear in any
    CLI-status / probe response."""
    _patch_cheap_detect(monkeypatch, installed=True)
    from errorta_model_gateway.providers import async_claude_cli

    leak = "sk-ant-aaaaaaaaaaaaaaaaaaaaaaaa"

    async def _leaky(self, *, api_key=None):
        return _ConnResult(False, f"login failed {leak}", 9)

    monkeypatch.setattr(async_claude_cli.ClaudeCliHandler, "test_connection", _leaky)

    probe = client.post("/provider-keys/claude_cli/test", headers=_TAURI)
    status = client.get("/gateway/providers/claude_cli/cli-status", headers=_TAURI)
    providers = client.get("/gateway/providers")
    for resp in (probe, status, providers):
        assert leak not in resp.text
