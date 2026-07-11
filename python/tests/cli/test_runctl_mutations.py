"""S3 run-control mutations — the first MUTATING slice (F147 §8).

Grounded against the real ``coding.py`` routes + error detail strings (file:line
cited inline). The sidecar is never booted: HTTP is either a ``RouteClient`` fake
or a real ``SidecarClient`` over ``httpx.MockTransport``. The autouse
``_neutralize_sole_owner_guard`` fixture (conftest) pins the guard to a no-op;
tests that assert the guard is *invoked* re-``setattr`` a spy over it.
"""
from __future__ import annotations

import json

import httpx
import pytest

from errorta_cli import registry, runstream
from errorta_cli.client import ORIGIN_HEADER, ORIGIN_VALUE, SidecarClient
from errorta_cli.errors import (
    AlphaLocked,
    CliError,
    LockBusy,
    OriginDenied,
    PreflightFailed,
    ResidencyRefused,
    SetupRequired,
    SidecarUnreachable,
)

from .conftest import RouteClient

PID = "proj-1"


# --------------------------------------------------------------------------- #
# 1. `run` POSTs to the right route with members/room_id + the origin header.
# --------------------------------------------------------------------------- #

def _mock_client(handler) -> SidecarClient:
    return SidecarClient("http://127.0.0.1:9", transport=httpx.MockTransport(handler))


def test_run_posts_room_id_with_origin_header(make_ctx) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["origin"] = request.headers.get(ORIGIN_HEADER)
        seen["body"] = json.loads(request.content) if request.content else None
        return httpx.Response(200, json={"started": True, "resumed": False})

    with _mock_client(handler) as client:
        # --detach: fire-and-return, so only the POST is exercised here.
        registry.dispatch("run", client, make_ctx(project_id=PID),
                          ["--room", "team-1", "--yes", "--detach"])
    assert seen["method"] == "POST"
    assert seen["path"] == f"/coding/projects/{PID}/run"
    assert seen["origin"] == ORIGIN_VALUE
    assert seen["body"] == {"room_id": "team-1"}


def test_run_posts_members_array(make_ctx) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content) if request.content else None
        return httpx.Response(200, json={"started": True})

    with _mock_client(handler) as client:
        registry.dispatch("run", client, make_ctx(project_id=PID),
                          ["--members", '[{"id": "m1"}]', "--yes", "--detach"])
    assert seen["body"] == {"members": [{"id": "m1"}]}


@pytest.mark.parametrize(
    ("name", "args", "route"),
    [
        ("cancel", ["--yes"], f"/coding/projects/{PID}/run/cancel"),
        ("resume", ["--yes"], f"/coding/projects/{PID}/run/resume"),
        ("continue", ["--yes"], f"/coding/projects/{PID}/run/continue"),
        ("setup", ["--confirm", "--yes"], f"/coding/projects/{PID}/run-setup/confirm"),
        ("setup", ["--preflight"], f"/coding/projects/{PID}/run-setup/preflight"),
    ],
)
def test_mutation_posts_expected_route(make_ctx, name, args, route) -> None:
    client = RouteClient()
    registry.dispatch(name, client, make_ctx(project_id=PID), args)
    assert ("POST", route) in client.calls


def test_setup_read_gets_run_setup(make_ctx) -> None:
    client = RouteClient()
    registry.dispatch("setup", client, make_ctx(project_id=PID), [])
    assert ("GET", f"/coding/projects/{PID}/run-setup") in client.calls


# --------------------------------------------------------------------------- #
# 2. require_sole_owner is invoked on every mutation and NOT on reads (#5).
# --------------------------------------------------------------------------- #

_MUTATIONS = [
    ("run", ["--yes", "--detach"]),
    ("cancel", ["--yes"]),
    ("resume", ["--yes"]),
    ("continue", ["--yes"]),
    ("setup", ["--confirm", "--yes"]),
]


def test_guard_invoked_on_every_mutation(make_ctx, monkeypatch) -> None:
    calls: list[tuple] = []
    # Overrides the autouse no-op with a spy (test body runs after fixtures).
    monkeypatch.setattr(
        "errorta_cli.commands._mutate.require_sole_owner",
        lambda home, handle: calls.append((home, handle)),
    )
    for name, args in _MUTATIONS:
        registry.dispatch(name, RouteClient(), make_ctx(project_id=PID), args)
    assert len(calls) == len(_MUTATIONS), calls


