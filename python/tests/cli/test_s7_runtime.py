"""S7 runtime control — detect/run/setup/start/stop/run-cli/test/repair/logs/profile.

Grounded against the real ``coding.py`` runtime routes. Mutations gate on
``--yes`` + guard sole-owner; ``run`` default is a preview; ``run-cli`` passes
argv+timeout; ``repair`` is RESID (ResidencyRefused → exit 4). The bare read
path is unchanged from S2.
"""
from __future__ import annotations

import json

import httpx
import pytest

from errorta_cli import registry
from errorta_cli.client import ORIGIN_HEADER, ORIGIN_VALUE, SidecarClient
from errorta_cli.errors import CliError, ResidencyRefused

from .conftest import RouteClient

PID = "proj-1"
RT = f"/coding/projects/{PID}/runtime"


def _mock_client(handler) -> SidecarClient:
    return SidecarClient("http://127.0.0.1:9", transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# Read path preserved (bare = profiles).
# --------------------------------------------------------------------------- #

def test_bare_runtime_reads_profiles(make_ctx) -> None:
    client = RouteClient()
    registry.dispatch("runtime", client, make_ctx(project_id=PID), [])
    assert ("GET", f"{RT}/profiles") in client.calls


# --------------------------------------------------------------------------- #
# Probes (no gate): detect / health.
# --------------------------------------------------------------------------- #

def test_detect_posts_no_gate(make_ctx) -> None:
    client = RouteClient(responses={f"{RT}/detect": {"proposed": []}})
    registry.dispatch("runtime", client, make_ctx(project_id=PID), ["detect"])
    assert ("POST", f"{RT}/detect") in client.calls


def test_health_posts_no_gate(make_ctx) -> None:
    client = RouteClient(responses={f"{RT}/p1/health-check": {"health_status": "ok"}})
    registry.dispatch("runtime", client, make_ctx(project_id=PID), ["health", "p1"])
    assert ("POST", f"{RT}/p1/health-check") in client.calls


def test_detect_and_health_do_not_require_yes(make_ctx) -> None:
    # Non-interactive, no --yes: probes still proceed (no confirmation gate).
    for args in (["detect"], ["health", "p1"]):
        client = RouteClient(default={"proposed": [], "health_status": "ok"})
        registry.dispatch("runtime", client, make_ctx(project_id=PID), args)
        assert client.calls  # a route was hit


# --------------------------------------------------------------------------- #
# run: default preview (no gate); --go executes (gated).
# --------------------------------------------------------------------------- #

def test_run_preview_posts_confirm_false_no_gate(make_ctx) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content) if request.content else None
        return httpx.Response(200, json={"resolved": True, "runnable": True,
                                         "plan": {"modality": "desktop"}, "session": None})

    with _mock_client(handler) as client:
        registry.dispatch("runtime", client, make_ctx(project_id=PID), ["run"])
    assert seen["path"] == f"{RT}/run"
    assert seen["body"] == {"confirm": False, "confirm_reduced_isolation": False}


def test_run_go_requires_yes(make_ctx) -> None:
    client = RouteClient()
    with pytest.raises(CliError) as ei:
        registry.dispatch("runtime", client, make_ctx(project_id=PID), ["run", "--go"])
    assert ei.value.code == "confirmation_required"
    assert client.calls == []


def test_run_go_yes_posts_confirm_true(make_ctx) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content) if request.content else None
        return httpx.Response(200, json={"resolved": True, "runnable": True,
                                         "plan": {"modality": "cli"},
                                         "session": {"session_id": "s1", "state": "running"}})

    with _mock_client(handler) as client:
        registry.dispatch("runtime", client, make_ctx(project_id=PID),
                          ["run", "--go", "--reduced-isolation", "--yes"])
    assert seen["body"] == {"confirm": True, "confirm_reduced_isolation": True}


