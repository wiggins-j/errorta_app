"""F088-04/05/06 — authority model, project isolation, and residency guard.

The marquee constraint: a raw dev/reviewer/tester claim is NEVER project truth,
retrieval is project-scoped by default, and the sync route refuses to write
local data under remote residency.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.workspace import CodingWorkspace
from errorta_project_grounding.memory_store import MemoryQuery, ProjectMemoryStore
from errorta_project_grounding.update_pipeline import sync_from_ledger


def _project(tmp: Path, pid: str):
    s = LedgerStore(pid, root=tmp)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    ws = CodingWorkspace(pid, s)
    ws.setup(target="new", repo_path=None)
    return s, ws


def test_false_dev_claim_is_claim_only_and_excluded_from_default(tmp_path: Path) -> None:
    s, ws = _project(tmp_path, "a1")
    t = s.add_task(title="impl", role="dev")
    # a dev asserts something untrue — recorded as a TURN (prose), never validated
    s.record_turn(role="dev", member_id="m-dev", task_id=t.task_id,
                  prompt="implement add", response="The database is definitely Postgres.",
                  outcome="written")
    sync_from_ledger(s, workspace=ws)
    mem = ProjectMemoryStore("a1", root=tmp_path)

    # default retrieval (and durable-only) must NOT surface the claim
    default_hits = mem.query(MemoryQuery(limit=500))
    assert not any("Postgres" in i.content for i in default_hits)
    durable = mem.query(MemoryQuery(authorities=("durable_truth",), limit=500))
    assert not any("Postgres" in i.content for i in durable)

    # it IS retained as a claim for audit
    claims = mem.query(MemoryQuery(authorities=("claim",), limit=500))
    assert any("Postgres" in i.content for i in claims)
    assert all(i.authority == "claim" for i in claims)


def test_retrieval_is_project_scoped_by_default(tmp_path: Path) -> None:
    s_b, _ = _project(tmp_path, "projB")
    s_b.record_decision(title="B secret", context="pm_decision", choice="pm_decision",
                        rationale="belongs to project B only")
    sync_from_ledger(s_b)

    _project(tmp_path, "projA")  # empty project A
    mem_a = ProjectMemoryStore("projA", root=tmp_path)
    hits = mem_a.query(MemoryQuery(limit=500))
    assert hits == []  # nothing from project B leaks into A's store
    assert all(i.project_id == "projA" for i in hits)


@pytest.fixture
def client(tmp_errorta_home) -> TestClient:
    from errorta_app.routes import coding as coding_routes
    app = FastAPI()
    app.include_router(coding_routes.router)
    return TestClient(app, headers={"x-errorta-origin": "tauri-ui"})


def test_memory_sync_route_refuses_under_remote_residency(client: TestClient) -> None:
    from errorta_residency import config as residency_config
    client.post("/coding/projects", json={"project_id": "res1", "north_star": "n",
                                           "definition_of_done": "d", "target": "new"})
    # local mode: sync succeeds
    ok = client.post("/coding/projects/res1/grounding/memory/sync")
    assert ok.status_code == 200, ok.text

    # remote residency: a local-disk write must fail closed (409)
    residency_config.update(mode="ssh-remote", ssh_host="example-host",
                            remote_sidecar_port=8770, local_tunnel_port=18770)
    blocked = client.post("/coding/projects/res1/grounding/memory/sync")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "residency_unsupported_path"
