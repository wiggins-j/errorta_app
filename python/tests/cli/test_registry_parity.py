"""The marquee invariant: argv and slash resolve to the SAME command + route.

Golden invariant #3 (F147 plan §4). Both front-ends dispatch through the one
registry, so a command is identical whichever surface invokes it — by
construction, verified here.
"""
from __future__ import annotations

from errorta_cli import registry

from .conftest import RecordingClient


def test_split_slash_strips_leading_slash_and_splits() -> None:
    assert registry.split_slash("/status") == ("status", [])
    assert registry.split_slash("status --json") == ("status", ["--json"])
    assert registry.split_slash("/log --role dev") == ("log", ["--role", "dev"])
    assert registry.split_slash("   ") == ("", [])


def test_slash_name_resolves_to_same_command_object() -> None:
    for cmd in registry.all_commands():
        name_s, _ = registry.split_slash("/" + cmd.name)
        assert registry.get(name_s) is registry.get(cmd.name) is cmd


def test_every_command_hits_identical_route_via_argv_and_slash(make_ctx) -> None:
    for cmd in registry.all_commands():
        client_argv = RecordingClient(response={"health": {}, "run": {}})
        client_slash = RecordingClient(response={"health": {}, "run": {}})

        # argv surface: (name, raw_args) straight from the Typer handler.
        registry.dispatch(cmd.name, client_argv, make_ctx(), [])

        # slash surface: parse "/name" then dispatch the same way.
        name_s, raw_s = registry.split_slash("/" + cmd.name)
        registry.dispatch(name_s, client_slash, make_ctx(), raw_s)

        assert client_argv.calls == client_slash.calls, cmd.name


def test_status_hits_healthz(make_ctx) -> None:
    client = RecordingClient(response={"health": {}, "run": None})
    registry.dispatch("status", client, make_ctx(), [])
    assert ("GET", "/healthz") in client.calls


def test_status_includes_run_route_when_project_bound(make_ctx) -> None:
    client = RecordingClient(response={"health": {}, "run": {}})
    registry.dispatch("status", client, make_ctx(project_id="proj-1"), [])
    assert ("GET", "/coding/projects/proj-1/run") in client.calls


def test_status_parity_with_bound_project(make_ctx) -> None:
    """Even with a project bound, both surfaces make the same 2 route calls."""
    client_argv = RecordingClient(response={"health": {}, "run": {}})
    client_slash = RecordingClient(response={"health": {}, "run": {}})
    registry.dispatch("status", client_argv, make_ctx(project_id="p"), [])
    name_s, raw_s = registry.split_slash("/status")
    registry.dispatch(name_s, client_slash, make_ctx(project_id="p"), raw_s)
    assert client_argv.calls == client_slash.calls
    assert client_argv.calls == [("GET", "/healthz"), ("GET", "/coding/projects/p/run")]


def test_unknown_command_raises_keyerror(make_ctx) -> None:
    import pytest

    with pytest.raises(KeyError):
        registry.dispatch("nope", RecordingClient(), make_ctx(), [])


def test_json_flag_stripped_and_bypasses_render(make_ctx) -> None:
    client = RecordingClient(response={"health": {"service": "s"}, "run": None})
    payload, text = registry.dispatch("status", client, make_ctx(), ["--json"])
    # --json emits the raw payload as JSON, not the human summary.
    assert text.strip().startswith("{")
    assert '"health"' in text