def test_guard_not_invoked_on_reads(make_ctx, monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(
        "errorta_cli.commands._mutate.require_sole_owner",
        lambda *a, **k: calls.append(1),
    )
    reads = [
        ("setup", []),               # GET /run-setup
        ("setup", ["--preflight"]),  # a provider probe — not a state mutation
        ("status", []),
        ("log", []),
    ]
    for name, args in reads:
        client = RouteClient(default={"health": {}, "run": {}, "entries": []})
        registry.dispatch(name, client, make_ctx(project_id=PID), args)
    assert calls == []


# --------------------------------------------------------------------------- #
# 3. --yes gating: non-interactive / --json requires --yes (#7).
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name, args", _MUTATIONS)
def test_mutation_requires_yes_non_interactive(make_ctx, name, args) -> None:
    # Drop the --yes token → non-interactive (pytest stdio isn't a TTY) must refuse.
    without_yes = [a for a in args if a != "--yes"]
    client = RouteClient()
    with pytest.raises(CliError) as ei:
        registry.dispatch(name, client, make_ctx(project_id=PID), without_yes)
    assert ei.value.code == "confirmation_required"
    assert client.calls == []  # never fired a request


def test_run_json_mode_requires_yes(make_ctx) -> None:
    client = RouteClient()
    with pytest.raises(CliError) as ei:
        registry.dispatch("run", client, make_ctx(project_id=PID),
                          ["--room", "t", "--detach"], json_mode=True)
    assert "yes" in ei.value.message.lower()
    assert client.calls == []


def test_run_with_yes_proceeds(make_ctx) -> None:
    client = RouteClient(default={"started": True})
    payload, _text = registry.dispatch(
        "run", client, make_ctx(project_id=PID), ["--room", "t", "--yes", "--detach"]
    )
    assert ("POST", f"/coding/projects/{PID}/run") in client.calls
    assert payload.get("_detach")


def test_interactive_decline_aborts_without_request(make_ctx, monkeypatch) -> None:
    monkeypatch.setattr("errorta_cli.commands._mutate.is_interactive", lambda: True)
    monkeypatch.setattr("errorta_cli.commands._mutate.prompt_yes_no", lambda q: False)
    client = RouteClient()
    payload, _text = registry.dispatch("run", client, make_ctx(project_id=PID), ["--room", "t"])
    assert payload.get("_aborted")
    assert client.calls == []


def test_interactive_accept_proceeds(make_ctx, monkeypatch) -> None:
    monkeypatch.setattr("errorta_cli.commands._mutate.is_interactive", lambda: True)
    monkeypatch.setattr("errorta_cli.commands._mutate.prompt_yes_no", lambda q: True)
    client = RouteClient(default={"started": True})
    registry.dispatch("run", client, make_ctx(project_id=PID), ["--room", "t", "--detach"])
    assert ("POST", f"/coding/projects/{PID}/run") in client.calls


# --------------------------------------------------------------------------- #
# 4. Error mapping → exit codes, cross-checked to real coding.py detail strings.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("name", "status", "detail", "exc", "exit_code"),
    [
        # coding.py:64 origin_not_authorized
        ("run", 403, "origin_not_authorized", OriginDenied, 6),
        # errorta_alpha/state.py:150 alpha_locked
        ("run", 403, {"error": "alpha_locked", "state": "locked"}, AlphaLocked, 5),
        # coding.py:2308 "a run is already in progress"
        ("run", 409, "a run is already in progress", LockBusy, 3),
        # _residency_proxy.py:60 residency_unsupported_path
        ("run", 409, {"code": "residency_unsupported_path", "message": "no"},
         ResidencyRefused, 4),
        # coding.py:2291 member_health_preflight_failed
        ("run", 409,
         {"code": "member_health_preflight_failed", "message": "not ready",
          "unhealthy": [{"provider": "anthropic", "route": "anthropic.x",
                         "reason": "auth_failed", "remediation": "log in via Settings"}]},
         PreflightFailed, 11),
        # coding.py:2237 run_setup_required
        ("run", 409, {"code": "run_setup_required", "message": "confirm setup first"},
         SetupRequired, 12),
        # coding.py:2310 "run is not recoverable" (resume)
        ("resume", 409, "run is not recoverable", LockBusy, 3),
        # coding.py:2315 "run is not continuable" (continue)
        ("continue", 409, "run is not continuable", LockBusy, 3),
    ],
)
def test_error_maps_to_exit_code(make_ctx, name, status, detail, exc, exit_code) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": detail})

    with _mock_client(handler) as client:
        with pytest.raises(exc) as ei:
            registry.dispatch(name, client, make_ctx(project_id=PID),
                              ["--room", "t", "--yes", "--detach"])
    assert ei.value.exit_code == exit_code


