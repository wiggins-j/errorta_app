"""F100-02 Slice 3 — human controls (force-accept route + comment tag).

Locks D (the "good enough, move on" override) and C (a brainstorm comment
threaded to the PM as a tagged interjection):
* force_accept force-approves the EXACT viewed artifact, records a human-override
  decision, advances the phase, and the scheduler moves to the spec stage;
* the accept route is Tauri-origin guarded, 409 on a stale id, 400 off-mode /
  unconfirmed;
* a tagged interjection records the artifact_id and the next PM redraft prompt
  includes the message.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from errorta_council.coding.governance import GovernanceError, GovernanceStore
from errorta_council.coding.governance_prompts import build_pm_governance_prompt
from errorta_council.coding.governance_scheduler import next_governance_action
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.topology import GovernanceReview


def _store(project_id: str) -> tuple[LedgerStore, GovernanceStore]:
    store = LedgerStore(project_id)
    store.create_project(
        north_star="x", definition_of_done="d", target="new", repo_path=None)
    gov = GovernanceStore.for_ledger(store)
    gov.update_state(mode="strict", phase="reviewing_brainstorm")
    return store, gov


# --- D: force_accept (store) -----------------------------------------------
def test_force_accept_advances_and_records_human_override(tmp_errorta_home: Path) -> None:
    store, gov = _store("f100-02-fa")
    art = gov.append_artifact(kind="brainstorm", title="BS", state="under_review")

    updated = gov.force_accept_artifact(art.artifact_id, by="human")
    assert updated.state == "approved"
    assert gov.latest_artifact("brainstorm").state == "approved"
    assert gov.load_state().phase == "drafting_spec"

    decisions = [d for d in store.list_decisions()
                 if d.get("choice") == "human_artifact_accept"]
    assert decisions
    assert decisions[-1].get("artifact_id") == art.artifact_id
    assert decisions[-1].get("accepted_by") == "human"


def test_force_accept_unsticks_scheduler(tmp_errorta_home: Path) -> None:
    store, gov = _store("f100-02-fa-sched")
    art = gov.append_artifact(kind="brainstorm", title="BS", state="under_review")
    # Before acceptance the scheduler dispatches a brainstorm review.
    action = next_governance_action(store, {"pm": ["m-pm"], "reviewer": ["m-r"]})
    assert isinstance(action, GovernanceReview)

    gov.force_accept_artifact(art.artifact_id, by="human")
    # After acceptance the brainstorm is approved -> scheduler advances to spec.
    action2 = next_governance_action(store, {"pm": ["m-pm"], "reviewer": ["m-r"]})
    assert not (isinstance(action2, GovernanceReview)
                and action2.artifact_id == art.artifact_id)
    assert gov.latest_approved_artifact("brainstorm") is not None


def test_force_accept_rejects_stale_artifact(tmp_errorta_home: Path) -> None:
    store, gov = _store("f100-02-fa-stale")
    old = gov.append_artifact(kind="brainstorm", title="v1", state="under_review")
    gov.append_artifact(kind="brainstorm", title="v2", state="under_review")
    # Accepting the OLD (now-superseded) version must 409.
    try:
        gov.force_accept_artifact(old.artifact_id, by="human")
    except GovernanceError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected GovernanceError for a stale artifact")


# --- D: accept route -------------------------------------------------------
def _client(tauri: bool = True) -> TestClient:
    from errorta_app.server import app
    headers = {"x-errorta-origin": "tauri-ui"} if tauri else {}
    return TestClient(app, headers=headers)


def _seed_project(client: TestClient, pid: str) -> None:
    created = client.post(
        "/coding/projects",
        json={"project_id": pid, "north_star": "n",
              "definition_of_done": "d", "target": "new"})
    assert created.status_code == 200, created.text
    s = client.put(f"/coding/projects/{pid}/governance/settings",
                   json={"mode": "strict"})
    assert s.status_code == 200, s.text


def test_accept_route_force_approves(tmp_errorta_home: Path) -> None:
    client = _client()
    _seed_project(client, "fa-route")
    gov = GovernanceStore.for_ledger(LedgerStore("fa-route"))
    art = gov.append_artifact(kind="brainstorm", title="BS", state="under_review")

    resp = client.post(
        f"/coding/projects/fa-route/governance/artifacts/{art.artifact_id}/accept",
        json={"confirm": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["artifact"]["state"] == "approved"
    assert resp.json()["state"]["phase"] == "drafting_spec"


def test_accept_route_requires_tauri_origin(tmp_errorta_home: Path) -> None:
    client = _client()
    _seed_project(client, "fa-route-origin")
    gov = GovernanceStore.for_ledger(LedgerStore("fa-route-origin"))
    art = gov.append_artifact(kind="brainstorm", title="BS", state="under_review")

    nope = _client(tauri=False)
    resp = nope.post(
        f"/coding/projects/fa-route-origin/governance/artifacts/"
        f"{art.artifact_id}/accept",
        json={"confirm": True})
    assert resp.status_code == 403


def test_accept_route_requires_confirm(tmp_errorta_home: Path) -> None:
    client = _client()
    _seed_project(client, "fa-route-confirm")
    gov = GovernanceStore.for_ledger(LedgerStore("fa-route-confirm"))
    art = gov.append_artifact(kind="brainstorm", title="BS", state="under_review")
    resp = client.post(
        f"/coding/projects/fa-route-confirm/governance/artifacts/"
        f"{art.artifact_id}/accept",
        json={"confirm": False})
    assert resp.status_code == 400


def test_accept_route_409_on_stale(tmp_errorta_home: Path) -> None:
    client = _client()
    _seed_project(client, "fa-route-stale")
    gov = GovernanceStore.for_ledger(LedgerStore("fa-route-stale"))
    old = gov.append_artifact(kind="brainstorm", title="v1", state="under_review")
    gov.append_artifact(kind="brainstorm", title="v2", state="under_review")
    resp = client.post(
        f"/coding/projects/fa-route-stale/governance/artifacts/"
        f"{old.artifact_id}/accept",
        json={"confirm": True})
    assert resp.status_code == 409


def test_accept_route_400_off_mode(tmp_errorta_home: Path) -> None:
    client = _client()
    created = client.post(
        "/coding/projects",
        json={"project_id": "fa-route-off", "north_star": "n",
              "definition_of_done": "d", "target": "new"})
    assert created.status_code == 200
    # mode stays "off" (default) — no settings PUT.
    gov = GovernanceStore.for_ledger(LedgerStore("fa-route-off"))
    art = gov.append_artifact(kind="brainstorm", title="BS", state="under_review")
    resp = client.post(
        f"/coding/projects/fa-route-off/governance/artifacts/"
        f"{art.artifact_id}/accept",
        json={"confirm": True})
    assert resp.status_code == 400


# --- C: comment-as-interjection --------------------------------------------
def test_tagged_interjection_records_artifact_id(tmp_errorta_home: Path) -> None:
    client = _client()
    _seed_project(client, "comment-tag")
    gov = GovernanceStore.for_ledger(LedgerStore("comment-tag"))
    art = gov.append_artifact(kind="brainstorm", title="BS", state="under_review")

    resp = client.post(
        "/coding/projects/comment-tag/interject",
        json={"message": "Stop adding owners — keep it high-level.",
              "artifact_id": art.artifact_id})
    assert resp.status_code == 200, resp.text
    rec = resp.json()["interjection"]
    assert rec["artifact_id"] == art.artifact_id
    assert "high-level" in rec["message"]


def test_tagged_comment_reaches_next_pm_prompt(tmp_errorta_home: Path) -> None:
    store, gov = _store("comment-prompt")
    art = gov.append_artifact(kind="brainstorm", title="BS", state="under_review")
    store.record_interjection(
        "Stop adding owners — keep it high-level and move on.",
        artifact_id=art.artifact_id)

    prompt = build_pm_governance_prompt(
        store=store, governance=gov, phase="brainstorming")
    assert "Stop adding owners" in prompt
    assert "AUTHORITATIVE USER DIRECTION" in prompt
