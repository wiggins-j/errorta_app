"""F101 S1 — runtime profile/session store + detectors + read routes.

Covers the persistence layer (RuntimeProfileStore over runtime-profiles.json +
runtime-sessions.jsonl), the fail-closed profile validation, the workspace
detectors, and the GET/PUT/detect read routes (Tauri-origin guarded). All shapes
re-pinned against the frozen contract (S0 canaries are the authoritative pin).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runtime import (
    PROFILE_SCHEMA,
    RuntimeProfile,
    RuntimeProfileStore,
    RuntimeSession,
    RuntimeValidationError,
    detect,
    validate_profile,
)
from tests.fakes.fake_runtime import PROFILE_KEYS, SESSION_KEYS

_TAURI = {"x-errorta-origin": "tauri-ui"}


def _client(*, headers: dict[str, str] | None = None) -> TestClient:
    from errorta_app.routes import coding as coding_routes

    app = FastAPI()
    app.include_router(coding_routes.router)
    return TestClient(app, headers=headers)


def _project(project_id: str, *, with_workspace: bool = False) -> LedgerStore:
    store = LedgerStore(project_id)
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    if with_workspace:
        from errorta_council.coding.workspace import CodingWorkspace
        ws = CodingWorkspace(project_id, store)
        ws.setup(target="new", repo_path=None)
    return store


# --- store ------------------------------------------------------------------ #

def test_store_upsert_and_list_round_trips_shape(tmp_errorta_home: Path) -> None:
    store = _project("rp1")
    rstore = RuntimeProfileStore.for_ledger(store)
    assert rstore.list_profiles() == []

    prof = validate_profile(
        {"kind": "web", "runtime_mode": "managed_local",
         "setup": [["npm", "install"]], "start": ["npm", "run", "dev"],
         "sandbox": "auto"},
        profile_id="default", project_id="rp1")
    saved = rstore.upsert_profile(prof)
    assert isinstance(saved, RuntimeProfile)

    got = rstore.get_profile("default")
    assert got is not None
    assert set(got.to_dict()) == PROFILE_KEYS
    assert got.to_dict()["schema_version"] == PROFILE_SCHEMA
    assert got.start == ["npm", "run", "dev"]
    assert [p.profile_id for p in rstore.list_profiles()] == ["default"]


def test_store_upsert_overwrites_same_id(tmp_errorta_home: Path) -> None:
    store = _project("rp2")
    rstore = RuntimeProfileStore.for_ledger(store)
    rstore.upsert_profile(validate_profile(
        {"kind": "web", "start": ["a"]}, profile_id="default", project_id="rp2"))
    rstore.upsert_profile(validate_profile(
        {"kind": "cli", "start": ["b"]}, profile_id="default", project_id="rp2"))
    profiles = rstore.list_profiles()
    assert len(profiles) == 1
    assert profiles[0].kind == "cli" and profiles[0].start == ["b"]


def test_store_unknown_keys_round_trip_via_extras(tmp_errorta_home: Path) -> None:
    store = _project("rp3")
    rstore = RuntimeProfileStore.for_ledger(store)
    prof = validate_profile(
        {"kind": "web", "start": ["x"], "future_field": {"k": 1}},
        profile_id="default", project_id="rp3")
    rstore.upsert_profile(prof)
    got = rstore.get_profile("default")
    assert got.to_dict()["future_field"] == {"k": 1}


def test_store_delete_profile(tmp_errorta_home: Path) -> None:
    store = _project("rp4")
    rstore = RuntimeProfileStore.for_ledger(store)
    rstore.upsert_profile(validate_profile(
        {"kind": "web", "start": ["x"]}, profile_id="default", project_id="rp4"))
    assert rstore.delete_profile("default") is True
    assert rstore.delete_profile("default") is False
    assert rstore.list_profiles() == []


def test_session_append_project_and_update(tmp_errorta_home: Path) -> None:
    store = _project("rs1")
    rstore = RuntimeProfileStore.for_ledger(store)
    sid = rstore.new_session_id()
    rstore.append_session(RuntimeSession(
        session_id=sid, profile_id="default", state="starting",
        sandbox_backend="seatbelt", allocated_ports=[5173]))
    got = rstore.get_session(sid)
    assert got is not None
    assert set(got.to_dict()) == SESSION_KEYS
    assert got.state == "starting"

    rstore.update_session(sid, state="healthy",
                          health_status={"ok": True, "detail": "200 OK"})
    after = rstore.get_session(sid)
    assert after.state == "healthy"
    assert after.health_status == {"ok": True, "detail": "200 OK"}
    # last-event-per-id projection: still exactly one session in the listing.
    assert [s.session_id for s in rstore.list_sessions()] == [sid]


def test_update_unknown_session_raises(tmp_errorta_home: Path) -> None:
    from errorta_council.coding.runtime import RuntimeError_
    store = _project("rs2")
    rstore = RuntimeProfileStore.for_ledger(store)
    with pytest.raises(RuntimeError_):
        rstore.update_session("nope", state="healthy")


# --- validation (fail-closed) ----------------------------------------------- #

def test_validate_rejects_bad_enums(tmp_errorta_home: Path) -> None:
    for bad in (
        {"kind": "bogus", "start": ["x"]},
        {"runtime_mode": "bogus", "start": ["x"]},
        {"sandbox": "bogus", "start": ["x"]},
        {"created_by": "bogus", "start": ["x"]},
    ):
        with pytest.raises(RuntimeValidationError):
            validate_profile(bad, profile_id="p", project_id="x")


def test_validate_rejects_unsafe_working_dir(tmp_errorta_home: Path) -> None:
    for wd in ("/etc", "../escape", "a/../../b"):
        with pytest.raises(RuntimeValidationError):
            validate_profile({"working_dir": wd, "start": ["x"]},
                             profile_id="p", project_id="x")


def test_validate_rejects_non_argv_start(tmp_errorta_home: Path) -> None:
    with pytest.raises(RuntimeValidationError):
        validate_profile({"start": "npm run dev"}, profile_id="p", project_id="x")
    with pytest.raises(RuntimeValidationError):
        validate_profile({"setup": [["ok"], "not-a-list"], "start": ["x"]},
                         profile_id="p", project_id="x")


def test_validate_requires_start_for_non_static(tmp_errorta_home: Path) -> None:
    with pytest.raises(RuntimeValidationError):
        validate_profile({"runtime_mode": "managed_local", "start": []},
                         profile_id="p", project_id="x")
    # static is allowed to have no start (Errorta opens the artifact directly).
    ok = validate_profile({"runtime_mode": "static", "kind": "static", "start": []},
                          profile_id="p", project_id="x")
    assert ok.start == []


def test_validate_accepts_binary_profile(tmp_errorta_home: Path) -> None:
    """Detector-produced native-binary profiles must survive route validation."""
    profile = validate_profile(
        {"kind": "binary", "runtime_mode": "managed_local", "start": ["./app"]},
        profile_id="native",
        project_id="x",
    )
    assert profile.kind == "binary"


def test_validate_forces_identity_and_schema(tmp_errorta_home: Path) -> None:
    prof = validate_profile(
        {"profile_id": "SPOOFED", "project_id": "SPOOFED", "start": ["x"]},
        profile_id="real", project_id="proj")
    assert prof.profile_id == "real"
    assert prof.project_id == "proj"
    assert prof.schema_version == PROFILE_SCHEMA
    assert prof.updated_at  # stamped


# --- detectors -------------------------------------------------------------- #

def test_detect_node_vite(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts":{"dev":"vite"},"devDependencies":{"vite":"^5"}}')
    props = detect(tmp_path, project_id="p")
    assert len(props) == 1
    p = props[0]
    assert p.profile_id == "default" and p.kind == "web"
    assert p.start == ["npm", "run", "dev"]
    assert p.ports[0]["preferred"] == 5173
    assert p.health["type"] == "http"


def test_detect_node_next_port_3000(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts":{"dev":"next dev"},"dependencies":{"next":"^14"}}')
    props = detect(tmp_path, project_id="p")
    assert props[0].ports[0]["preferred"] == 3000


def test_detect_python_app_py(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("flask\n")
    (tmp_path / "app.py").write_text("print('x')\n")
    props = detect(tmp_path, project_id="p")
    assert props[0].kind == "api"
    assert props[0].start == ["python", "app.py"]
    assert props[0].setup == [["pip", "install", "-r", "requirements.txt"]]


def test_detect_python_cli_main(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('x')\n")
    props = detect(tmp_path, project_id="p")
    assert props[0].kind == "cli"
    assert props[0].start == ["python", "main.py"]
    assert props[0].demo["type"] == "command"


def test_detect_static_only(tmp_path: Path) -> None:
    # F101-01: a root index.html now yields a SERVED managed_local profile
    # (python -m http.server over loopback), not a file:// open.
    (tmp_path / "index.html").write_text("<html></html>")
    props = detect(tmp_path, project_id="p")
    assert len(props) == 1
    p = props[0]
    assert p.profile_id == "default"
    assert p.kind == "static" and p.runtime_mode == "managed_local"
    assert p.working_dir == "."
    assert p.start == ["python", "-m", "http.server", "{port}",
                       "--bind", "127.0.0.1"]
    assert p.health["type"] == "http"
    assert p.health["url"] == "http://127.0.0.1:{port}"
    assert p.demo == {"type": "url", "url": "http://127.0.0.1:{port}"}
    assert p.ports == [{"name": "web", "container_port": None, "preferred": 8000}]


def test_detect_static_is_secondary_when_web_present(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts":{"dev":"vite"}}')
    (tmp_path / "index.html").write_text("<html></html>")
    props = detect(tmp_path, project_id="p")
    ids = [p.profile_id for p in props]
    assert ids == ["default", "static"]
    assert props[0].kind == "web" and props[1].kind == "static"


def test_detect_container_proposal(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n")
    props = detect(tmp_path, project_id="p")
    assert props[0].profile_id == "container"
    assert props[0].kind == "container"
    # S6: the container proposal is now executable (real docker argv) with a
    # stable name + explicit `docker rm -f` teardown.
    assert props[0].start[:2] == ["docker", "run"]
    assert "--name" in props[0].start and "errorta-preview-p" in props[0].start
    assert props[0].setup == [["docker", "build", "-t", "errorta-preview-p", "."]]
    assert props[0].stop == ["docker", "rm", "-f", "errorta-preview-p"]
    assert any("Docker" in w for w in props[0].safety_warnings)


def test_detect_container_compose_proposal(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    props = detect(tmp_path, project_id="p")
    assert props[0].start == ["docker", "compose", "up", "--build"]
    assert props[0].stop == ["docker", "compose", "down"]


def test_detect_empty_workspace_is_honest_empty(tmp_path: Path) -> None:
    assert detect(tmp_path, project_id="p") == []


# --- routes ----------------------------------------------------------------- #

def test_get_profiles_empty(tmp_errorta_home: Path) -> None:
    _project("routes1")
    c = _client(headers=_TAURI)
    r = c.get("/coding/projects/routes1/runtime/profiles")
    assert r.status_code == 200
    assert r.json() == {"profiles": []}


def test_get_profiles_unknown_project_404(tmp_errorta_home: Path) -> None:
    c = _client(headers=_TAURI)
    assert c.get("/coding/projects/ghost/runtime/profiles").status_code == 404


def test_put_profile_validates_and_persists(tmp_errorta_home: Path) -> None:
    _project("routes2")
    c = _client(headers=_TAURI)
    r = c.put("/coding/projects/routes2/runtime/profiles/default",
              json={"profile": {"kind": "web", "runtime_mode": "managed_local",
                                "setup": [["npm", "install"]],
                                "start": ["npm", "run", "dev"], "sandbox": "auto"}})
    assert r.status_code == 200, r.text
    body = r.json()["profile"]
    assert set(body) == PROFILE_KEYS
    assert body["profile_id"] == "default" and body["project_id"] == "routes2"
    # round-trips through GET
    listed = c.get("/coding/projects/routes2/runtime/profiles").json()["profiles"]
    assert len(listed) == 1 and listed[0]["start"] == ["npm", "run", "dev"]


def test_put_profile_requires_tauri_origin(tmp_errorta_home: Path) -> None:
    _project("routes3")
    c = _client()  # no origin header
    r = c.put("/coding/projects/routes3/runtime/profiles/default",
              json={"profile": {"kind": "web", "start": ["x"]}})
    assert r.status_code == 403
    assert r.json()["detail"] == "origin_not_authorized"


def test_put_profile_invalid_body_422(tmp_errorta_home: Path) -> None:
    _project("routes4")
    c = _client(headers=_TAURI)
    r = c.put("/coding/projects/routes4/runtime/profiles/default",
              json={"profile": {"kind": "web", "start": ["x"], "working_dir": "/etc"}})
    assert r.status_code == 422


def test_put_profile_rejects_unsafe_profile_id(tmp_errorta_home: Path) -> None:
    # Path-traversal ids ("..", "/") are normalized away by URL routing before
    # they reach the handler (-> 404); the safe_segment guard is defense-in-depth
    # for any that slip through. What DOES reach the handler is an over-long id,
    # rejected by the explicit length guard.
    _project("routes5")
    c = _client(headers=_TAURI)
    r = c.put(f"/coding/projects/routes5/runtime/profiles/{'x' * 65}",
              json={"profile": {"kind": "web", "start": ["x"]}})
    assert r.status_code == 422


def test_detect_route_against_real_workspace(tmp_errorta_home: Path) -> None:
    _project("routes6", with_workspace=True)
    from errorta_council.coding.workspace import CodingWorkspace
    store = LedgerStore("routes6")
    ws = CodingWorkspace("routes6", store)
    ws.set_target("new")
    (ws.root() / "package.json").write_text('{"scripts":{"dev":"vite"}}')

    c = _client(headers=_TAURI)
    r = c.post("/coding/projects/routes6/runtime/detect")
    assert r.status_code == 200, r.text
    proposed = r.json()["proposed"]
    assert len(proposed) == 1
    assert proposed[0]["kind"] == "web"
    assert set(proposed[0]) == PROFILE_KEYS


def test_detect_route_no_workspace_is_empty(tmp_errorta_home: Path) -> None:
    _project("routes7")  # no worktree created
    c = _client(headers=_TAURI)
    r = c.post("/coding/projects/routes7/runtime/detect")
    assert r.status_code == 200
    assert r.json() == {"proposed": []}


def test_detect_route_requires_tauri_origin(tmp_errorta_home: Path) -> None:
    _project("routes8")
    c = _client()
    assert c.post("/coding/projects/routes8/runtime/detect").status_code == 403