def test_preflight_failure_message_includes_remediation(make_ctx) -> None:
    detail = {
        "code": "member_health_preflight_failed", "message": "not ready",
        "unhealthy": [{"provider": "anthropic", "route": "anthropic.x",
                       "reason": "auth_failed", "remediation": "log in via Settings"}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": detail})

    with _mock_client(handler) as client:
        with pytest.raises(PreflightFailed) as ei:
            registry.dispatch("run", client, make_ctx(project_id=PID),
                              ["--room", "t", "--yes", "--detach"])
    # The enriched message renders the unhealthy provider + its remediation.
    assert "anthropic" in ei.value.message
    assert "log in via Settings" in ei.value.message
    assert ei.value.exit_code == 11


# --------------------------------------------------------------------------- #
# 5. Terminal stop_reason → exit-code classes (pure classifier + payload stamp).
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("status", "stop_reason", "expected"),
    [
        ("stopped", "definition_of_done", runstream.EXIT_OK),
        ("stopped", "checkpoint", runstream.EXIT_OK),
        ("stopped", "cancelled", runstream.EXIT_OK),
        ("stopped", "no_actionable_work", runstream.EXIT_OK),
        ("stopped", "no_progress", runstream.EXIT_RUN_FAILED),
        ("stopped", "budget_exhausted", runstream.EXIT_RUN_FAILED),
        ("stopped", "hard_blocker", runstream.EXIT_RUN_FAILED),
        ("stopped", "member_unhealthy", runstream.EXIT_RUN_FAILED),
        ("stopped", "worker_unproductive", runstream.EXIT_RUN_FAILED),
        ("stopped", "completion_blocked", runstream.EXIT_RUN_FAILED),
        ("stopped", "not_converging", runstream.EXIT_RUN_FAILED),
        ("failed", None, runstream.EXIT_RUN_FAILED),
        ("interrupted", None, runstream.EXIT_RUN_FAILED),
    ],
)
def test_classify_exit(status, stop_reason, expected) -> None:
    payload = {"running": False, "state": {"status": status, "stop_reason": stop_reason}}
    assert runstream.classify_exit(payload) == expected


def test_run_stamps_exit_code_on_failure_terminal(make_ctx) -> None:
    # POST /run then a single GET /run that is already terminal-failed.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={
                "running": False,
                "state": {"status": "stopped", "stop_reason": "no_progress",
                          "counters": {"iterations": 4}},
            })
        return httpx.Response(200, json={"started": True})

    with _mock_client(handler) as client:
        payload, text = registry.dispatch(
            "run", client, make_ctx(project_id=PID), ["--room", "t", "--yes"]
        )
    assert payload.get("_exit_code") == runstream.EXIT_RUN_FAILED
    assert registry.exit_code_for(payload) == 7
    assert "no_progress" in text


def test_run_success_terminal_has_no_failure_exit_code(make_ctx) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={
                "running": False,
                "state": {"status": "stopped", "stop_reason": "definition_of_done"},
            })
        return httpx.Response(200, json={"started": True})

    with _mock_client(handler) as client:
        payload, _text = registry.dispatch(
            "run", client, make_ctx(project_id=PID), ["--room", "t", "--yes"]
        )
    assert registry.exit_code_for(payload) == 0


# --------------------------------------------------------------------------- #
# 6. The streaming loop drives to terminal over a mocked sequence and STOPS.
# --------------------------------------------------------------------------- #

class _SequencedRunClient:
    """GET /run returns ``running`` for the first N polls, then terminal.

    Every other GET returns ``{}`` (empty ledgers). ``base_url`` is present so the
    poller's own-sidecar invariant holds.
    """

    def __init__(self, running_polls: int) -> None:
        self.base_url = "http://127.0.0.1:59999"
        self._left = running_polls
        self.run_gets = 0
        self.paths: list[str] = []

    def get_json(self, path: str, *, params=None):
        self.paths.append(path)
        if path.endswith("/run"):
            self.run_gets += 1
            if self._left > 0:
                self._left -= 1
                return {"running": True, "state": {"status": "running"}}
            return {"running": False,
                    "state": {"status": "stopped", "stop_reason": "definition_of_done"}}
        return {}

    def post_json(self, path: str, *, json=None, params=None):
        self.paths.append(path)
        return {"started": True}


