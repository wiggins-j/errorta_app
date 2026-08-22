"""``liverun`` — the terminal control surface for the live-run supervisor.

The CLI is a pure sidecar *client*, so everything here runs against a fake
transport: a path-aware ``RouteClient`` for the route/render assertions, and a
real :class:`SidecarClient` over ``httpx.MockTransport`` where the wire itself is
the thing under test (the origin header, a 422 body becoming a typed error).

The invariants worth having a test for:

* the asymmetric gate — ``start`` / ``resume`` / ``fix resume`` refuse without
  ``--yes``; ``stop`` and ``fix pause`` never do, because a subtractive action
  must not be the hard one to perform;
* ``--watch`` is refused on every mutating sub-action (a watched start would
  relaunch every tick) and ENDS on a terminal phase rather than spinning;
* the command never imports ``errorta_liverun`` — it only talks HTTP.
"""
from __future__ import annotations

import io
import json

import httpx
import pytest

from errorta_cli import registry, watch
from errorta_cli.client import ORIGIN_HEADER, ORIGIN_VALUE, SidecarClient
from errorta_cli.errors import CliError

from .conftest import RouteClient

LIVE = {
    "status": "live", "run_id": "run-7", "profile": "osrs", "phase": "watching",
    "reason": None, "project_id": "senditai-ng", "elapsed_s": 125.0,
    "probes": {
        "api": {"last_ok_age_s": 4.0, "stall_after_s": 90, "on_stall": "stop"},
        "screen": {"last_ok_age_s": 80.0, "stall_after_s": 90, "on_stall": "stop"},
        "never-yet": {"last_ok_age_s": None, "stall_after_s": 90, "on_stall": "stop"},
    },
    "caps": {"would_refuse": None}, "literals": {"logoff_verified": "PRESENT"},
    "fix_cycle": 0, "fix_of": None, "fix_cycles_today": 1, "fix_cap": 3,
    "fix_paused": False, "paused": False,
}

PROFILES = {"profiles": [
    {"name": "osrs", "valid": True, "error": None},
    {"name": "broken", "valid": False, "error": "banned_token"},
]}


