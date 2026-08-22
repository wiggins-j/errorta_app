"""``/liverun/*`` — the sidecar-side operator control surface.

Driven against a FAKE ``LiveRunManager`` injected through the router's lazy
``_manager`` seam, so no profile is ever loaded off disk and no supervisor thread
is ever started: these tests are about the door, not the machine behind it. The
manager's own behaviour has its own suite (``tests/liverun/test_supervisor.py``).

What is locked here: the origin/token guard on every route, the profile-name
422, the deliberate 200-with-a-reason on a refused start, the operator stop note
being carried *inside* an ``operator_stop`` reason it cannot escape, and the two
pause markers being reported even when nothing is running.
"""
from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any, Iterator

import pytest

# ``starlette.testclient`` emits a deprecation warning at IMPORT time in this
# environment (it wants the httpx2 shim). That fires during collection, before
# any filterwarnings mark could apply, so a ``-W error`` run of this file would
# fail to collect. Swallow it at the import itself; every warning raised by the
# code under test still errors normally.
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

CLI = {"x-errorta-origin": "cli"}
UI = {"x-errorta-origin": "tauri-ui"}

# Every route, with a body that is valid for it — used by the guard sweeps so a
# new route cannot be added without inheriting the origin check.
ROUTES: list[tuple[str, str, dict | None]] = [
    ("GET", "/liverun/profiles", None),
    ("POST", "/liverun/start", {"profile": "osrs"}),
    ("POST", "/liverun/stop", {}),
    ("GET", "/liverun/status", None),
    ("POST", "/liverun/resume", {"profile": "osrs"}),
    ("POST", "/liverun/fix/pause", {"profile": "osrs"}),
    ("POST", "/liverun/fix/resume", {"profile": "osrs"}),
]

# The routes that take a profile name and REQUIRE it (the 422 surface).
NAMED_ROUTES = ["/liverun/start", "/liverun/resume",
                "/liverun/fix/pause", "/liverun/fix/resume"]

BAD_NAMES = [
    "../escape",          # path traversal — the name reaches the filesystem
    "-leading-dash",      # must start alphanumeric
    "has space",
    "semi;colon",
    "a" * 65,             # over the 64-char cap
    "",                   # blank
]


class FakeManager:
    """Records every call and answers with a canned dict per verb."""

    def __init__(self, **answers: Any) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.answers = answers

    def _answer(self, verb: str, default: Any) -> Any:
        value = self.answers.get(verb, default)
        return dict(value) if isinstance(value, dict) else value

    def start(self, profile_name, **kw):
        self.calls.append(("start", (profile_name,), kw))
        return self._answer("start", {"status": "started", "run_id": "run-1"})

    def stop(self, **kw):
        self.calls.append(("stop", (), kw))
        return self._answer("stop", {"status": "stopping", "run_id": "run-1"})

    def status(self, **kw):
        self.calls.append(("status", (), kw))
        return self._answer("status", {"status": "empty", "last": None})

    def resume(self, profile_name):
        self.calls.append(("resume", (profile_name,), {}))
        return self._answer("resume", {"status": "resumed"})

    def pause_fix(self, profile_name):
        self.calls.append(("pause_fix", (profile_name,), {}))
        return self._answer("pause_fix", {"status": "paused", "profile": profile_name})

    def resume_fix(self, profile_name):
        self.calls.append(("resume_fix", (profile_name,), {}))
        return self._answer("resume_fix", {"status": "resumed", "profile": profile_name})


@pytest.fixture
def client(tmp_errorta_home: Path) -> Iterator[TestClient]:
    from errorta_app.server import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> FakeManager:
    fake = FakeManager()
    from errorta_app.routes import liverun as routes

    monkeypatch.setattr(routes, "_manager", lambda: fake)
    return fake


def _call(client: TestClient, method: str, path: str, body: dict | None,
          headers: dict) -> Any:
    if method == "GET":
        return client.get(path, headers=headers)
    return client.post(path, json=body or {}, headers=headers)


# --------------------------------------------------------------------------- #
# The origin guard — on every route, not just the mutations.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("method,path,body", ROUTES)
def test_no_origin_header_is_refused(client, manager, method, path, body) -> None:
    resp = _call(client, method, path, body, {})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "origin_not_authorized"
    assert manager.calls == []  # refused BEFORE the manager is touched


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_untrusted_origin_is_refused(client, manager, method, path, body) -> None:
    resp = _call(client, method, path, body, {"x-errorta-origin": "evil"})
    assert resp.status_code == 403
    assert manager.calls == []


@pytest.mark.parametrize("origin", [CLI, UI])
def test_both_trusted_origins_are_accepted(client, manager, origin) -> None:
    # The CLI origin is a first-class caller here — that is the point of the router.
    assert client.get("/liverun/status", headers=origin).status_code == 200


# --------------------------------------------------------------------------- #
# Profile-name validation, at the edge.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("path", NAMED_ROUTES)
@pytest.mark.parametrize("name", BAD_NAMES)
def test_bad_profile_name_is_422_and_never_reaches_the_manager(
    client, manager, path, name
) -> None:
    resp = client.post(path, json={"profile": name}, headers=CLI)
    assert resp.status_code == 422, (path, name, resp.text)
    assert manager.calls == []


