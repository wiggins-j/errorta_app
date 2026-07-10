"""Adversarial tests for the F088 foundation hardening (architecture review).

Covers: durable-truth admission enforcement, WIP crowding, AIAR filter
forwarding, include_claims, secret/size caps, stale bootstrap recovery, missing
corpus ids / transaction-safe create, and honest embedding-locality.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_project_grounding.adapter import (
    AiarProjectGroundingAdapter,
    UnsupportedGroundingOperation,
)
from errorta_project_grounding.capabilities import _embedding_is_local
from errorta_project_grounding.memory_store import (
    InvalidMemoryItem,
    MemoryItem,
    MemoryQuery,
    MemorySourceRef,
    ProjectMemoryStore,
)

# --- High: durable-truth admission is enforced ------------------------------


def _durable(store, *, source_type, source_ref, content="x", **kw):
    return store.put(MemoryItem(project_id=store.project_id, authority="durable_truth",
                                source_type=source_type, source_ref=source_ref,
                                content=content, **kw))


def test_raw_prose_cannot_be_admitted_as_durable_truth(tmp_path: Path) -> None:
    store = ProjectMemoryStore("p", root=tmp_path)
    # a non-allowlisted source type is rejected outright
    with pytest.raises(InvalidMemoryItem):
        _durable(store, source_type="dev_note", source_ref=MemorySourceRef(task_id="t"))
    # an allowlisted type with insufficient provenance is rejected
    with pytest.raises(InvalidMemoryItem):
        _durable(store, source_type="pm_decision", source_ref=MemorySourceRef(path="a.py"))
    with pytest.raises(InvalidMemoryItem):
        _durable(store, source_type="code_chunk", source_ref=MemorySourceRef(path="a.py"))  # no commit/head
    with pytest.raises(InvalidMemoryItem):
        _durable(store, source_type="test_evidence", source_ref=MemorySourceRef(task_id="t"))  # no test_run_id


def test_evidence_backed_durable_truth_is_accepted(tmp_path: Path) -> None:
    store = ProjectMemoryStore("p", root=tmp_path)
    _durable(store, source_type="pm_decision", source_ref=MemorySourceRef(task_id="t"))
    _durable(store, source_type="code_chunk",
             source_ref=MemorySourceRef(path="a.py", commit="abc", head="abc"))
    _durable(store, source_type="test_evidence", source_ref=MemorySourceRef(test_run_id="tr1"))
    assert len(store.query(MemoryQuery(authorities=("durable_truth",)))) == 3


def test_admission_api_sets_authority(tmp_path: Path) -> None:
    store = ProjectMemoryStore("p", root=tmp_path)
    d = store.admit_durable(source_type="pm_decision",
                            source_ref=MemorySourceRef(task_id="t"), content="decided")
    assert d.authority == "durable_truth"
    c = store.admit_claim(source_type="dev_turn",
                          source_ref=MemorySourceRef(task_id="t"), content="i think it's postgres")
    assert c.authority == "claim"
    # the claim does NOT leak into default retrieval
    assert "i think it's postgres" not in [m.content for m in store.query()]


# --- High: WIP cannot crowd durable truth out of retrieval ------------------


def test_wip_burst_does_not_evict_durable_before_limit(tmp_path: Path) -> None:
    store = ProjectMemoryStore("p", root=tmp_path)
    for i in range(3):
        _durable(store, source_type="pm_decision",
                 source_ref=MemorySourceRef(task_id=f"t{i}"), content=f"durable {i}")
    # a NEWER, noisier WIP burst
    for i in range(20):
        store.admit_wip(source_type="open_pr",
                        source_ref=MemorySourceRef(pr_id=f"pr{i}"), content=f"wip {i}")
    top = store.query(MemoryQuery(limit=3))
    assert all(m.authority == "durable_truth" for m in top)  # durable survives the limit


# --- Medium: include_claims actually includes ------------------------------


def test_include_claims_rescues_claims(tmp_path: Path) -> None:
    store = ProjectMemoryStore("p", root=tmp_path)
    store.admit_claim(source_type="dev_turn", source_ref=MemorySourceRef(task_id="t"),
                      content="raw claim")
    assert store.query() == []  # excluded by default
    got = store.query(MemoryQuery(include_claims=True))
    assert [m.content for m in got] == ["raw claim"]


# --- Medium: secrets and oversized content are never indexed ---------------


def test_secret_content_is_rejected(tmp_path: Path) -> None:
    store = ProjectMemoryStore("p", root=tmp_path)
    with pytest.raises(InvalidMemoryItem):
        store.admit_claim(source_type="dev_turn", source_ref=MemorySourceRef(task_id="t"),
                          content="here is the key sk-ant-api03-AAAAAAAAAAAAAAAAAAAA")


def test_oversized_content_is_rejected(tmp_path: Path) -> None:
    store = ProjectMemoryStore("p", root=tmp_path)
    with pytest.raises(InvalidMemoryItem):
        store.admit_claim(source_type="dev_turn", source_ref=MemorySourceRef(task_id="t"),
                          content="x" * 20_000)


# --- High: AIAR filters fail closed when the seam can't forward them --------


def test_retrieve_fails_closed_when_filters_cannot_be_forwarded() -> None:
    from errorta_project_grounding.capabilities import probe_aiar_grounding_capabilities
    adapter = AiarProjectGroundingAdapter(probe_aiar_grounding_capabilities())
    # errorta_query's retrieval seam has no filters parameter, so a filtered
    # query must fail closed rather than silently drop the filter.
    with pytest.raises(UnsupportedGroundingOperation):
        adapter.retrieve(corpus_id="c", query="q", top_k=3, filters={"path": "a.py"})


# --- Medium: bootstrap stale-job recovery + idempotency key -----------------


def test_stale_running_job_is_recovered(tmp_errorta_home: Path) -> None:
    from errorta_council.coding.ledger import LedgerStore
    from errorta_project_grounding import bootstrap as bs

    s = LedgerStore("boot1")
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    stale = bs.BootstrapJob(job_id="boot_old", project_id="boot1", corpus_id="c",
                            source_root="/tmp/x", status="running",
                            started_at="2000-01-01T00:00:00+00:00")
    bs.save_job(s, stale)
    assert bs.active_job(s) is None  # already stale -> not counted active
    assert bs.recover_stale_jobs(s) == 1
    assert bs.load_job(s, "boot_old").status == "interrupted"


def test_fresh_running_job_blocks_as_active(tmp_errorta_home: Path) -> None:
    from datetime import datetime, timezone

    from errorta_council.coding.ledger import LedgerStore
    from errorta_project_grounding import bootstrap as bs

    s = LedgerStore("boot2")
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    fresh = bs.BootstrapJob(job_id="boot_now", project_id="boot2", corpus_id="c",
                            source_root="/tmp/x", status="running",
                            started_at=datetime.now(timezone.utc).isoformat())
    bs.save_job(s, fresh)
    assert bs.active_job(s) is not None  # an in-flight job is the idempotency key


# --- Medium: create-project is transaction-safe with grounding --------------


@pytest.fixture
def client(tmp_errorta_home) -> TestClient:
    from errorta_app.routes import coding as coding_routes
    app = FastAPI()
    app.include_router(coding_routes.router)
    return TestClient(app, headers={"x-errorta-origin": "tauri-ui"})


def test_invalid_grounding_leaves_no_partial_project(client: TestClient) -> None:
    # existing-mode grounding with no corpus_id is invalid -> 422 BEFORE the
    # project is written.
    r = client.post("/coding/projects", json={
        "project_id": "txn1", "north_star": "n", "definition_of_done": "d",
        "target": "new", "grounding": {"mode": "existing"}})
    assert r.status_code == 422
    assert client.get("/coding/projects/txn1").status_code == 404  # no partial project


def test_corpus_listing_fails_closed_under_remote_residency(client: TestClient) -> None:
    from errorta_residency import config as residency_config
    assert client.get("/coding/grounding/corpora").status_code == 200  # local: ok
    residency_config.update(mode="ssh-remote", ssh_host="example-host",
                            remote_sidecar_port=8770, local_tunnel_port=18770)
    assert client.get("/coding/grounding/corpora").status_code == 409


# --- Medium: honest embedding-locality --------------------------------------


def test_embedding_locality_reflects_residency(tmp_errorta_home: Path) -> None:
    from errorta_residency import config as residency_config
    local, _note = _embedding_is_local()
    assert local is True
    residency_config.update(mode="ssh-remote", ssh_host="example-host",
                            remote_sidecar_port=8770, local_tunnel_port=18770)
    remote_local, note = _embedding_is_local()
    assert remote_local is False
    assert "ssh-remote" in note
