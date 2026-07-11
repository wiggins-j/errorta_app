"""S4 — connect + team config + wizard (F147 §7, §14).

Grounded against the real routes in ``routes/gateway.py`` (provider-keys + gateway),
``routes/coding.py`` (wizard + run-setup), ``routes/council.py`` (rooms). The
sidecar is never booted: HTTP is a ``RouteClient`` fake or a real ``SidecarClient``
over ``httpx.MockTransport``. The autouse ``_neutralize_sole_owner_guard`` fixture
(conftest) pins the guard to a no-op; tests that assert the guard is *invoked*
re-``setattr`` a spy over it.

The marquee safety property (§14, golden invariant #4): an API key is NEVER passed
as a CLI argument and NEVER reaches stdout / stderr / logs / the rendered output —
only the server-returned mask (``…<last4>``) is shown.
"""
from __future__ import annotations

import json
import logging

import httpx
import pytest

from errorta_cli import registry, teamdraft
from errorta_cli.client import ORIGIN_HEADER, ORIGIN_VALUE, SidecarClient
from errorta_cli.errors import CliError

from .conftest import RouteClient

PID = "proj-1"
SENTINEL = "sk-ant-SENTINEL-DO-NOT-LEAK-9999"


def _mock_client(handler) -> SidecarClient:
    return SidecarClient("http://127.0.0.1:9", transport=httpx.MockTransport(handler))


def _key_file(tmp_path, value: str = SENTINEL):
    p = tmp_path / "key.txt"
    p.write_text(value + "\n", encoding="utf-8")
    return str(p)


# --------------------------------------------------------------------------- #
# 1. §14 — the API key NEVER appears in argv / stdout / stderr / logs (#4).
# --------------------------------------------------------------------------- #

def test_connect_key_never_leaks(make_ctx, tmp_path, capsys, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    key_file = _key_file(tmp_path)
    argv = ["anthropic", "api", "--key-file", key_file, "--yes"]

    seen_bodies: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        # The key travels ONLY in the PUT body (to the store the user chose).
        seen_bodies.append(request.content.decode() if request.content else "")
        if request.url.path.endswith("/test"):
            return httpx.Response(200, json={"ok": True, "detail": "ok"})
        return httpx.Response(200, json={
            "anthropic": {"configured": True, "key_preview": "…9999"}, "custom": []})

    with _mock_client(handler) as client:
        _payload, text = registry.dispatch("connect", client, make_ctx(), argv)

    # 1) never in argv (the CLI reads it from the file, not the command line).
    assert SENTINEL not in argv
    # 2) never in the rendered output.
    assert SENTINEL not in text
    # 3) never on stdout / stderr.
    out = capsys.readouterr()
    assert SENTINEL not in out.out and SENTINEL not in out.err
    # 4) never in any log record.
    assert all(SENTINEL not in r.getMessage() for r in caplog.records)
    # 5) it DID reach the PUT body exactly once (the write actually happened).
    assert sum(SENTINEL in b for b in seen_bodies) == 1


def test_connect_refuses_key_as_argument() -> None:
    """There is no ``--key`` value option — a key is never an argv token."""
    connect = registry.get("connect")
    assert connect is not None
    assert not any(p.name == "key" for p in connect.params)
    # The only key source is the file path (never the secret itself).
    assert any(p.name == "key-file" for p in connect.params)


def test_connect_non_interactive_without_key_file_refuses(make_ctx, monkeypatch) -> None:
    # No --key-file + non-interactive (pytest stdio) → refuse before any getpass.
    monkeypatch.setattr(
        "errorta_cli.commands._mutate.is_interactive", lambda: False)
    client = RouteClient()
    with pytest.raises(CliError) as ei:
        registry.dispatch("connect", client, make_ctx(),
                          ["anthropic", "api", "--yes"])
    assert ei.value.code in ("key_required",)
    # A key write must not have fired.
    assert not any(m == "PUT" for m, _ in client.calls)


# --------------------------------------------------------------------------- #
# 2. connect {provider} api → PUT then /test, in that order, with origin header.
# --------------------------------------------------------------------------- #

def test_connect_api_puts_then_tests_in_order(make_ctx, tmp_path) -> None:
    calls: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path,
                      request.headers.get(ORIGIN_HEADER)))
        if request.url.path.endswith("/test"):
            return httpx.Response(200, json={"ok": True, "detail": "ok"})
        return httpx.Response(200, json={"anthropic": {"configured": True,
                                                       "key_preview": "…9999"}})

    with _mock_client(handler) as client:
        registry.dispatch("connect", client, make_ctx(),
                          ["openai", "api", "--key-file", _key_file(tmp_path), "--yes"])

    # PUT the key first, THEN POST /test to warm the `connected` probe.
    assert [(m, p) for m, p, _ in calls] == [
        ("PUT", "/provider-keys/openai"),
        ("POST", "/provider-keys/openai/test"),
    ]
    # The origin header rides on both (invariant #2).
    assert all(origin == ORIGIN_VALUE for _, _, origin in calls)


