"""F135 S4 — North Star inference (orientation scan)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_TAURI = {"x-errorta-origin": "tauri-ui"}


def _client() -> TestClient:
    from errorta_app.routes import coding as coding_routes
    app = FastAPI()
    app.include_router(coding_routes.router)
    return TestClient(app, headers=_TAURI)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "somerepo"
    repo.mkdir()
    (repo / "README.md").write_text("# Widget\n\nA CLI that widgets things.\n")
    (repo / "pyproject.toml").write_text("[project]\nname='widget'\n")
    # a secret that MUST NOT reach the model
    (repo / ".env").write_text("SECRET_TOKEN=sk-ant-super-secret-value\n")
    (repo / "app.py").write_text("print('hi')\n")
    return repo


def _project(project_id: str, repo: Path):
    from errorta_council.coding.ledger import LedgerStore
    store = LedgerStore(project_id)
    store.create_project(north_star="", definition_of_done="",
                         target="existing", repo_path=str(repo))
    return store


def test_scan_builds_and_persists_proposal_without_writing_repo(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    from errorta_council.coding import orientation_scan as osc
    repo = _repo(tmp_path)
    store = _project("p1", repo)
    seen = {}

    def fake_caller(member, prompt):
        seen["prompt"] = prompt
        return ('{"north_star": "Widget things well", '
                '"definition_of_done": "widgets ship", '
                '"summary": "a widget CLI", "detected_stack": ["python"], '
                '"suggested_first_tasks": ["add tests"]}')

    member = {"gateway_route_id": "local.qwen", "role": "answerer"}
    proposal = osc.run_orientation_scan(store, member=member, caller=fake_caller,
                                        repo_path=str(repo))
    assert proposal["north_star"] == "Widget things well"
    assert proposal["accepted"] is False
    assert "README.md" in proposal["source_refs"]
    # persisted
    assert store.get_orientation_proposal()["north_star"] == "Widget things well"
    # repo untouched (no new files, README unchanged)
    assert (repo / "README.md").read_text().startswith("# Widget")


def test_scan_never_leaks_secrets_to_the_model(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    from errorta_council.coding import orientation_scan as osc
    repo = _repo(tmp_path)
    store = _project("p2", repo)
    captured = {}

    def fake_caller(member, prompt):
        captured["prompt"] = prompt
        return '{"north_star": "x"}'

    osc.run_orientation_scan(store, member={"gateway_route_id": "r"},
                             caller=fake_caller, repo_path=str(repo))
    assert "sk-ant-super-secret-value" not in captured["prompt"]
    assert "SECRET_TOKEN" not in captured["prompt"]
    assert ".env" not in captured["prompt"]


def test_repo_reader_drops_credential_files_by_content_and_suffix(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    """F135 hardening: the blob goes to a model provider, so a credential file the
    name-based skip-set misses — a GCP-style ``credentials.json`` carrying a PEM
    private key, or a ``.p8`` key — must never reach the blob."""
    from errorta_tools.runner import repo_reader
    repo = tmp_path / "creds"
    repo.mkdir()
    (repo / "README.md").write_text("# App\n\nHas creds checked in by mistake.\n")
    # non-standard name, but the content is unmistakably a private key
    (repo / "credentials.json").write_text(
        '{"type": "service_account", "private_key": '
        '"-----BEGIN PRIVATE KEY-----\\nMIIsecretvalue\\n-----END PRIVATE KEY-----\\n"}')
    (repo / "AuthKey_ABC123.p8").write_text(
        "-----BEGIN PRIVATE KEY-----\nMIIapplekey\n-----END PRIVATE KEY-----\n")
    read = repo_reader.read_bounded(str(repo))
    assert "README.md" in read["files"]
    assert "credentials.json" not in read["files"]   # dropped by content check
    assert "AuthKey_ABC123.p8" not in read["files"]  # dropped by suffix
    assert "BEGIN PRIVATE KEY" not in read["blob"]
    assert "MIIsecretvalue" not in read["blob"]
    assert "MIIapplekey" not in read["blob"]


def test_empty_repo_yields_low_signal_without_calling_model(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    from errorta_council.coding import orientation_scan as osc
    empty = tmp_path / "empty"
    empty.mkdir()
    store = _project("p3", empty)

    def boom(member, prompt):  # must NOT be called
        raise AssertionError("model should not be called for an empty repo")

    proposal = osc.run_orientation_scan(store, member={"gateway_route_id": "r"},
                                        caller=boom, repo_path=str(empty))
    assert proposal["low_signal"] is True
    assert proposal["north_star"] == ""


def test_unparseable_reply_is_low_signal(tmp_errorta_home: Path, tmp_path: Path) -> None:
    from errorta_council.coding import orientation_scan as osc
    repo = _repo(tmp_path)
    store = _project("p4", repo)
    proposal = osc.run_orientation_scan(
        store, member={"gateway_route_id": "r"},
        caller=lambda m, p: "sorry I can't do that", repo_path=str(repo))
    assert proposal["low_signal"] is True
    assert proposal["north_star"] == ""


def test_resolve_scan_member_requires_route_without_team(
        tmp_errorta_home: Path) -> None:
    from errorta_council.coding.ledger import LedgerStore
    from errorta_council.coding.orientation_scan import ScanError, resolve_scan_member
    store = LedgerStore("p5")
    store.create_project(north_star="", definition_of_done="", target="new",
                         repo_path=None)
    with pytest.raises(ScanError):
        resolve_scan_member(store)  # no team, no route_id
    m = resolve_scan_member(store, route_id="local.qwen")
    assert m["gateway_route_id"] == "local.qwen"


def test_resolve_scan_member_prefers_pm(tmp_errorta_home: Path) -> None:
    from errorta_council.coding.ledger import LedgerStore
    from errorta_council.coding.orientation_scan import resolve_scan_member
    store = LedgerStore("p6")
    store.create_project(north_star="", definition_of_done="", target="new",
                         repo_path=None)
    store.set_run_config(members=[
        {"id": "m-dev", "coding_role": "dev", "gateway_route_id": "r.dev",
         "enabled": True},
        {"id": "m-pm", "coding_role": "pm", "gateway_route_id": "r.pm",
         "enabled": True},
    ])
    assert resolve_scan_member(store)["gateway_route_id"] == "r.pm"


def test_scan_route_400_when_no_route_resolvable(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    _project("p7", _repo(tmp_path))
    r = _client().post("/coding/projects/p7/orientation-scan", json={})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "no_route"


def test_scan_route_409_while_run_active(
        tmp_errorta_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _project("p8", _repo(tmp_path))
    from errorta_app.routes import coding as coding_routes
    monkeypatch.setattr(coding_routes, "_thread_alive", lambda pid: True)
    r = _client().post("/coding/projects/p8/orientation-scan",
                       json={"route_id": "local.qwen"})
    assert r.status_code == 409


def test_accept_proposal_promotes_north_star(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    store = _project("p9", _repo(tmp_path))
    store.save_orientation_proposal({
        "north_star": "Ship the widget", "definition_of_done": "green tests",
        "accepted": False, "accepted_at": None})
    client = _client()
    r = client.post("/coding/projects/p9/north-star-proposal/accept")
    assert r.status_code == 200
    assert r.json()["project"]["north_star"] == "Ship the widget"
    from errorta_council.coding.ledger import LedgerStore
    assert LedgerStore("p9").get_project().north_star == "Ship the widget"
    assert store.get_orientation_proposal()["accepted"] is True


def test_get_proposal_404_when_none(tmp_errorta_home: Path, tmp_path: Path) -> None:
    _project("p10", _repo(tmp_path))
    r = _client().get("/coding/projects/p10/north-star-proposal")
    assert r.status_code == 404


def test_accept_409_while_run_active(
        tmp_errorta_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """F135 review #3: accept promotes the run's North Star inputs, so it must 409
    mid-run rather than silently changing the North Star under the run."""
    store = _project("p11", _repo(tmp_path))
    store.save_orientation_proposal({"north_star": "x", "definition_of_done": "y",
                                     "accepted": False, "accepted_at": None})
    from errorta_app.routes import coding as coding_routes
    monkeypatch.setattr(coding_routes, "_thread_alive", lambda pid: True)
    r = _client().post("/coding/projects/p11/north-star-proposal/accept")
    assert r.status_code == 409
    # North Star was NOT changed
    from errorta_council.coding.ledger import LedgerStore
    assert LedgerStore("p11").get_project().north_star == ""
