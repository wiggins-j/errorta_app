"""F101 S0 — frozen runtime-preview contract canaries.

These tests pin the route table + the ``coding_runtime_profile.v1`` /
``RuntimeSession`` JSON shapes from
``docs/handoff/F101-frontend-engineer-handoff.md``. They are the coordination
seam between the backend (me) and the frontend engineer: a change to any of
these shapes must break a test here BEFORE it can silently drift the FE/BE
contract. When the real store (S1) and process manager (S3) land, their route
tests assert the same shapes; this file stays as the frozen-shape guard.

The fake (``tests/fakes/fake_runtime.py``) is the executable mirror of the
handoff doc — never a real process.
"""
from __future__ import annotations

from tests.fakes.fake_runtime import (
    PROFILE_KEYS,
    PROFILE_SCHEMA,
    RUNTIME_ROUTES,
    SESSION_KEYS,
    FakeRuntime,
    canned_profile,
    canned_session,
)


# --- route table (handoff doc §"The frozen route contract") ------------------

def test_route_table_matches_handoff_verbatim():
    expected = {
        ("GET", "/runtime/profiles"),
        ("PUT", "/runtime/profiles/{pid}"),
        ("POST", "/runtime/detect"),
        ("POST", "/runtime/{pid}/setup"),
        ("POST", "/runtime/{pid}/start"),
        ("POST", "/runtime/{pid}/stop"),
        ("GET", "/runtime/sessions/{sid}"),
        ("GET", "/runtime/sessions/{sid}/logs"),
        ("POST", "/runtime/{pid}/health-check"),
        ("POST", "/runtime/{pid}/test"),
    }
    assert set(RUNTIME_ROUTES) == expected
    # No duplicate (method, suffix) pairs.
    assert len(RUNTIME_ROUTES) == len(set(RUNTIME_ROUTES)) == 10


# --- profile shape (coding_runtime_profile.v1) -------------------------------

def test_profile_shape_is_frozen():
    prof = canned_profile()
    assert set(prof) == PROFILE_KEYS
    assert prof["schema_version"] == PROFILE_SCHEMA == "coding_runtime_profile.v1"


def test_profile_field_types_and_nesting():
    prof = canned_profile()
    assert isinstance(prof["setup"], list)
    assert all(isinstance(step, list) for step in prof["setup"])  # argv arrays
    assert isinstance(prof["start"], list)                        # single argv
    assert prof["stop"] is None
    assert set(prof["health"]) == {"type", "url", "timeout_seconds"}
    assert set(prof["demo"]) == {"type", "url"}
    port = prof["ports"][0]
    assert set(port) == {"name", "container_port", "preferred"}
    assert port["container_port"] is None
    assert isinstance(prof["env_required"], list)
    assert isinstance(prof["tests"], list)
    assert isinstance(prof["safety_warnings"], list)


def test_profile_enum_domains():
    assert canned_profile(kind="static")["kind"] == "static"
    assert canned_profile(runtime_mode="container")["runtime_mode"] == "container"
    assert canned_profile(sandbox="none")["sandbox"] == "none"


# --- session shape (RuntimeSession) ------------------------------------------

def test_session_shape_is_frozen():
    sess = canned_session(session_id="sess-0001")
    assert set(sess) == SESSION_KEYS


def test_session_initial_field_values():
    sess = canned_session(session_id="sess-0001")
    assert sess["state"] == "starting"
    assert sess["ended_at"] is None
    assert sess["exit_code"] is None
    assert sess["error"] is None
    assert sess["health_status"] is None
    assert isinstance(sess["pgid"], int)
    assert isinstance(sess["allocated_ports"], list)
    assert sess["sandbox_backend"] in {"seatbelt", "bwrap", "docker", "none"}


# --- FakeRuntime behavioral contract -----------------------------------------

def test_fake_seeds_a_default_profile():
    rt = FakeRuntime(project_id="demo")
    listing = rt.list_profiles()
    assert set(listing) == {"profiles"}
    assert listing["profiles"][0]["profile_id"] == "default"
    assert set(listing["profiles"][0]) == PROFILE_KEYS


def test_fake_detect_returns_proposed_profiles():
    out = FakeRuntime().detect()
    assert set(out) == {"proposed"}
    assert all(set(p) == PROFILE_KEYS for p in out["proposed"])


def test_fake_put_profile_round_trips_shape():
    rt = FakeRuntime(project_id="demo")
    out = rt.put_profile("custom", canned_profile(profile_id="custom"))
    assert set(out) == {"profile"}
    assert set(out["profile"]) == PROFILE_KEYS
    assert out["profile"]["profile_id"] == "custom"
    assert out["profile"]["project_id"] == "demo"
    assert rt.list_profiles()["profiles"][-1]["profile_id"] == "custom"


def test_fake_start_returns_session_and_state_advances_deterministically():
    rt = FakeRuntime()
    started = rt.start("default")
    assert set(started) == {"session"}
    sid = started["session"]["session_id"]
    assert started["session"]["state"] == "starting"

    # starting -> running -> healthy, then sticks at healthy.
    assert rt.get_session(sid)["session"]["state"] == "running"
    healthy = rt.get_session(sid)["session"]
    assert healthy["state"] == "healthy"
    assert healthy["health_status"] == {"ok": True, "detail": "200 OK"}
    assert rt.get_session(sid)["session"]["state"] == "healthy"


def test_fake_get_unknown_session_is_none():
    assert FakeRuntime().get_session("nope") is None


def test_fake_stop_marks_session_stopped():
    rt = FakeRuntime()
    sid = rt.start("default")["session"]["session_id"]
    out = rt.stop("default")
    assert out == {"stopped": True}
    ended = rt.get_session(sid)["session"]
    assert ended["state"] == "stopped"
    assert ended["ended_at"] is not None
    assert ended["exit_code"] == 0


def test_fake_logs_shape():
    out = FakeRuntime().get_logs("sess-0001")
    assert set(out) == {"lines", "truncated"}
    assert isinstance(out["lines"], list)
    assert all(isinstance(line, str) for line in out["lines"])
    assert out["truncated"] is False


def test_fake_health_check_shape():
    out = FakeRuntime().health_check("default")
    assert set(out) == {"health_status"}
    assert set(out["health_status"]) == {"ok", "detail"}


def test_fake_run_test_shape():
    out = FakeRuntime().run_test("default", "runtime_start")
    assert set(out) == {"result"}
    assert out["result"]["kind"] == "runtime_start"
    assert out["result"]["passed"] is True
