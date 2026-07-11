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


# --- S2: parity across every read command (argv ≡ slash) ----------------------

# Representative args per command so a required positional is satisfied. Both
# surfaces receive the identical token list, so identical routes are the property
# under test.
_S2_ARGS = {
    "pr": ["pr-1"],
    "turn": ["t1", "tn-1"],
    "pm": ["chat"],
    # S3 mutations: --yes arms them non-interactively; --detach keeps `run` from
    # entering its live-stream loop (so a bare RouteClient can't hang the test).
    "run": ["--yes", "--detach"],
    "cancel": ["--yes"],
    "resume": ["--yes"],
    "continue": ["--yes"],
    # `setup` with no args is the read path (GET /run-setup) — no gate, no guard.
}


def _args_for(name: str) -> list[str]:
    return list(_S2_ARGS.get(name, []))


def test_all_commands_parity_with_bound_project(make_ctx) -> None:
    """Every registered command hits the identical route sequence via argv and
    slash when a project is bound — and actually reaches a route (not a no-op)."""
    from .conftest import RouteClient

    for cmd in registry.all_commands():
        args = _args_for(cmd.name)
        argv_client = RouteClient()
        slash_client = RouteClient()

        registry.dispatch(cmd.name, argv_client, make_ctx(project_id="p"), args)

        name_s, base = registry.split_slash("/" + cmd.name + " " + " ".join(args))
        registry.dispatch(name_s, slash_client, make_ctx(project_id="p"), base)

        assert argv_client.calls == slash_client.calls, cmd.name
        assert argv_client.calls, f"{cmd.name} made no route call with a project bound"


# --- S3: the new run-control commands are registered + parity-clean ----------

def test_s3_commands_are_registered() -> None:
    for name in ("setup", "run", "cancel", "resume", "continue"):
        assert registry.get(name) is not None, name


def test_s3_mutations_hit_expected_routes_via_both_surfaces(make_ctx) -> None:
    """run/cancel/resume/continue POST to the real coding.py routes, identically
    on argv and slash."""
    from .conftest import RouteClient

    expected = {
        "run": ("POST", "/coding/projects/p/run", ["--yes", "--detach"]),
        "cancel": ("POST", "/coding/projects/p/run/cancel", ["--yes"]),
        "resume": ("POST", "/coding/projects/p/run/resume", ["--yes"]),
        "continue": ("POST", "/coding/projects/p/run/continue", ["--yes"]),
    }
    for name, (method, path, args) in expected.items():
        argv_client = RouteClient()
        slash_client = RouteClient()
        registry.dispatch(name, argv_client, make_ctx(project_id="p"), list(args))
        n_s, base = registry.split_slash("/" + name + " " + " ".join(args))
        registry.dispatch(n_s, slash_client, make_ctx(project_id="p"), base)
        assert argv_client.calls == slash_client.calls, name
        assert (method, path) in argv_client.calls, (name, argv_client.calls)


# --- S4: connect / wizard registered + parity-clean (argv ≡ slash) -----------

def test_s4_commands_are_registered() -> None:
    for name in ("connect", "wizard"):
        assert registry.get(name) is not None, name


def test_s4_reads_hit_identical_routes_via_both_surfaces(make_ctx) -> None:
    """Bare ``connect`` (→ status) and ``wizard`` (→ models list) make the same
    route calls whether invoked as argv or slash, and actually reach a route."""
    from .conftest import RouteClient

    for name, expected in (
        ("connect", ("GET", "/gateway/providers")),
        ("wizard", ("GET", "/coding/wizard/models")),
    ):
        argv_client = RouteClient(default={"providers": [], "routes": [], "custom": []})
        slash_client = RouteClient(default={"providers": [], "routes": [], "custom": []})
        registry.dispatch(name, argv_client, make_ctx(project_id="p"), [])
        n_s, base = registry.split_slash("/" + name)
        registry.dispatch(n_s, slash_client, make_ctx(project_id="p"), base)
        assert argv_client.calls == slash_client.calls, name
        assert expected in argv_client.calls, (name, argv_client.calls)


def test_json_bypasses_render_for_every_command(make_ctx) -> None:
    from .conftest import RouteClient

    for cmd in registry.all_commands():
        client = RouteClient(default={"entries": [], "run": {}, "health": {}})
        _payload, text = registry.dispatch(
            cmd.name, client, make_ctx(project_id="p"), _args_for(cmd.name), json_mode=True
        )
        stripped = text.strip()
        assert stripped.startswith("{") or stripped.startswith("["), cmd.name