@pytest.mark.parametrize("name", ["osrs", "osrs-ng", "osrs_2.prod", "a", "A9"])
def test_good_profile_names_pass(client, manager, name) -> None:
    assert client.post("/liverun/start", json={"profile": name},
                       headers=CLI).status_code == 200
    assert manager.calls[0][1] == (name,)


def test_bad_profile_name_on_the_status_query_is_422(client, manager) -> None:
    resp = client.get("/liverun/status", params={"profile": "../escape"}, headers=CLI)
    assert resp.status_code == 422
    assert manager.calls == []


def test_bad_profile_name_on_stop_is_422(client, manager) -> None:
    resp = client.post("/liverun/stop", json={"profile": "../escape"}, headers=CLI)
    assert resp.status_code == 422
    assert manager.calls == []


def test_stop_with_no_selector_is_allowed(client, manager) -> None:
    # No profile, no project: the manager addresses the newest live run.
    assert client.post("/liverun/stop", json={}, headers=CLI).status_code == 200
    assert manager.calls[0][2]["profile_name"] is None


# --------------------------------------------------------------------------- #
# Profiles.
# --------------------------------------------------------------------------- #

def test_profiles_lists_what_list_profiles_returns(client, monkeypatch) -> None:
    from errorta_app.routes import liverun as routes

    rows = [{"name": "osrs", "valid": True, "error": None},
            {"name": "broken", "valid": False, "error": "banned_token"}]
    monkeypatch.setattr(routes, "_list_profiles", lambda: rows)
    resp = client.get("/liverun/profiles", headers=CLI)
    assert resp.status_code == 200
    assert resp.json() == {"profiles": rows}


# --------------------------------------------------------------------------- #
# Start — a refusal is an ANSWER, not a transport error.
# --------------------------------------------------------------------------- #

def test_refused_start_is_200_with_the_reason_intact(client, monkeypatch) -> None:
    fake = FakeManager(start={"status": "refused", "reason": "already_running",
                              "run_id": "run-7"})
    from errorta_app.routes import liverun as routes

    monkeypatch.setattr(routes, "_manager", lambda: fake)
    resp = client.post("/liverun/start", json={"profile": "osrs"}, headers=CLI)
    assert resp.status_code == 200, resp.text
    # A 4xx would flatten this into an error string and lose the run_id.
    assert resp.json() == {"status": "refused", "reason": "already_running",
                           "run_id": "run-7"}


def test_start_passes_the_project_id_through(client, manager) -> None:
    client.post("/liverun/start", json={"profile": "osrs", "project_id": "senditai-ng"},
                headers=CLI)
    assert manager.calls == [("start", ("osrs",), {"project_id": "senditai-ng"})]


def test_start_blank_project_id_becomes_none(client, manager) -> None:
    client.post("/liverun/start", json={"profile": "osrs", "project_id": "  "}, headers=CLI)
    assert manager.calls[0][2] == {"project_id": None}


# --------------------------------------------------------------------------- #
# Stop — the operator's note can never impersonate a fixable reason.
# --------------------------------------------------------------------------- #

def test_stop_with_no_note_uses_the_plain_operator_reason(client, manager) -> None:
    client.post("/liverun/stop", json={}, headers=CLI)
    assert manager.calls[0][2]["reason"] == "operator_stop"


def test_operator_note_is_carried_inside_the_operator_stop_reason(client, manager) -> None:
    client.post("/liverun/stop", json={"reason": "swapping the client build"}, headers=CLI)
    assert manager.calls[0][2]["reason"] == "operator_stop:swapping the client build"


@pytest.mark.parametrize("note", ["stall: it hung", "launch_step_failed: nope"])
def test_a_note_cannot_forge_a_fix_loop_trigger(client, manager, note) -> None:
    """``Supervisor`` starts a fix cycle on a reason matching
    ``^(stall|launch_step_failed):``. An operator's note must never be able to
    talk the fix loop into a cycle for a stop a human ordered."""
    import re

    client.post("/liverun/stop", json={"reason": note}, headers=CLI)
    reason = manager.calls[0][2]["reason"]
    assert reason.startswith("operator_stop:")
    assert not re.match(r"^(stall|launch_step_failed):", reason)


def test_a_long_multiline_note_is_folded_and_capped(client, manager) -> None:
    client.post("/liverun/stop", json={"reason": "a\nb   c" + " x" * 200}, headers=CLI)
    reason = manager.calls[0][2]["reason"]
    assert "\n" not in reason
    assert reason.startswith("operator_stop:a b c")
    assert len(reason) <= len("operator_stop:") + 120


# --------------------------------------------------------------------------- #
# Status — the live snapshot, plus the holds an operator needs when it is empty.
# --------------------------------------------------------------------------- #

def test_status_passes_both_selectors(client, manager) -> None:
    client.get("/liverun/status", params={"profile": "osrs", "project_id": "p1"},
               headers=CLI)
    assert manager.calls == [("status", (), {"profile_name": "osrs", "project_id": "p1"})]