# --------------------------------------------------------------------------- #
# 3. connect claudecode cli → detect + /test populates the `connected` cache.
# --------------------------------------------------------------------------- #

def test_connect_cli_triggers_test_to_populate_connected(make_ctx) -> None:
    client = RouteClient(responses={
        "/gateway/providers/claude_cli/cli-status": {"source": "path",
                                                      "binary": "/x/claude"},
        "/provider-keys/claude_cli/test": {"ok": True, "detail": "ok",
                                           "state": "connected"},
    })
    _payload, text = registry.dispatch("connect", client, make_ctx(),
                                       ["claudecode", "cli"])
    assert ("GET", "/gateway/providers/claude_cli/cli-status") in client.calls
    # The /test POST is the ONLY way to populate the fail-closed `connected` cache.
    assert ("POST", "/provider-keys/claude_cli/test") in client.calls
    assert "connected" in text


def test_connect_cli_binary_put_is_guarded_and_gated(make_ctx, tmp_path, monkeypatch) -> None:
    spy: list = []
    monkeypatch.setattr("errorta_cli.commands._mutate.require_sole_owner",
                        lambda home, handle: spy.append((home, handle)))
    client = RouteClient(default={"source": "override"})
    registry.dispatch("connect", client, make_ctx(),
                      ["codex", "cli", "--binary", "/usr/bin/codex", "--yes"])
    assert ("PUT", "/provider-keys/codex_cli/cli-binary") in client.calls
    assert spy, "sole-owner guard not invoked for a cli-binary write"


def test_connect_cli_binary_requires_yes_non_interactive(make_ctx) -> None:
    client = RouteClient()
    with pytest.raises(CliError) as ei:
        registry.dispatch("connect", client, make_ctx(),
                          ["cursor", "cli", "--binary", "/usr/bin/agent"])
    assert ei.value.code == "confirmation_required"
    assert client.calls == []


def test_connect_cli_shows_login_command(make_ctx) -> None:
    client = RouteClient(responses={
        "/cli-status": {"source": "path"},
        "/login-command": {"login_argv": ["claude", "setup-token"],
                           "install_command": "npm i -g x"},
        "/test": {"ok": False, "detail": "logged out", "state": "logged_out"},
    })
    _payload, text = registry.dispatch("connect", client, make_ctx(),
                                       ["claudecode", "cli", "--login"])
    assert ("GET", "/provider-keys/claude_cli/login-command") in client.calls
    assert "setup-token" in text


# --------------------------------------------------------------------------- #
# 4. connect status → gateway providers + masked keys (configured + connected).
# --------------------------------------------------------------------------- #

def test_connect_status_reflects_configured_and_connected(make_ctx) -> None:
    client = RouteClient(responses={
        "/gateway/providers": {"providers": [
            {"provider_class": "anthropic", "display_name": "Anthropic",
             "configured": True},
            {"provider_class": "claude_cli", "display_name": "Claude Code",
             "configured": True, "connected": None},
            {"provider_class": "openai", "display_name": "OpenAI",
             "configured": False},
        ]},
        "/provider-keys": {"anthropic": {"configured": True, "key_preview": "…abcd"},
                           "custom": []},
    })
    _payload, text = registry.dispatch("connect", client, make_ctx(), ["status"])
    assert ("GET", "/gateway/providers") in client.calls
    assert ("GET", "/provider-keys") in client.calls
    assert "anthropic" in text and "claude_cli" in text
    # configured surfaces as yes/no.
    assert "yes" in text and "no" in text


