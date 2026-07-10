"""F097 — Resume run: persist + recover the team, accept a fast-forwarded base."""
from pathlib import Path

from fastapi.testclient import TestClient

from errorta_app.routes.coding import _fingerprint_matches
from errorta_council.coding.ledger import LedgerStore


def _client(tmp_errorta_home: Path) -> TestClient:
    from errorta_app.server import app
    return TestClient(app, headers={"x-errorta-origin": "tauri-ui"})


_MEMBERS = [
    {"id": "m-1", "enabled": True, "role": "pm",
     "gateway_route_id": "fake.local.deterministic", "provider_kind": "local"},
    {"id": "m-2", "enabled": True, "role": "dev",
     "gateway_route_id": "fake.local.deterministic", "provider_kind": "local"},
]


def _make_interrupted(c: TestClient, pid: str, *, with_config: bool) -> LedgerStore:
    c.post("/coding/projects", json={"project_id": pid, "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    store = LedgerStore(pid)
    if with_config:
        store.set_run_config(members=_MEMBERS, room_id="demo-room", saved_at="t0")
    # interrupted, no persisted fingerprint -> integrity check is skipped
    store.set_run_state(status="interrupted")
    return store


# --- BE-1: persist round-trip -------------------------------------------------

def test_run_config_set_get_round_trip(tmp_errorta_home: Path) -> None:
    _client(tmp_errorta_home).post("/coding/projects", json={
        "project_id": "pcfg", "north_star": "n", "definition_of_done": "d", "target": "new"})
    store = LedgerStore("pcfg")
    assert store.get_run_config() == {}
    store.set_run_config(members=_MEMBERS, room_id="r", saved_at="t0")
    got = store.get_run_config()
    assert got["room_id"] == "r" and [m["id"] for m in got["members"]] == ["m-1", "m-2"]


def _confirm_run_setup(pid: str) -> None:
    # F121: a fresh project's first Start otherwise opens the readiness gate.
    from errorta_app.routes.coding import _set_run_setup_confirmed
    _set_run_setup_confirmed(LedgerStore(pid), True)


def test_start_persists_run_config(tmp_errorta_home: Path) -> None:
    c = _client(tmp_errorta_home)
    c.post("/coding/projects", json={"project_id": "pstart", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    _confirm_run_setup("pstart")
    r = c.post("/coding/projects/pstart/run", json={"members": _MEMBERS})
    assert r.status_code == 200, r.text
    cfg = LedgerStore("pstart").get_run_config()
    assert [m["id"] for m in cfg.get("members", [])] == ["m-1", "m-2"]


# --- BE-2: recover on resume + actionable error -------------------------------

def test_resume_recovers_persisted_members(tmp_errorta_home: Path) -> None:
    c = _client(tmp_errorta_home)
    _make_interrupted(c, "precover", with_config=True)
    r = c.post("/coding/projects/precover/run/resume", json={})  # empty body
    assert r.status_code == 200, r.text
    assert r.json().get("resumed") is True


def test_resume_body_override_wins(tmp_errorta_home: Path) -> None:
    c = _client(tmp_errorta_home)
    _make_interrupted(c, "poverride", with_config=True)
    override = [{"id": "x-9", "enabled": True, "role": "dev",
                 "gateway_route_id": "fake.local.deterministic", "provider_kind": "local"}]
    r = c.post("/coding/projects/poverride/run/resume", json={"members": override})
    assert r.status_code == 200, r.text
    # the explicit override is persisted as the new run_config
    assert "x-9" in [m["id"] for m in LedgerStore("poverride").get_run_config()["members"]]


def test_failed_resume_does_not_overwrite_run_config(tmp_errorta_home: Path) -> None:
    c = _client(tmp_errorta_home)
    c.post("/coding/projects", json={"project_id": "pfail", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    store = LedgerStore("pfail")
    store.set_run_config(members=_MEMBERS, room_id="demo-room", saved_at="t0")
    store.set_run_state(status="stopped")
    override = [{"id": "x-9", "enabled": True, "role": "dev",
                 "gateway_route_id": "fake.local.deterministic", "provider_kind": "local"}]

    r = c.post("/coding/projects/pfail/run/resume", json={"members": override})

    assert r.status_code == 409
    cfg = LedgerStore("pfail").get_run_config()
    assert cfg["room_id"] == "demo-room"
    assert [m["id"] for m in cfg["members"]] == ["m-1", "m-2"]


def test_resume_without_config_is_actionable(tmp_errorta_home: Path) -> None:
    c = _client(tmp_errorta_home)
    _make_interrupted(c, "pnocfg", with_config=False)
    r = c.post("/coding/projects/pnocfg/run/resume", json={})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "run_config_missing"


def test_fresh_start_without_members_keeps_generic_400(tmp_errorta_home: Path) -> None:
    c = _client(tmp_errorta_home)
    c.post("/coding/projects", json={"project_id": "pgen", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    _confirm_run_setup("pgen")  # past the gate, so the no-members 400 is reached
    r = c.post("/coding/projects/pgen/run", json={})  # start, not resume
    assert r.status_code == 400
    assert r.json()["detail"] == "no members (pass members or room_id)"


# --- BE-3: integrity accepts fast-forwarded base, rejects real corruption ------

class _FakeWs:
    """ws stub: master 'aaa' fast-forwarded to 'bbb' (aaa is ancestor of bbb)."""
    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return ancestor == "aaa" and descendant == "bbb"


def _fp(branches: dict, worktrees: dict | None = None) -> dict:
    return {"format": "coding-workspace-fingerprint.v1", "branches": branches,
            "worktrees": worktrees or {}, "primary": {"branch": "master", "head": ""}}


def test_resume_accepts_fast_forwarded_master() -> None:
    persisted = _fp({"master": "aaa", "task-t-1": "ccc"})
    current = _fp({"master": "bbb", "task-t-1": "ccc"})  # master advanced via merge
    assert _fingerprint_matches(persisted, current, ws=_FakeWs()) is True
    # ... and without a ws (can't probe ancestry) it fails closed:
    assert _fingerprint_matches(persisted, current, ws=None) is False


def test_resume_rejects_diverged_master() -> None:
    persisted = _fp({"master": "aaa", "task-t-1": "ccc"})
    diverged = _fp({"master": "zzz", "task-t-1": "ccc"})  # not a descendant
    assert _fingerprint_matches(persisted, diverged, ws=_FakeWs()) is False


def test_resume_rejects_changed_or_missing_task_branch() -> None:
    persisted = _fp({"master": "aaa", "task-t-1": "ccc"})
    # task branch rewritten — real corruption, even though master fast-forwarded
    rewritten = _fp({"master": "bbb", "task-t-1": "REWRITTEN"})
    assert _fingerprint_matches(persisted, rewritten, ws=_FakeWs()) is False
    # task branch dropped entirely
    dropped = _fp({"master": "bbb"})
    assert _fingerprint_matches(persisted, dropped, ws=_FakeWs()) is False


def test_resume_rejects_changed_worktree() -> None:
    persisted = _fp({"master": "aaa"}, {"t-1": {"branch": "task-t-1", "exists": True, "head": "ccc"}})
    changed = _fp({"master": "bbb"}, {"t-1": {"branch": "task-t-1", "exists": True, "head": "DIFF"}})
    assert _fingerprint_matches(persisted, changed, ws=_FakeWs()) is False