def test_stream_run_drives_to_terminal_and_stops(make_ctx) -> None:
    client = _SequencedRunClient(running_polls=3)
    ctx = make_ctx(project_id=PID)
    sleeps: list[float] = []
    final = runstream.stream_run(
        client, ctx, sleep=lambda s: sleeps.append(s), emit=lambda _l: None, max_ticks=50
    )
    assert runstream.is_terminal(final)
    assert final["state"]["stop_reason"] == "definition_of_done"
    # 3 running polls + 1 terminal poll = 4 GET /run; loop slept only between ticks.
    assert client.run_gets == 4
    assert len(sleeps) == 3  # no sleep after the terminal tick
    # Only ever the CLI's own sidecar (invariant #6).
    assert all(p.startswith("/coding/") for p in client.paths)


def test_stream_run_respects_max_ticks_guard(make_ctx) -> None:
    # A run that never terminates must still stop at the max_ticks bound (no hang).
    client = _SequencedRunClient(running_polls=10_000)
    final = runstream.stream_run(
        client, make_ctx(project_id=PID), sleep=lambda _s: None, emit=lambda _l: None, max_ticks=5
    )
    assert not runstream.is_terminal(final)
    assert client.run_gets == 5


def test_block_until_terminal_polls_only_run(make_ctx) -> None:
    client = _SequencedRunClient(running_polls=2)
    final = runstream.block_until_terminal(
        client, PID, sleep=lambda _s: None, max_ticks=50
    )
    assert runstream.is_terminal(final)
    # block-to-done never streams ledgers — every GET is the run route.
    assert all(p.endswith("/run") for p in client.paths)


# --------------------------------------------------------------------------- #
# 7. Ctrl-C detaches the view; it does NOT cancel the run (§8.2).
# --------------------------------------------------------------------------- #

def test_ctrl_c_detaches_without_cancel(make_ctx, monkeypatch) -> None:
    posted: list[str] = []

    class _CtrlCClient:
        base_url = "http://127.0.0.1:59999"

        def post_json(self, path, *, json=None, params=None):
            posted.append(path)
            return {"started": True}

        def get_json(self, path, *, params=None):
            return {}

    # Make the stream raise KeyboardInterrupt (as if the user pressed Ctrl-C).
    def _boom(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(runstream, "stream_run", _boom)
    client = _CtrlCClient()
    payload, text = registry.dispatch(
        "run", client, make_ctx(project_id=PID), ["--room", "t", "--yes"]
    )
    assert payload.get("_detached")
    # The ONLY POST was the start — cancel was never sent.
    assert posted == [f"/coding/projects/{PID}/run"]
    assert not any(p.endswith("/run/cancel") for p in posted)
    assert "detached" in text.lower()


# --------------------------------------------------------------------------- #
# 8. classify_exit fails CLOSED on an unknown/未-triaged terminal reason (#1).
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "stop_reason",
    ["some_future_reason_the_cli_has_not_triaged", None, ""],
)
def test_classify_exit_unknown_reason_fails_closed(stop_reason) -> None:
    # A terminal `stopped` run whose reason isn't in the SUCCESS allowlist must
    # classify as failure (non-zero) — never a silent CI "success".
    payload = {"running": False,
               "state": {"status": "stopped", "stop_reason": stop_reason}}
    assert runstream.classify_exit(payload) == runstream.EXIT_RUN_FAILED


def _engine_stop_reasons() -> set[str]:
    """Parse the CURRENT engine stop_reason constants straight from the source.

    Reads ``autonomy.py`` as text (no heavy engine import) between the
    ``# --- stop reasons`` header and the next section header, extracting each
    ``NAME = "value"`` literal. A NEW engine reason added to that block that the
    CLI hasn't triaged will surface here and fail the guard below — loudly.
    """
    import re
    from pathlib import Path

    autonomy = (Path(__file__).resolve().parents[2]
                / "errorta_council" / "coding" / "autonomy.py")
    assert autonomy.exists(), f"engine autonomy.py not found at {autonomy}"
    text = autonomy.read_text()
    start = text.index("# --- stop reasons")
    block = text[start:]
    block = block[: block.index("\n# ---", 1)]  # up to the next section header
    return set(re.findall(r'^[A-Z][A-Z0-9_]*\s*=\s*"([a-z_]+)"', block, re.MULTILINE))