def test_bare_connect_defaults_to_status(make_ctx) -> None:
    client = RouteClient(default={"providers": [], "custom": []})
    registry.dispatch("connect", client, make_ctx(), [])
    assert ("GET", "/gateway/providers") in client.calls


def test_connect_status_does_not_invoke_sole_owner(make_ctx, monkeypatch) -> None:
    called: list = []
    monkeypatch.setattr("errorta_cli.commands._mutate.require_sole_owner",
                        lambda *a, **k: called.append(1))
    client = RouteClient(default={"providers": [], "custom": []})
    registry.dispatch("connect", client, make_ctx(), ["status"])
    assert called == []  # reads never guard


# --------------------------------------------------------------------------- #
# 5. connect custom → PUT /provider-keys/custom (+ the key never leaks).
# --------------------------------------------------------------------------- #

def test_connect_custom_puts_custom_entry(make_ctx, tmp_path) -> None:
    bodies: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"custom": [{"alias": "lm",
                              "base_url": "http://x", "api_style": "raw",
                              "configured": True, "key_preview": "…9999"}]})

    with _mock_client(handler) as client:
        _payload, text = registry.dispatch(
            "connect", client, make_ctx(),
            ["custom", "lm", "--base-url", "http://x", "--api-style", "raw",
             "--key-file", _key_file(tmp_path), "--yes"])
    assert bodies and bodies[0]["alias"] == "lm"
    assert bodies[0]["api_style"] == "raw"
    assert bodies[0]["api_key"] == SENTINEL  # in the body only
    assert SENTINEL not in text            # never rendered


def test_connect_custom_needs_base_url_and_style(make_ctx, tmp_path) -> None:
    client = RouteClient()
    _payload, text = registry.dispatch("connect", client, make_ctx(),
                                       ["custom", "lm"])
    assert "usage" in text.lower()
    assert client.calls == []  # nothing written without required fields


# --------------------------------------------------------------------------- #
# 6. connect ollama → local routes + availability (guidance, not a mutation).
# --------------------------------------------------------------------------- #

def test_connect_ollama_lists_local_routes(make_ctx, monkeypatch) -> None:
    called: list = []
    monkeypatch.setattr("errorta_cli.commands._mutate.require_sole_owner",
                        lambda *a, **k: called.append(1))
    client = RouteClient(responses={
        "/gateway/routes": {"routes": [{"route_id": "local.llama3", "label": "llama3"}]},
        "/gateway/model-availability": {"routes": []},
    })
    _payload, text = registry.dispatch("connect", client, make_ctx(), ["ollama"])
    assert ("GET", "/gateway/routes") in client.calls
    assert "ERRORTA_OLLAMA_HOST" in text
    assert called == []  # a read/guidance command, no guard


# --------------------------------------------------------------------------- #
# 7. team set/pool/mode/enable produce the members shape run-setup consumes.
# --------------------------------------------------------------------------- #

def test_team_set_builds_single_member(make_ctx) -> None:
    ctx = make_ctx(project_id=PID)
    registry.dispatch("team", RouteClient(), ctx, ["set", "dev", "anthropic.sonnet"])
    draft = teamdraft.load(ctx.home, PID)
    m = draft["members"][0]
    # Exactly the fields _resolve_members/_validate_member_ids/_ensure_coding_roles need.
    assert m["id"] == "dev"
    assert m["role"] == "member"
    assert m["enabled"] is True
    assert m["model_mode"] == "single"
    assert m["gateway_route_id"] == "anthropic.sonnet"
    assert m["metadata"]["coding_role"] == "dev"


def test_team_pool_builds_multi_member(make_ctx) -> None:
    ctx = make_ctx(project_id=PID)
    registry.dispatch("team", RouteClient(), ctx,
                      ["pool", "tester", "openai.a,anthropic.b"])
    m = teamdraft.load(ctx.home, PID)["members"][0]
    assert m["model_mode"] == "multi"
    assert m["model_pool"] == ["openai.a", "anthropic.b"]
    assert "gateway_route_id" not in m
    assert m["metadata"]["coding_role"] == "tester"