def _mock_client(handler) -> SidecarClient:
    return SidecarClient("http://127.0.0.1:9", transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# Routes.
# --------------------------------------------------------------------------- #

def test_bare_liverun_reads_status(make_ctx) -> None:
    client = RouteClient(responses={"/liverun/status": {"status": "empty", "last": None}})
    registry.dispatch("liverun", client, make_ctx(), [])
    assert ("GET", "/liverun/status") in client.calls


def test_profiles_hits_the_profiles_route(make_ctx) -> None:
    client = RouteClient(responses={"/liverun/profiles": PROFILES})
    registry.dispatch("liverun", client, make_ctx(), ["profiles"])
    assert ("GET", "/liverun/profiles") in client.calls


def test_start_posts_to_start(make_ctx) -> None:
    client = RouteClient(default={"status": "started", "run_id": "r1"})
    registry.dispatch("liverun", client, make_ctx(), ["start", "osrs", "--yes"])
    assert ("POST", "/liverun/start") in client.calls


def test_stop_posts_to_stop(make_ctx) -> None:
    client = RouteClient(default={"status": "stopping", "run_id": "r1"})
    registry.dispatch("liverun", client, make_ctx(), ["stop"])
    assert ("POST", "/liverun/stop") in client.calls


def test_resume_posts_to_resume(make_ctx) -> None:
    client = RouteClient(default={"status": "resumed"})
    registry.dispatch("liverun", client, make_ctx(), ["resume", "osrs", "--yes"])
    assert ("POST", "/liverun/resume") in client.calls


def test_fix_pause_and_resume_hit_their_routes(make_ctx) -> None:
    client = RouteClient(default={"status": "paused"})
    registry.dispatch("liverun", client, make_ctx(), ["fix", "pause", "osrs"])
    registry.dispatch("liverun", client, make_ctx(), ["fix", "resume", "osrs", "--yes"])
    assert client.paths() == ["/liverun/fix/pause", "/liverun/fix/resume"]


# --------------------------------------------------------------------------- #
# Bodies + selectors.
# --------------------------------------------------------------------------- #

def _capture(bodies: list, params: list, payload: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        params.append(dict(request.url.params))
        bodies.append(json.loads(request.content) if request.content else None)
        return httpx.Response(200, json=payload)
    return handler


def test_start_sends_the_profile_and_project(make_ctx) -> None:
    bodies, params = [], []
    client = _mock_client(_capture(bodies, params, {"status": "started", "run_id": "r"}))
    registry.dispatch("liverun", client, make_ctx(),
                      ["start", "osrs", "--project", "senditai-ng", "--yes"])
    assert bodies == [{"profile": "osrs", "project_id": "senditai-ng"}]


def test_stop_sends_the_operator_note(make_ctx) -> None:
    bodies, params = [], []
    client = _mock_client(_capture(bodies, params, {"status": "stopping"}))
    registry.dispatch("liverun", client, make_ctx(),
                      ["stop", "--profile", "osrs", "--reason", "swapping the build"])
    assert bodies == [{"profile": "osrs", "reason": "swapping the build"}]


def test_stop_with_no_selector_sends_an_empty_body(make_ctx) -> None:
    bodies, params = [], []
    client = _mock_client(_capture(bodies, params, {"status": "empty"}))
    registry.dispatch("liverun", client, make_ctx(), ["stop"])
    assert bodies == [{}]  # the sidecar addresses the newest live run


def test_status_selector_travels_as_a_query_param(make_ctx) -> None:
    bodies, params = [], []
    client = _mock_client(_capture(bodies, params, dict(LIVE)))
    registry.dispatch("liverun", client, make_ctx(), ["status", "--profile", "osrs"])
    assert params == [{"profile": "osrs"}]


def test_a_positional_profile_works_for_status_and_stop(make_ctx) -> None:
    bodies, params = [], []
    client = _mock_client(_capture(bodies, params, dict(LIVE)))
    registry.dispatch("liverun", client, make_ctx(), ["status", "osrs"])
    assert params == [{"profile": "osrs"}]


def test_named_profile_wins_over_the_positional(make_ctx) -> None:
    bodies, params = [], []
    client = _mock_client(_capture(bodies, params, dict(LIVE)))
    registry.dispatch("liverun", client, make_ctx(), ["status", "ignored", "--profile", "osrs"])
    assert params == [{"profile": "osrs"}]


# --------------------------------------------------------------------------- #
# The asymmetric confirmation gate.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("args", [
    ["start", "osrs"],
    ["resume", "osrs"],
    ["fix", "resume", "osrs"],
])
def test_turning_autonomy_on_requires_yes(make_ctx, args) -> None:
    """Non-interactive (the suite's stdio) and no ``--yes`` → refuse, no route."""
    client = RouteClient()
    with pytest.raises(CliError, match="--yes"):
        registry.dispatch("liverun", client, make_ctx(), args)
    assert client.calls == []


@pytest.mark.parametrize("args", [["stop"], ["fix", "pause", "osrs"]])
def test_turning_things_off_is_never_gated(make_ctx, args) -> None:
    """A stop an operator has to confirm is a stop that happens too late."""
    client = RouteClient(default={"status": "stopping"})
    registry.dispatch("liverun", client, make_ctx(), args)
    assert client.calls  # the route fired without --yes


def test_a_declined_prompt_changes_nothing(make_ctx, monkeypatch) -> None:
    monkeypatch.setattr("errorta_cli.commands._mutate.is_interactive", lambda: True)
    monkeypatch.setattr("errorta_cli.commands._mutate.prompt_yes_no", lambda _q: False)
    client = RouteClient()
    _payload, text = registry.dispatch("liverun", client, make_ctx(), ["start", "osrs"])
    assert client.calls == []
    assert "aborted" in text


@pytest.mark.parametrize("args", [
    ["start", "osrs", "--yes"],
    ["resume", "osrs", "--yes"],
    ["fix", "resume", "osrs", "--yes"],
])
def test_the_gated_actions_guard_sole_ownership(make_ctx, monkeypatch, args) -> None:
    seen: list[object] = []
    monkeypatch.setattr("errorta_cli.commands._mutate.require_sole_owner",
                        lambda *a, **k: seen.append(a))
    registry.dispatch("liverun", RouteClient(default={"status": "started"}),
                      make_ctx(), args)
    assert seen, args


@pytest.mark.parametrize("args", [["stop"], ["fix", "pause", "osrs"]])
def test_the_ungated_actions_do_not_guard_sole_ownership(make_ctx, monkeypatch,
                                                         args) -> None:
    """A foreign Errorta app on the host must not be able to block a stop."""
    seen: list[object] = []
    monkeypatch.setattr("errorta_cli.commands._mutate.require_sole_owner",
                        lambda *a, **k: seen.append(a))
    registry.dispatch("liverun", RouteClient(default={"status": "stopping"}),
                      make_ctx(), args)
    assert seen == []


