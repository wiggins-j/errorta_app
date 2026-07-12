"""F150 — team builder: `add_members`, role/value/count parsing, the route-id
capability heuristic, and the `--default` assembler."""
from __future__ import annotations

from typing import Any

import pytest

from errorta_cli import teamdraft
from errorta_cli.commands import team as tm
from errorta_cli.errors import CliError

# --- teamdraft.add_members ---------------------------------------------------

def test_add_members_ids_and_role() -> None:
    d = teamdraft.add_members({"members": [], "room_id": None}, "dev", 3,
                              route="cursor_cli.composer-2.5")
    assert [m["id"] for m in d["members"]] == ["dev-1", "dev-2", "dev-3"]
    assert all((m["metadata"]["coding_role"]) == "dev" for m in d["members"])
    assert all(m["model_mode"] == "single" for m in d["members"])
    assert all(m["gateway_route_id"] == "cursor_cli.composer-2.5" for m in d["members"])


def test_add_members_appends_from_max_suffix() -> None:
    d = teamdraft.add_members({"members": [], "room_id": None}, "dev", 2, route="x")
    d = teamdraft.add_members(d, "dev", 2, route="x")
    assert [m["id"] for m in d["members"]] == ["dev-1", "dev-2", "dev-3", "dev-4"]


def test_add_members_no_collision_with_set_route() -> None:
    d = teamdraft.set_route({"members": [], "room_id": None}, "dev", "x")  # id "dev"
    d = teamdraft.add_members(d, "dev", 1, route="x")                      # id "dev-2"
    assert [m["id"] for m in d["members"]] == ["dev", "dev-2"]


def test_add_members_multi_pool() -> None:
    d = teamdraft.add_members({"members": [], "room_id": None}, "reviewer", 1,
                              pool=["a", "b"])
    m = d["members"][0]
    assert m["model_mode"] == "multi" and m["model_pool"] == ["a", "b"]
    assert "gateway_route_id" not in m


def test_add_members_requires_exactly_one_of_route_pool() -> None:
    with pytest.raises(ValueError):
        teamdraft.add_members({"members": [], "room_id": None}, "dev", 1)
    with pytest.raises(ValueError):
        teamdraft.add_members({"members": [], "room_id": None}, "dev", 1,
                              route="x", pool=["y"])


# --- role / value / count parsing --------------------------------------------

def test_role_from_flag_value_from_positional_a() -> None:
    assert tm._add_role_value({"dev": True}, "cursor_cli.composer-2.5", None) \
        == ("dev", "cursor_cli.composer-2.5")


def test_role_positional_value_from_b() -> None:
    assert tm._add_role_value({}, "pm", "claude_cli.opus") == ("pm", "claude_cli.opus")


def test_role_aliases() -> None:
    assert tm._add_role_value({"test": True}, "r", None)[0] == "tester"
    assert tm._add_role_value({"programmer": True}, "r", None)[0] == "dev"
    assert tm._add_role_value({}, "test", "r") == ("tester", "r")


def test_two_role_flags_error() -> None:
    with pytest.raises(CliError):
        tm._add_role_value({"dev": True, "pm": True}, "r", None)


@pytest.mark.parametrize("raw,expected", [(None, 1), ("", 1), ("3", 3), ("1", 1)])
def test_count_ok(raw: Any, expected: int) -> None:
    assert tm._add_count({"count": raw}) == expected


@pytest.mark.parametrize("raw", ["0", "-2", "two", "1.5"])
def test_count_bad(raw: str) -> None:
    with pytest.raises(CliError):
        tm._add_count({"count": raw})


# --- capability heuristic + deterministic pick -------------------------------