def test_team_mode_and_enable_toggle(make_ctx) -> None:
    ctx = make_ctx(project_id=PID)
    registry.dispatch("team", RouteClient(), ctx, ["set", "dev", "r1"])
    registry.dispatch("team", RouteClient(), ctx, ["mode", "dev", "multi"])
    registry.dispatch("team", RouteClient(), ctx, ["disable", "dev"])
    m = teamdraft.load(ctx.home, PID)["members"][0]
    assert m["model_mode"] == "multi"
    assert m["enabled"] is False


def test_team_mode_on_unknown_role_is_usage(make_ctx) -> None:
    ctx = make_ctx(project_id=PID)
    _payload, text = registry.dispatch("team", RouteClient(), ctx,
                                       ["mode", "ghost", "multi"])
    assert "no such team member" in text.lower()


def test_team_show_reads_projection_without_draft(make_ctx) -> None:
    client = RouteClient(default={"usage": {"multi_members": [], "single_members": []}})
    registry.dispatch("team", client, make_ctx(project_id=PID), [])
    assert ("GET", f"/coding/projects/{PID}/model-usage") in client.calls


def test_team_show_prefers_draft_over_projection(make_ctx) -> None:
    ctx = make_ctx(project_id=PID)
    registry.dispatch("team", RouteClient(), ctx, ["set", "pm", "r1"])
    client = RouteClient()
    _payload, text = registry.dispatch("team", client, ctx, [])
    # With a draft present, show does NOT hit the projection route.
    assert client.calls == []
    assert "Team draft" in text


# --------------------------------------------------------------------------- #
# 8. team apply → POST /run-setup/confirm with the drafted members (guarded+gated).
# --------------------------------------------------------------------------- #

def test_team_apply_confirms_members(make_ctx, monkeypatch) -> None:
    spy: list = []
    monkeypatch.setattr("errorta_cli.commands._mutate.require_sole_owner",
                        lambda home, handle: spy.append(1))
    ctx = make_ctx(project_id=PID)
    registry.dispatch("team", RouteClient(), ctx, ["set", "dev", "anthropic.sonnet"])

    bodies: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"run_setup_confirmed": True})

    with _mock_client(handler) as client:
        registry.dispatch("team", client, ctx, ["apply", "--yes"])
    assert bodies[0][0] == f"/coding/projects/{PID}/run-setup/confirm"
    assert bodies[0][1]["members"][0]["id"] == "dev"
    assert spy, "sole-owner guard not invoked on team apply"


def test_team_apply_requires_yes_non_interactive(make_ctx) -> None:
    ctx = make_ctx(project_id=PID)
    registry.dispatch("team", RouteClient(), ctx, ["set", "dev", "r1"])
    client = RouteClient()
    with pytest.raises(CliError) as ei:
        registry.dispatch("team", client, ctx, ["apply"])
    assert ei.value.code == "confirmation_required"
    assert client.calls == []


def test_team_apply_room_maps_to_team_room_id(make_ctx) -> None:
    ctx = make_ctx(project_id=PID)
    # Select a room into the draft (validates via GET /council/rooms/{id}).
    registry.dispatch("team", RouteClient(default={"room": {}}), ctx,
                      ["room", "team-7"])

    bodies: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"run_setup_confirmed": True})

    with _mock_client(handler) as client:
        registry.dispatch("team", client, ctx, ["apply", "--yes"])
    assert bodies[0] == {"team_room_id": "team-7"}


# --------------------------------------------------------------------------- #
# 9. team preflight → POST /run-setup/preflight with the draft (a probe, no guard).
# --------------------------------------------------------------------------- #

def test_team_preflight_probes_members(make_ctx, monkeypatch) -> None:
    guard: list = []
    monkeypatch.setattr("errorta_cli.commands._mutate.require_sole_owner",
                        lambda *a, **k: guard.append(1))
    ctx = make_ctx(project_id=PID)
    registry.dispatch("team", RouteClient(), ctx, ["set", "dev", "anthropic.sonnet"])

    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"unhealthy": [
            {"provider": "anthropic", "route": "anthropic.sonnet",
             "reason": "auth_failed", "remediation": "log in"}]})

    with _mock_client(handler) as client:
        _payload, text = registry.dispatch("team", client, ctx, ["preflight"])
    assert seen[0][0] == f"/coding/projects/{PID}/run-setup/preflight"
    assert seen[0][1]["members"][0]["id"] == "dev"
    assert "anthropic" in text and "log in" in text
    assert guard == []  # a probe, not a mutation