# --------------------------------------------------------------------------- #
# --watch.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("args", [
    ["start", "osrs", "--watch", "--yes"],
    ["stop", "--watch"],
    ["resume", "osrs", "--watch", "--yes"],
    ["fix", "pause", "osrs", "--watch"],
])
def test_watch_is_refused_on_every_mutation(make_ctx, args) -> None:
    client = RouteClient()
    with pytest.raises(CliError) as exc:
        registry.dispatch("liverun", client, make_ctx(), args)
    assert exc.value.code == "watch_on_mutation"
    assert client.calls == []  # refused BEFORE the write


def test_watch_stops_on_a_terminal_phase(make_ctx) -> None:
    stopped = {**LIVE, "phase": "stopped", "reason": "operator_stop"}
    client = RouteClient(responses={"/liverun/status": stopped})
    slept: list[float] = []
    out = io.StringIO()
    watch.run_watch("liverun", client, make_ctx(), ["status", "--watch"],
                    iterations=10, sleep=slept.append, out=out, clear=False)
    # One frame, then out — not ten frames of a run that already ended.
    assert client.paths() == ["/liverun/status"]
    assert slept == []
    assert "stopped" in out.getvalue()


def test_watch_stops_when_there_is_no_live_run(make_ctx) -> None:
    client = RouteClient(responses={"/liverun/status": {"status": "empty", "last": None}})
    watch.run_watch("liverun", client, make_ctx(), ["status", "--watch"],
                    iterations=10, sleep=lambda _s: None, out=io.StringIO(), clear=False)
    assert client.paths() == ["/liverun/status"]


def test_watch_keeps_polling_a_running_run(make_ctx) -> None:
    client = RouteClient(responses={"/liverun/status": dict(LIVE)})
    slept: list[float] = []
    watch.run_watch("liverun", client, make_ctx(), ["status", "--watch"],
                    iterations=3, sleep=slept.append, out=io.StringIO(), clear=False)
    assert len(client.paths()) == 3
    assert slept == [5.0, 5.0]  # the command's own 5s cadence


def test_the_operator_poll_interval_still_wins(make_ctx) -> None:
    ctx = make_ctx()
    ctx.poll_interval = 1.0
    client = RouteClient(responses={"/liverun/status": dict(LIVE)})
    slept: list[float] = []
    watch.run_watch("liverun", client, ctx, ["status", "--watch"],
                    iterations=2, sleep=slept.append, out=io.StringIO(), clear=False)
    assert slept == [1.0]


def test_watch_done_is_a_watch_only_marker(make_ctx) -> None:
    """A one-shot ``status`` must not carry the loop-control flag into --json."""
    client = RouteClient(responses={
        "/liverun/status": {**LIVE, "phase": "stopped"}})
    payload, _text = registry.dispatch("liverun", client, make_ctx(), ["status"])
    assert "_watch_done" not in payload


# --------------------------------------------------------------------------- #
# Rendering (field selection, no raw dump).
# --------------------------------------------------------------------------- #

def test_status_renders_phase_probe_ages_and_holds(make_ctx) -> None:
    payload = {**LIVE, "paused": True, "fix_paused": True,
               "reason": "stall:screen went quiet"}
    client = RouteClient(responses={"/liverun/status": payload})
    _p, text = registry.dispatch("liverun", client, make_ctx(), ["status"])
    assert "osrs" in text and "watching" in text
    assert "2m05s" in text                      # elapsed
    assert "4s/1m30s" in text                   # a healthy probe, with its budget
    assert "1m20s/1m30s" in text                # a probe nearly out of budget
    assert "never" in text                      # a probe that has never answered
    assert "stall:screen went quiet" in text
    assert "fix cycles today: 1/3" in text
    assert "PAUSED" in text and "fix loop paused" in text


def test_status_renders_the_empty_case_with_the_last_run(make_ctx) -> None:
    client = RouteClient(responses={"/liverun/status": {
        "status": "empty", "paused": False, "fix_paused": False,
        "last": {"profile_name": "osrs", "phase": "failed", "reason": "launch_step_failed:2"}}})
    _p, text = registry.dispatch("liverun", client, make_ctx(), [])
    assert "no live run" in text
    assert "osrs" in text and "failed" in text