# --------------------------------------------------------------------------- #
# Mutations hit the right route + body, gated by --yes.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("args", "method", "route"),
    [
        (["setup", "p1", "--yes"], "POST", f"{RT}/p1/setup"),
        (["start", "p1", "--yes"], "POST", f"{RT}/p1/start"),
        (["stop", "p1", "--yes"], "POST", f"{RT}/p1/stop"),
        (["run-cli", "p1", "--yes"], "POST", f"{RT}/p1/run-cli"),
        (["test", "p1", "--kind", "demo_smoke", "--yes"], "POST", f"{RT}/p1/test"),
        (["repair", "p1", "--yes"], "POST", f"{RT}/p1/repair"),
        (["profile", "set", "p1", "--profile", "{}", "--yes"], "PUT", f"{RT}/profiles/p1"),
    ],
)
def test_runtime_mutation_hits_route(make_ctx, args, method, route) -> None:
    client = RouteClient(default={"session": {}, "result": {"kind": "demo_smoke",
                                                            "passed": True}, "profile": {}})
    registry.dispatch("runtime", client, make_ctx(project_id=PID), args)
    assert (method, route) in client.calls


@pytest.mark.parametrize(
    "args",
    [
        ["setup", "p1"], ["start", "p1"], ["stop", "p1"], ["run-cli", "p1"],
        ["test", "p1", "--kind", "demo_smoke"], ["repair", "p1"],
        ["profile", "set", "p1", "--profile", "{}"],
    ],
)
def test_runtime_mutation_requires_yes(make_ctx, args) -> None:
    client = RouteClient()
    with pytest.raises(CliError) as ei:
        registry.dispatch("runtime", client, make_ctx(project_id=PID), args)
    assert ei.value.code == "confirmation_required"
    assert client.calls == []


def test_setup_body_is_confirm_true(make_ctx) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content) if request.content else None
        seen["origin"] = request.headers.get(ORIGIN_HEADER)
        return httpx.Response(200, json={"session": {"session_id": "s"}})

    with _mock_client(handler) as client:
        registry.dispatch("runtime", client, make_ctx(project_id=PID),
                          ["setup", "p1", "--yes"])
    assert seen["body"] == {"confirm": True}
    assert seen["origin"] == ORIGIN_VALUE


def test_run_cli_passes_args_and_timeout(make_ctx) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content) if request.content else None
        return httpx.Response(200, json={"session": {"session_id": "s"}})

    with _mock_client(handler) as client:
        registry.dispatch("runtime", client, make_ctx(project_id=PID),
                          ["run-cli", "p1", "--args", "--flag val", "--timeout", "30",
                           "--yes"])
    assert seen["body"] == {"extra_args": "--flag val", "timeout_seconds": "30"}


def test_profile_set_puts_wrapped_profile(make_ctx) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["body"] = json.loads(request.content) if request.content else None
        return httpx.Response(200, json={"profile": {"profile_id": "p1"}})

    with _mock_client(handler) as client:
        registry.dispatch("runtime", client, make_ctx(project_id=PID),
                          ["profile", "set", "p1", "--profile",
                           '{"kind": "cli", "start": ["python", "x.py"]}', "--yes"])
    assert seen["method"] == "PUT"
    assert seen["body"] == {"profile": {"kind": "cli", "start": ["python", "x.py"]}}


def test_profile_set_bad_json_errors(make_ctx) -> None:
    client = RouteClient()
    with pytest.raises(CliError) as ei:
        registry.dispatch("runtime", client, make_ctx(project_id=PID),
                          ["profile", "set", "p1", "--profile", "not json", "--yes"])
    assert ei.value.code == "bad_profile"
    assert client.calls == []


# --------------------------------------------------------------------------- #
# test action surfaces the screenshot ref; repair is RESID.
# --------------------------------------------------------------------------- #

def test_test_action_renders_screenshot_ref(make_ctx) -> None:
    resp = {"result": {"kind": "demo_smoke", "passed": True,
                       "screenshot_ref": "shots/abc123.png"}}
    client = RouteClient(responses={f"{RT}/p1/test": resp})
    _p, text = registry.dispatch("runtime", client, make_ctx(project_id=PID),
                                 ["test", "p1", "--kind", "demo_smoke", "--yes"])
    assert "shots/abc123.png" in text


