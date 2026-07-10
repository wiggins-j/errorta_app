from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_agent_context.pack import pack_capsule
from errorta_agent_context.refs import ReferenceResolver
from errorta_agent_context.schema import AgentContextCapsule
from errorta_agent_context.store import AgentContextStore
from errorta_app.routes import agent_context as agent_context_routes


def _capsule(ref_sensitivity: str = "safe_metadata") -> AgentContextCapsule:
    return AgentContextCapsule.from_dict({
        "format": "errorta.agent_context_capsule.v1",
        "capsule_id": "cap_test",
        "kind": "micro",
        "created_at": "2026-06-12T00:00:00Z",
        "task": {
            "title": "Continue F035",
            "status": "implementing",
            "intent": "Ship capsule handoff safely",
        },
        "state": {
            "facts": [{
                "id": "F1",
                "text": "Capsules store refs instead of raw logs.",
                "refs": ["R1"],
                "confidence": "high",
            }],
            "next_actions": [{"id": "N1", "text": "Run focused tests."}],
        },
        "refs": [{
            "id": "R1",
            "uri": "file://README.md",
            "class": "file",
            "sensitivity": ref_sensitivity,
            "fetch_policy": "summary_only",
        }],
    })


def test_schema_store_hash_and_materialize_round_trip(tmp_errorta_home):
    store = AgentContextStore(tmp_errorta_home / ".errorta" / "agent-context")
    capsule = _capsule()
    store.write_capsule(capsule)

    reread = store.materialize("cap_test")
    assert reread.capsule_id == "cap_test"
    assert reread.to_dict()["digest"]["canonical_sha256"] == capsule.canonical_sha256()
    assert store.list_capsules()[0]["canonical_sha256"] == capsule.canonical_sha256()


def test_remote_pack_omits_local_only_refs(tmp_errorta_home):
    capsule = _capsule(ref_sensitivity="local_only")
    packed = pack_capsule(
        capsule,
        resolver=ReferenceResolver(
            repo_root=tmp_errorta_home,
            errorta_home=tmp_errorta_home / ".errorta",
        ),
        destination_scope="remote_provider",
    )
    assert "Capsules store refs" in packed.text
    assert packed.included_refs == []
    assert packed.omitted_refs[0]["reason"] == "policy_block"


def test_token_budget_drop_removes_ref_from_included_refs(tmp_errorta_home):
    capsule = _capsule()
    packed = pack_capsule(
        capsule,
        resolver=ReferenceResolver(
            repo_root=tmp_errorta_home,
            errorta_home=tmp_errorta_home / ".errorta",
        ),
        max_tokens=1,
        include_ref_summaries=False,
    )
    assert packed.included_refs == []
    assert packed.omitted_refs[-1]["reason"] == "token_budget"


def test_agent_context_routes_create_pack_and_validate(tmp_errorta_home):
    app = FastAPI()
    app.include_router(agent_context_routes.router)
    client = TestClient(app)

    created = client.post(
        "/agent-context/capsules",
        json={"capsule": _capsule().to_dict()},
    )
    assert created.status_code == 200
    assert created.json()["canonical_sha256"]

    listed = client.get("/agent-context/capsules")
    assert listed.status_code == 200
    assert listed.json()["capsules"][0]["capsule_id"] == "cap_test"

    packed = client.post(
        "/agent-context/pack",
        json={"capsule_id": "cap_test", "include_ref_summaries": False},
    )
    assert packed.status_code == 200
    assert "capsule_id: cap_test" in packed.json()["text"]

    validation = client.post("/agent-context/validate", json=_capsule().to_dict())
    assert validation.status_code == 200
    assert validation.json()["ok"] is True