# --------------------------------------------------------------------------- #
# 10. team room → lists Council rooms / selects one into the draft.
# --------------------------------------------------------------------------- #

def test_team_room_lists_rooms(make_ctx) -> None:
    client = RouteClient(responses={"/council/rooms": {"rooms": [
        {"id": "team-1", "name": "Coding A"}]}})
    _payload, text = registry.dispatch("team", client, make_ctx(project_id=PID),
                                       ["room"])
    assert ("GET", "/council/rooms") in client.calls
    assert "team-1" in text


def test_team_room_selects_into_draft(make_ctx) -> None:
    ctx = make_ctx(project_id=PID)
    client = RouteClient(default={"room": {}})
    registry.dispatch("team", client, ctx, ["room", "team-9"])
    assert ("GET", "/council/rooms/team-9") in client.calls
    assert teamdraft.load(ctx.home, PID)["room_id"] == "team-9"


# --------------------------------------------------------------------------- #
# 11. wizard — drives GET models → start → message → create (via IO seams).
# --------------------------------------------------------------------------- #

def test_wizard_lists_models_non_interactive(make_ctx) -> None:
    client = RouteClient(responses={"/coding/wizard/models": {"routes": [
        {"route_id": "anthropic.x", "label": "Claude"}]}})
    _payload, text = registry.dispatch("wizard", client, make_ctx(), [])
    assert ("GET", "/coding/wizard/models") in client.calls
    assert "anthropic.x" in text


def test_wizard_drives_start_message_create(make_ctx, monkeypatch) -> None:
    from errorta_cli.commands import wizard

    spy: list = []
    monkeypatch.setattr("errorta_cli.commands._mutate.require_sole_owner",
                        lambda home, handle: spy.append(1))

    client = RouteClient(responses={
        "/coding/wizard/start": {"session_id": "sess-1", "reply": "hi"},
        "/message": {"reply": "got it", "ready": True, "missing": []},
        "/create": {"project": {"id": "snake"}},
    })
    lines = iter(["build a snake game", ":create snake"])
    written: list[str] = []
    ctx = make_ctx()
    ctx.json_mode = False
    payload = wizard.run_wizard(
        client, ctx, {"model": "anthropic.x", "yes": True},
        read_line=lambda _p: next(lines), write=written.append,
    )
    paths = [p for _m, p in client.calls]
    assert "/coding/wizard/start" in paths
    assert "/coding/wizard/sess-1/message" in paths
    assert "/coding/wizard/sess-1/create" in paths
    assert payload["_kind"] == "created" and payload["project_id"] == "snake"
    assert spy, "sole-owner guard not invoked on wizard create"
    # The opening + the model's reply reached the user via the write seam.
    assert "hi" in written and "got it" in written


def test_wizard_create_requires_yes_non_interactive(make_ctx) -> None:
    from errorta_cli.commands import wizard

    client = RouteClient(responses={
        "/coding/wizard/start": {"session_id": "s", "reply": "hi"},
        "/create": {"project": {}},
    })
    lines = iter([":create p"])
    ctx = make_ctx()
    ctx.json_mode = False
    with pytest.raises(CliError) as ei:
        wizard.run_wizard(client, ctx, {"model": "r", "yes": False},
                          read_line=lambda _p: next(lines), write=lambda _t: None)
    assert ei.value.code == "confirmation_required"
    # create was never POSTed.
    assert not any(p.endswith("/create") for _m, p in client.calls)


def test_wizard_quit_aborts_without_create(make_ctx) -> None:
    from errorta_cli.commands import wizard

    client = RouteClient(responses={
        "/coding/wizard/start": {"session_id": "s", "reply": "hi"}})
    ctx = make_ctx()
    ctx.json_mode = False
    payload = wizard.run_wizard(client, ctx, {"model": "r"},
                                read_line=lambda _p: ":quit",
                                write=lambda _t: None)
    assert payload["_kind"] == "aborted"
    assert not any(p.endswith("/create") for _m, p in client.calls)