def test_empty_status_still_answers_the_hold_question(client, monkeypatch,
                                                      tmp_errorta_home) -> None:
    """"Nothing is running" and "nothing CAN run, it is paused" are different
    answers, and the empty snapshot alone cannot tell them apart."""
    from errorta_app.routes import liverun as routes
    from errorta_liverun.supervisor import fix_paused_marker, paused_marker

    marker = paused_marker("osrs")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("held", encoding="utf-8")

    fake = FakeManager(status={"status": "empty", "last": None})
    monkeypatch.setattr(routes, "_manager", lambda: fake)
    body = client.get("/liverun/status", params={"profile": "osrs"}, headers=CLI).json()
    assert body["paused"] is True
    assert body["fix_paused"] is False
    assert not fix_paused_marker("osrs").exists()
    assert body["fix_cycles_today"] is None


def test_empty_status_with_no_selector_reports_no_holds(client, manager) -> None:
    body = client.get("/liverun/status", headers=CLI).json()
    assert body["paused"] is False and body["fix_paused"] is False


def test_empty_status_takes_the_profile_from_the_last_run(client, monkeypatch,
                                                          tmp_errorta_home) -> None:
    from errorta_app.routes import liverun as routes
    from errorta_liverun.supervisor import paused_marker

    marker = paused_marker("osrs")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("held", encoding="utf-8")
    fake = FakeManager(status={"status": "empty",
                               "last": {"profile_name": "osrs", "phase": "failed"}})
    monkeypatch.setattr(routes, "_manager", lambda: fake)
    body = client.get("/liverun/status", headers=CLI).json()
    assert body["profile"] == "osrs"
    assert body["paused"] is True


def test_a_live_snapshot_is_passed_through_and_not_overwritten(client, monkeypatch,
                                                               tmp_errorta_home) -> None:
    from errorta_app.routes import liverun as routes

    snapshot = {
        "status": "live", "run_id": "r1", "profile": "osrs", "phase": "watching",
        "reason": None, "elapsed_s": 42.0,
        "probes": {"api": {"last_ok_age_s": 3.0, "stall_after_s": 90, "on_stall": "stop"}},
        "caps": {"would_refuse": None}, "literals": {},
        "fix_cycle": 0, "fix_cycles_today": 2, "fix_cap": 3,
        # The supervisor already answered this one; the route must not clobber it.
        "fix_paused": True,
    }
    monkeypatch.setattr(routes, "_manager", lambda: FakeManager(status=snapshot))
    body = client.get("/liverun/status", headers=CLI).json()
    assert body["phase"] == "watching"
    assert body["fix_cycles_today"] == 2
    assert body["fix_paused"] is True          # snapshot wins over the marker read
    assert body["paused"] is False             # marker read fills the gap
    assert body["probes"]["api"]["last_ok_age_s"] == 3.0


# --------------------------------------------------------------------------- #
# The fix-loop holds.
# --------------------------------------------------------------------------- #

def test_fix_pause_and_resume_reach_their_manager_verbs(client, manager) -> None:
    client.post("/liverun/fix/pause", json={"profile": "osrs"}, headers=CLI)
    client.post("/liverun/fix/resume", json={"profile": "osrs"}, headers=CLI)
    assert [c[0] for c in manager.calls] == ["pause_fix", "resume_fix"]
    assert all(c[1] == ("osrs",) for c in manager.calls)


def test_resume_reaches_the_manager(client, manager) -> None:
    resp = client.post("/liverun/resume", json={"profile": "osrs"}, headers=CLI)
    assert resp.json() == {"status": "resumed"}
    assert manager.calls == [("resume", ("osrs",), {})]


# --------------------------------------------------------------------------- #
# Residency: a live run is a local-disk data plane end to end.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("method,path,body", ROUTES)
def test_remote_residency_refuses_every_route(client, manager, monkeypatch,
                                              method, path, body) -> None:
    from errorta_app.routes import _residency_proxy

    monkeypatch.setattr(_residency_proxy, "active_remote_base",
                        lambda: ("http://127.0.0.1:1", {}))
    resp = _call(client, method, path, body, CLI)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "residency_unsupported_path"
    assert manager.calls == []


# --------------------------------------------------------------------------- #
# The manager seam is the real singleton in production.
# --------------------------------------------------------------------------- #

def test_manager_seam_resolves_to_the_slack_bridge_singleton() -> None:
    """One manager, two doors. If these ever diverge, a run started from the
    terminal becomes invisible to Slack (and unstoppable from it)."""
    from errorta_app.routes.liverun import _manager
    from errorta_liverun.supervisor import live_run_manager
    from errorta_slack.tools import _liverun as _slack_manager

    assert _manager() is live_run_manager is _slack_manager()


def test_router_is_mounted_on_the_app(client) -> None:
    """Read off the generated schema, not ``app.routes``: this FastAPI resolves
    ``include_router`` lazily, so the route objects are not there to inspect."""
    schema = client.get("/openapi.json").json()["paths"]
    for _method, path, _body in ROUTES:
        assert path in schema, path