@pytest.mark.parametrize("route,bucket", [
    ("claude_cli.opus", "reasoning"),
    ("openai.gpt-5", "reasoning"),
    ("cursor_cli.claude-4.5-opus-high", "reasoning"),  # keyed on route_id, not family
    ("cursor_cli.gpt-5.3-codex", "coding"),
    ("claude_cli.sonnet", "coding"),
    ("cursor_cli.composer-2.5", "coding"),
    ("claude_cli.haiku", "light"),
    ("openai.gpt-5-mini", "light"),
    ("cursor_cli.default", "mid"),
])
def test_bucket(route: str, bucket: str) -> None:
    assert tm._bucket(route) == bucket


def test_pick_is_deterministic_sorted() -> None:
    cands = [("z.sonnet", "z"), ("a.sonnet", "a")]  # both coding
    assert tm._pick(cands, ("coding",)) == ("a.sonnet", "a")  # sorted tie-break


def test_pick_excludes_provider() -> None:
    cands = [("a.sonnet", "a"), ("b.sonnet", "b")]
    assert tm._pick(cands, ("coding",), exclude_provider="a") == ("b.sonnet", "b")


def test_resolve_value_provider_vs_route() -> None:
    by_prov = {"cursor_cli": ["cursor_cli.a", "cursor_cli.b"]}
    allr = {"cursor_cli.a", "cursor_cli.b"}
    assert tm._resolve_value("cursor_cli", by_prov, allr) \
        == (None, ["cursor_cli.a", "cursor_cli.b"])
    assert tm._resolve_value("cursor_cli.a", by_prov, allr) == ("cursor_cli.a", None)
    with pytest.raises(CliError):
        tm._resolve_value("nope", by_prov, allr)


# --- --default assembler (mocked gateway) ------------------------------------

class _FakeClient:
    def __init__(self, providers: list[dict], routes: list[dict]) -> None:
        self._p = {"providers": providers}
        self._r = {"routes": routes}

    def get_json(self, path: str, **_: Any) -> Any:
        return self._p if "providers" in path else self._r


def _route(rid: str, prov: str) -> dict:
    return {"route_id": rid, "provider_class": prov, "label": rid, "family": ""}


def test_default_uses_configured_api_provider(make_ctx: Any) -> None:
    # anthropic is API-key (configured, no `connected`) — must still be usable.
    client = _FakeClient(
        providers=[{"provider_class": "anthropic", "configured": True}],
        routes=[_route("anthropic.opus", "anthropic"),
                _route("anthropic.sonnet", "anthropic"),
                _route("anthropic.haiku", "anthropic")],
    )
    out = tm._assemble_default(client, make_ctx())
    roles = [(m["metadata"]["coding_role"], m["gateway_route_id"]) for m in out["draft"]["members"]]
    from collections import Counter
    assert Counter(r for r, _ in roles) == {"pm": 1, "dev": 3, "reviewer": 1, "tester": 1}
    pm = next(rt for r, rt in roles if r == "pm")
    assert pm == "anthropic.opus"  # reasoning-strong


def test_default_prefers_provider_diversity_for_reviewer(make_ctx: Any) -> None:
    client = _FakeClient(
        providers=[{"provider_class": "cursor_cli", "connected": True},
                   {"provider_class": "claude_cli", "connected": True}],
        routes=[_route("claude_cli.opus", "claude_cli"),
                _route("claude_cli.sonnet", "claude_cli"),
                _route("cursor_cli.gpt-5.3-codex", "cursor_cli")],
    )
    out = tm._assemble_default(client, make_ctx())
    by = {m["metadata"]["coding_role"]: m["gateway_route_id"] for m in out["draft"]["members"]}
    dev_prov = by["dev"].split(".")[0]
    rev_prov = by["reviewer"].split(".")[0]
    assert dev_prov != rev_prov  # diversity honored


def test_default_no_usable_providers_errors(make_ctx: Any) -> None:
    client = _FakeClient(
        providers=[{"provider_class": "anthropic", "configured": False},
                   {"provider_class": "claude_cli", "connected": None}],
        routes=[],
    )
    with pytest.raises(CliError):
        tm._assemble_default(client, make_ctx())