def test_repair_residency_refused_maps_exit_4(make_ctx) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": {"code": "residency_unsupported_path",
                                                    "message": "remote"}})

    with _mock_client(handler) as client:
        with pytest.raises(ResidencyRefused) as ei:
            registry.dispatch("runtime", client, make_ctx(project_id=PID),
                              ["repair", "p1", "--yes"])
    assert ei.value.exit_code == 4


# --------------------------------------------------------------------------- #
# logs read (watchable); --watch on a mutation is refused.
# --------------------------------------------------------------------------- #

def test_logs_reads_session_logs(make_ctx) -> None:
    client = RouteClient(responses={f"{RT}/sessions/s-9/logs": {"lines": ["boot", "ok"]}})
    _p, text = registry.dispatch("runtime", client, make_ctx(project_id=PID),
                                 ["logs", "s-9"])
    assert ("GET", f"{RT}/sessions/s-9/logs") in client.calls
    assert "boot" in text


def test_watch_on_mutation_refused(make_ctx) -> None:
    client = RouteClient()
    with pytest.raises(CliError) as ei:
        registry.dispatch("runtime", client, make_ctx(project_id=PID),
                          ["start", "p1", "--watch", "--yes"])
    assert ei.value.code == "watch_on_mutation"
    assert client.calls == []


# --------------------------------------------------------------------------- #
# evidence surfaces runtime_evidence + delivery outcome.
# --------------------------------------------------------------------------- #

def test_evidence_surfaces_runtime_evidence_and_delivery(make_ctx) -> None:
    project = {"project": {
        "id": PID, "delivered": True, "delivered_at": "2026-07-10T00:00:00",
        "runtime_evidence": {"any_fresh_pass": True, "current_head": "abc",
                             "results": [{"kind": "launch", "passed": True, "head": "abc"}]},
    }}
    client = RouteClient(responses={f"/coding/projects/{PID}": project})
    _p, text = registry.dispatch("runtime", client, make_ctx(project_id=PID), ["evidence"])
    assert "launch" in text
    assert "fresh" in text.lower()
    assert "delivered" in text.lower()


# --------------------------------------------------------------------------- #
# Guard + parity.
# --------------------------------------------------------------------------- #

def test_runtime_mutations_guard_sole_owner(make_ctx, monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr("errorta_cli.commands._mutate.require_sole_owner",
                        lambda *a, **k: calls.append(1))
    client = RouteClient(default={"session": {}, "result": {}, "profile": {}})
    for args in (["setup", "p1", "--yes"], ["start", "p1", "--yes"],
                 ["stop", "p1", "--yes"], ["run-cli", "p1", "--yes"],
                 ["test", "p1", "--kind", "demo_smoke", "--yes"],
                 ["repair", "p1", "--yes"],
                 ["profile", "set", "p1", "--profile", "{}", "--yes"],
                 ["run", "--go", "--yes"]):
        registry.dispatch("runtime", client, make_ctx(project_id=PID), args)
    assert len(calls) == 8


def test_runtime_probes_and_reads_do_not_guard(make_ctx, monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr("errorta_cli.commands._mutate.require_sole_owner",
                        lambda *a, **k: calls.append(1))
    for args in ([], ["detect"], ["health", "p1"], ["logs", "s"], ["run"]):
        registry.dispatch("runtime", RouteClient(default={"lines": []}),
                          make_ctx(project_id=PID), args)
    assert calls == []


def test_runtime_action_parity_argv_slash(make_ctx) -> None:
    argv, slash = RouteClient(default={"session": {}}), RouteClient(default={"session": {}})
    args = ["start", "p1", "--yes"]
    registry.dispatch("runtime", argv, make_ctx(project_id=PID), args)
    n, base = registry.split_slash("/runtime " + " ".join(args))
    registry.dispatch(n, slash, make_ctx(project_id=PID), base)
    assert argv.calls == slash.calls
    assert ("POST", f"{RT}/p1/start") in argv.calls