def test_every_engine_stop_reason_is_triaged() -> None:
    """Drift guard: the two CLI sets must EXACTLY partition the engine reasons.

    So a future engine reason the CLI forgot to classify fails CI here instead of
    silently mis-exiting as success (finding #1).
    """
    engine = _engine_stop_reasons()
    assert len(engine) >= 11, f"parsed too few stop_reasons: {engine}"

    success = runstream.SUCCESS_STOP_REASONS
    failure = runstream.FAILURE_STOP_REASONS
    assert not (success & failure), f"stop_reason in BOTH CLI sets: {success & failure}"

    untriaged = engine - (success | failure)
    assert not untriaged, f"engine stop_reasons the CLI hasn't triaged: {untriaged}"
    phantom = (success | failure) - engine
    assert not phantom, f"CLI references unknown stop_reasons: {phantom}"

    # Every engine reason classifies to its intended code.
    for reason in engine:
        payload = {"running": False,
                   "state": {"status": "stopped", "stop_reason": reason}}
        expected = runstream.EXIT_OK if reason in success else runstream.EXIT_RUN_FAILED
        assert runstream.classify_exit(payload) == expected, reason


# --------------------------------------------------------------------------- #
# 9. Poll-error tolerance: a transient blip mid-stream is survived (#2).
# --------------------------------------------------------------------------- #

class _BlippyRunClient:
    """``GET /run`` raises on the given (1-based) poll indices, else advances.

    Non-``/run`` gets (ledger polls) return ``{}``. After ``running_before_terminal``
    successful ``/run`` polls it returns a clean terminal.
    """

    def __init__(self, *, fail_at, running_before_terminal: int) -> None:
        self.base_url = "http://127.0.0.1:59999"
        self.fail_at = set(fail_at)
        self.running_before_terminal = running_before_terminal
        self.run_gets = 0
        self.ok_run_gets = 0
        self.paths: list[str] = []

    def get_json(self, path: str, *, params=None):
        self.paths.append(path)
        if not path.endswith("/run"):
            return {}
        self.run_gets += 1
        if self.run_gets in self.fail_at:
            raise SidecarUnreachable("transient blip")
        self.ok_run_gets += 1
        if self.ok_run_gets <= self.running_before_terminal:
            return {"running": True, "state": {"status": "running"}}
        return {"running": False,
                "state": {"status": "stopped", "stop_reason": "definition_of_done"}}

    def post_json(self, path: str, *, json=None, params=None):
        self.paths.append(path)
        return {"started": True}


def test_block_until_terminal_survives_transient_blip() -> None:
    client = _BlippyRunClient(fail_at={2}, running_before_terminal=1)
    slept: list[float] = []
    final = runstream.block_until_terminal(
        client, PID, sleep=slept.append, interval=0.1, max_ticks=50
    )
    assert runstream.is_terminal(final)
    assert final["state"]["stop_reason"] == "definition_of_done"
    # get#1 running, get#2 blip (tolerated, backoff), get#3 terminal.
    assert client.run_gets == 3
    assert runstream.POLL_ERROR_BACKOFF in slept  # backed off on the blip


def test_stream_run_survives_transient_blip(make_ctx) -> None:
    client = _BlippyRunClient(fail_at={2}, running_before_terminal=1)
    emitted: list[str] = []
    final = runstream.stream_run(
        client, make_ctx(project_id=PID),
        sleep=lambda _s: None, emit=emitted.append, max_ticks=50,
    )
    assert runstream.is_terminal(final)
    assert final["state"]["stop_reason"] == "definition_of_done"
    # The tolerated blip surfaced a retry note rather than aborting the stream.
    assert any("retry" in line.lower() for line in emitted)


def test_block_until_terminal_gives_up_after_tolerance() -> None:
    class _Down:
        base_url = "http://127.0.0.1:59999"

        def get_json(self, path, *, params=None):
            raise SidecarUnreachable("sidecar down")

    with pytest.raises(runstream.RunStreamDetached):
        runstream.block_until_terminal(
            _Down(), PID, sleep=lambda _s: None, max_ticks=50
        )


def test_run_detaches_when_poll_repeatedly_fails(make_ctx, monkeypatch) -> None:
    # A persistent poll failure DETACHES (exit 0), it does NOT surface as the
    # exit-9 sidecar-unreachable failure that would kill a still-live run.
    monkeypatch.setattr(runstream, "POLL_ERROR_BACKOFF", 0.0)

    class _Down:
        base_url = "http://127.0.0.1:59999"

        def __init__(self) -> None:
            self.posts: list[str] = []

        def get_json(self, path, *, params=None):
            raise SidecarUnreachable("sidecar down")

        def post_json(self, path, *, json=None, params=None):
            self.posts.append(path)
            return {"started": True}

    client = _Down()
    payload, _text = registry.dispatch(
        "run", client, make_ctx(project_id=PID),
        ["--room", "t", "--yes"], json_mode=True,
    )
    assert payload.get("_detached")
    assert registry.exit_code_for(payload) == 0  # graceful detach, NOT exit 9
    assert client.posts == [f"/coding/projects/{PID}/run"]
