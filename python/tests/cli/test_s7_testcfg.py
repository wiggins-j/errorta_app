"""S7 test-commands / test-settings / test-runs — the merge-gate suite config.

Grounded against the real ``coding.py``: GET/PUT test-commands (2867/2877),
GET/PUT test-settings (2898/2903), GET test-runs (2893). The ``set`` paths gate
on ``--yes`` + guard and PUT the right body; reads don't.
"""
from __future__ import annotations

import json

import httpx
import pytest

from errorta_cli import registry
from errorta_cli.client import SidecarClient
from errorta_cli.errors import CliError, ResidencyRefused

from .conftest import RouteClient

PID = "proj-1"
P = f"/coding/projects/{PID}"


def _mock_client(handler) -> SidecarClient:
    return SidecarClient("http://127.0.0.1:9", transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# Reads.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("name", "route"),
    [
        ("test-commands", f"{P}/test-commands"),
        ("test-settings", f"{P}/test-settings"),
        ("test-runs", f"{P}/test-runs"),
    ],
)
def test_reads_hit_route(make_ctx, name, route) -> None:
    client = RouteClient()
    registry.dispatch(name, client, make_ctx(project_id=PID), [])
    assert ("GET", route) in client.calls


# --------------------------------------------------------------------------- #
# test-commands set — PUT {commands: [...]}.
# --------------------------------------------------------------------------- #

def test_test_commands_set_puts_body(make_ctx) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content) if request.content else None
        return httpx.Response(200, json={"commands": []})

    payload = '[{"label": "unit", "command": ["pytest", "-q"]}]'
    with _mock_client(handler) as client:
        registry.dispatch("test-commands", client, make_ctx(project_id=PID),
                          ["set", "--commands", payload, "--yes"])
    assert seen["method"] == "PUT"
    assert seen["path"] == f"{P}/test-commands"
    assert seen["body"] == {"commands": [{"label": "unit", "command": ["pytest", "-q"]}]}


def test_test_commands_set_requires_yes(make_ctx) -> None:
    client = RouteClient()
    with pytest.raises(CliError) as ei:
        registry.dispatch("test-commands", client, make_ctx(project_id=PID),
                          ["set", "--commands", "[]"])
    assert ei.value.code == "confirmation_required"
    assert client.calls == []


def test_test_commands_set_bad_json(make_ctx) -> None:
    client = RouteClient()
    with pytest.raises(CliError) as ei:
        registry.dispatch("test-commands", client, make_ctx(project_id=PID),
                          ["set", "--commands", "nope", "--yes"])
    assert ei.value.code == "bad_commands"
    assert client.calls == []


# --------------------------------------------------------------------------- #
# test-settings set — PUT {require_sandbox: bool}.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(("flag", "expected"), [("true", True), ("false", False)])
def test_test_settings_set_puts_bool(make_ctx, flag, expected) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["body"] = json.loads(request.content) if request.content else None
        return httpx.Response(200, json={"require_sandbox": expected})

    with _mock_client(handler) as client:
        registry.dispatch("test-settings", client, make_ctx(project_id=PID),
                          ["set", "--require-sandbox", flag, "--yes"])
    assert seen["method"] == "PUT"
    assert seen["body"] == {"require_sandbox": expected}


def test_test_settings_set_requires_yes(make_ctx) -> None:
    client = RouteClient()
    with pytest.raises(CliError) as ei:
        registry.dispatch("test-settings", client, make_ctx(project_id=PID),
                          ["set", "--require-sandbox", "true"])
    assert ei.value.code == "confirmation_required"
    assert client.calls == []


def test_test_settings_set_residency_refused(make_ctx) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": {"code": "residency_unsupported_path",
                                                    "message": "remote"}})

    with _mock_client(handler) as client:
        with pytest.raises(ResidencyRefused) as ei:
            registry.dispatch("test-settings", client, make_ctx(project_id=PID),
                              ["set", "--require-sandbox", "true", "--yes"])
    assert ei.value.exit_code == 4


# --------------------------------------------------------------------------- #
# Guard + parity.
# --------------------------------------------------------------------------- #

def test_set_guards_reads_dont(make_ctx, monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr("errorta_cli.commands._mutate.require_sole_owner",
                        lambda *a, **k: calls.append(1))
    client = RouteClient(default={"commands": [], "require_sandbox": True})
    registry.dispatch("test-commands", client, make_ctx(project_id=PID),
                      ["set", "--commands", "[]", "--yes"])
    registry.dispatch("test-settings", client, make_ctx(project_id=PID),
                      ["set", "--require-sandbox", "true", "--yes"])
    assert len(calls) == 2
    calls.clear()
    for name in ("test-commands", "test-settings", "test-runs"):
        registry.dispatch(name, RouteClient(), make_ctx(project_id=PID), [])
    assert calls == []


def test_testcfg_parity_argv_slash(make_ctx) -> None:
    for name, route in (("test-commands", f"{P}/test-commands"),
                        ("test-runs", f"{P}/test-runs")):
        argv, slash = RouteClient(), RouteClient()
        registry.dispatch(name, argv, make_ctx(project_id=PID), [])
        n, base = registry.split_slash("/" + name)
        registry.dispatch(n, slash, make_ctx(project_id=PID), base)
        assert argv.calls == slash.calls == [("GET", route)]


def test_s7_commands_registered() -> None:
    for name in ("publish", "runtime", "grounding", "test-commands",
                 "test-settings", "test-runs"):
        assert registry.get(name) is not None, name
