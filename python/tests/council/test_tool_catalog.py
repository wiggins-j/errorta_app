"""F045 — tool catalog metadata + room-policy filtering."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from errorta_tools import catalog


def test_builtin_metadata_has_required_fields():
    meta = catalog.get_metadata("web_fetch")
    assert meta is not None
    d = meta.to_dict()
    for field in (
        "tool_id", "family", "egress_class", "default_timeout_seconds",
        "max_output_bytes", "requires_approval", "source_class",
    ):
        assert field in d, field
    assert meta.requires_approval is True  # web egress needs approval


def test_filter_for_room_returns_only_granted_and_configured():
    # No grants -> nothing, even though builtins are "configured".
    assert catalog.filter_for_room(tool_policy={}) == []
    # Grant web_fetch + code_read.
    policy = {"web_fetch": {"enabled": True}, "code_read": {"enabled": True}}
    ids = {m.tool_id for m in catalog.filter_for_room(tool_policy=policy)}
    assert ids == {"web_fetch", "code_read"}


def test_filter_excludes_granted_but_unconfigured_tool():
    # Granted code_exec, but configured set excludes it -> not listed.
    policy = {"code_exec": {"enabled": True}}
    out = catalog.filter_for_room(
        tool_policy=policy, configured_tool_ids={"web_fetch"}
    )
    assert out == []


def test_catalog_route_full_and_room_filtered(tmp_path, monkeypatch):
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    from errorta_app.server import app

    client = TestClient(app)
    full = client.get("/tools/catalog")
    assert full.status_code == 200
    assert any(t["tool_id"] == "web_fetch" for t in full.json()["tools"])

    # Unknown room -> 404 (cannot present tools for a room that doesn't exist).
    missing = client.get("/tools/catalog?room_id=does-not-exist")
    assert missing.status_code == 404


def test_mcp_tool_registration_requires_server():
    from errorta_tools.catalog import ToolMetadata

    bad = ToolMetadata(
        tool_id="mcp.foo", family="artifact", egress_class="remote",
        default_timeout_seconds=10, max_output_bytes=1000,
        requires_approval=True, backend="mcp",  # no server_id
    )
    with pytest.raises(ValueError):
        catalog.register_mcp_tool(bad)
    catalog.clear_mcp_tools()
