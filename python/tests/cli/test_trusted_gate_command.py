"""``trusted-gate`` — read-only view of the operator-declared trusted gate.

Grounded against the real ``coding.py`` route added in Task 5 of
2026-08-23-trusted-gate: ``GET /coding/projects/{id}/trusted-gate``. Mirrors
``test_s7_testcfg.py``'s harness (``RouteClient`` + ``registry.dispatch``).
There is no ``set`` action — the route is GET-only.
"""
from __future__ import annotations

from errorta_cli import registry

from .conftest import RouteClient

PID = "proj-1"
P = f"/coding/projects/{PID}"
ROUTE = f"{P}/trusted-gate"


def test_read_hits_route(make_ctx) -> None:
    client = RouteClient()
    registry.dispatch("trusted-gate", client, make_ctx(project_id=PID), [])
    assert ("GET", ROUTE) in client.calls


def test_no_project_short_circuits(make_ctx) -> None:
    client = RouteClient()
    _, text = registry.dispatch("trusted-gate", client, make_ctx(project_id=None), [])
    assert client.calls == []
    assert "project" in text.lower()


def test_valid_gate_renders_commands(make_ctx) -> None:
    payload = {
        "tier": "trusted", "path": "/home/x/.errorta/gates/proj-1.yaml",
        "present": True, "valid": True, "code": "",
        "commands": [{"id": "unit", "argv": ["/usr/bin/true"], "cwd": ".",
                     "timeout_seconds": 5, "scope": "unit"}],
        "env_passthrough": ["PATH"],
    }
    client = RouteClient(responses={ROUTE: payload})
    out, text = registry.dispatch("trusted-gate", client, make_ctx(project_id=PID), [])
    assert out == payload
    assert "trusted" in text.lower()
    assert "unit" in text
    assert "PATH" in text


def test_invalid_gate_shows_code(make_ctx) -> None:
    payload = {
        "tier": "trusted_invalid", "path": "/home/x/.errorta/gates/proj-1.yaml",
        "present": True, "valid": False, "code": "gate_mode_insecure",
        "commands": [], "env_passthrough": [],
    }
    client = RouteClient(responses={ROUTE: payload})
    out, text = registry.dispatch("trusted-gate", client, make_ctx(project_id=PID), [])
    assert out == payload
    assert "gate_mode_insecure" in text


def test_none_gate_shows_none(make_ctx) -> None:
    payload = {
        "tier": "none", "path": "/home/x/.errorta/gates/proj-1.yaml",
        "present": False, "valid": False, "code": "",
        "commands": [], "env_passthrough": [],
    }
    client = RouteClient(responses={ROUTE: payload})
    out, text = registry.dispatch("trusted-gate", client, make_ctx(project_id=PID), [])
    assert out == payload
    assert "none" in text.lower()


def test_json_flag_passes_through(make_ctx) -> None:
    payload = {
        "tier": "trusted", "path": "/home/x/.errorta/gates/proj-1.yaml",
        "present": True, "valid": True, "code": "",
        "commands": [{"id": "unit", "argv": ["/usr/bin/true"], "cwd": ".",
                     "timeout_seconds": 5, "scope": "unit"}],
        "env_passthrough": [],
    }
    client = RouteClient(responses={ROUTE: payload})
    out, text = registry.dispatch("trusted-gate", client, make_ctx(project_id=PID), ["--json"])
    assert out == payload
    assert '"tier"' in text
    assert '"trusted"' in text