def test_profiles_renders_the_failing_rule(make_ctx) -> None:
    client = RouteClient(responses={"/liverun/profiles": PROFILES})
    _p, text = registry.dispatch("liverun", client, make_ctx(), ["profiles"])
    assert "osrs" in text and "ok" in text
    assert "broken" in text and "invalid" in text and "banned_token" in text


def test_a_refused_start_shows_the_reason(make_ctx) -> None:
    client = RouteClient(default={"status": "refused", "reason": "already_running",
                                  "run_id": "run-7"})
    _p, text = registry.dispatch("liverun", client, make_ctx(), ["start", "osrs", "--yes"])
    assert "refused" in text and "already_running" in text and "run-7" in text


def test_json_mode_returns_the_route_payload(make_ctx) -> None:
    client = RouteClient(responses={"/liverun/status": dict(LIVE)})
    _p, text = registry.dispatch("liverun", client, make_ctx(), ["status"], json_mode=True)
    parsed = json.loads(text)
    assert parsed["run_id"] == "run-7"
    assert parsed["_kind"] == "status"


def test_a_bad_subaction_prints_usage_and_calls_nothing(make_ctx) -> None:
    client = RouteClient()
    _p, text = registry.dispatch("liverun", client, make_ctx(), ["frobnicate"])
    assert text.startswith("usage:")
    assert client.calls == []


@pytest.mark.parametrize("args,expected", [
    (["start"], "liverun start"),
    (["resume"], "liverun resume"),
    (["fix", "pause"], "liverun fix"),
    (["fix", "wobble", "osrs"], "liverun fix"),
])
def test_a_missing_profile_is_a_usage_line_not_a_request(make_ctx, args, expected) -> None:
    client = RouteClient()
    _p, text = registry.dispatch("liverun", client, make_ctx(), [*args, "--yes"])
    assert expected in text
    assert client.calls == []


# --------------------------------------------------------------------------- #
# The wire: origin header, and a route refusal becoming a typed CLI error.
# --------------------------------------------------------------------------- #

def test_every_liverun_request_carries_the_cli_origin(make_ctx) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get(ORIGIN_HEADER, ""))
        return httpx.Response(200, json={"status": "started", "profiles": []})

    client = _mock_client(handler)
    for args in (["profiles"], ["status"], ["start", "osrs", "--yes"], ["stop"],
                 ["resume", "osrs", "--yes"], ["fix", "pause", "osrs"],
                 ["fix", "resume", "osrs", "--yes"]):
        registry.dispatch("liverun", client, make_ctx(), args)
    assert len(seen) == 7
    assert set(seen) == {ORIGIN_VALUE}


def test_a_422_bad_profile_name_surfaces_the_route_code(make_ctx) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": {
            "code": "bad_profile_name", "message": "a profile name must start alphanumeric"}})

    client = _mock_client(handler)
    with pytest.raises(CliError) as exc:
        registry.dispatch("liverun", client, make_ctx(), ["start", "../escape", "--yes"])
    assert exc.value.code == "bad_profile_name"
    assert "alphanumeric" in exc.value.message


def test_a_403_becomes_an_origin_error(make_ctx) -> None:
    from errorta_cli.errors import OriginDenied

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "origin_not_authorized"})

    with pytest.raises(OriginDenied):
        registry.dispatch("liverun", _mock_client(handler), make_ctx(), ["profiles"])


def test_a_409_residency_refusal_maps_to_the_residency_error(make_ctx) -> None:
    from errorta_cli.errors import ResidencyRefused

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": {
            "code": "residency_unsupported_path", "path": "/liverun/start",
            "message": "not available in remote data-residency mode yet"}})

    with pytest.raises(ResidencyRefused):
        registry.dispatch("liverun", _mock_client(handler), make_ctx(),
                          ["start", "osrs", "--yes"])


# --------------------------------------------------------------------------- #
# Boundaries.
# --------------------------------------------------------------------------- #

def test_the_command_never_imports_the_supervisor() -> None:
    """Golden invariant #1, restated where it is easiest to break: the operator
    surface for a supervisor must not link against the supervisor."""
    from pathlib import Path

    import errorta_cli.commands.liverun as mod

    source = Path(mod.__file__).read_text("utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        assert not stripped.startswith(("import errorta_liverun", "from errorta_liverun")), line


def test_liverun_is_registered_on_both_surfaces() -> None:
    name, raw = registry.split_slash("/liverun")
    assert registry.get(name) is registry.get("liverun") is not None
    assert raw == []
