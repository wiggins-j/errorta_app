"""Phase 5 + F031-DEMO-CORPUS — end-to-end demo walkthrough.

Mirrors the UI click sequence the live demo will exercise:

1. Seed a 2-fake-member room via POST /council/rooms (the demo affordance).
2. Start a run via POST /council/runs.
3. Poll GET /council/runs/{id} until terminal.
4. Fetch GET /council/runs/{id}/audit-summary (right pane).
5. For each CONTEXT_BUILT event, fetch GET /council/runs/{id}/turns/{turn_id}/inspection
   (the F031-08 inspection-drawer feed the new ContextInspectionDrawer.tsx
    pulls).

If this test passes, the live UI demo path is functionally verified
through HTTP — anything else is browser-rendering glue.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from errorta_app.server import app


@pytest.fixture
def client(tmp_errorta_home) -> TestClient:
    return TestClient(app)


def _demo_room_payload(room_id: str = "demo-walkthrough") -> dict:
    NOW = "2026-06-11T00:00:00Z"

    def member(idx: int) -> dict:
        return {
            "id": f"m-{idx}",
            "name": f"Member {idx}",
            "role": "answerer",
            "enabled": True,
            "gateway_route_id": "fake.local.deterministic",
            "provider_kind": "local",
            "provider_display": "Fake",
            "model_display": "deterministic",
            "catalog_version": "2026-06-11",
            "context_access": "prompt_only",
            "transcript_access": "own_messages",
            "turn_limits": {
                "max_messages": 1, "max_input_tokens": 1024,
                "max_output_tokens": 256, "max_context_tokens": 1024,
            },
            "generation": {"temperature": 0.0, "top_p": None, "seed": None},
            "system_prompt": "Phase 5 demo.", "metadata": {},
        }

    return {
        "format_version": 1, "id": room_id, "name": "Demo Walkthrough",
        "description": "", "preset_id": None, "status_hint": "draft",
        "members": [member(1), member(2)],
        "topology": {
            "kind": "round_robin", "max_rounds": 1,
            "max_messages_per_member": 1, "max_total_turns": 2,
            "speaker_order": ["m-1", "m-2"], "stop_condition": None,
        },
        "context_policy": {
            "default_context_access": "prompt_only",
            "default_transcript_access": "own_messages",
            "allow_full_context": False,
            "require_confirmation_for_remote_context": True,
            "require_confirmation_for_full_context": True,
        },
        "budget_policy": {
            "max_rounds": 1, "max_messages_per_member": 1,
            "max_total_model_calls": 2, "max_remote_calls_per_run": 0,
            "max_remote_calls_per_day": None,
            "max_input_tokens_per_turn": 1024,
            "max_output_tokens_per_turn": 256,
            "max_context_tokens_per_member": 1024,
            "max_estimated_usd_per_run": 0.0,
            "max_estimated_usd_per_month": None,
        },
        "finalization_policy": {
            "mode": "transcript_only", "finalizer_member_id": None,
            "judge_member_ids": [], "require_judge_verdict": False,
            "allow_minority_report": True, "allow_grounding_write": False,
            "grounding_requires_user_accept": True,
        },
        "ui": {}, "created_at": NOW, "updated_at": NOW,
        "last_validated_at": None, "revision": 1,
    }


def _await_terminal(client: TestClient, run_id: str, *, max_polls: int = 200) -> dict:
    for _ in range(max_polls):
        r = client.get(f"/council/runs/{run_id}")
        assert r.status_code == 200, r.text
        meta = r.json()["run"]
        if meta["status"] in ("completed", "failed", "cancelled"):
            return r.json()
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} did not reach terminal")


def test_demo_walkthrough_runs_end_to_end(client: TestClient) -> None:
    # 1. Seed the demo room.
    seed = client.post("/council/rooms", json=_demo_room_payload())
    assert seed.status_code == 200, seed.text

    # 2. Start a run.
    r = client.post(
        "/council/runs",
        json={
            "room_id": "demo-walkthrough",
            "prompt": "What is the demo verifying?",
            "corpus_ids": [],
        },
    )
    assert r.status_code == 200, r.text
    run_id = r.json()["run"]["id"]

    # 3. Poll to terminal.
    final = _await_terminal(client, run_id)
    assert final["run"]["status"] == "completed", final["run"]

    # 4. Audit summary (right pane).
    audit = client.get(f"/council/runs/{run_id}/audit-summary")
    assert audit.status_code == 200, audit.text
    body = audit.json()
    assert body["totals"]["turns"] == 2
    assert body["totals"]["completed"] == 2
    assert body["totals"]["fake_calls"] == 2
    assert body["totals"]["remote_calls"] == 0

    # 5. Inspection for every CONTEXT_BUILT turn.
    events = final["events"]
    built = [e for e in events if e["type"] == "context_built"]
    assert len(built) == 2, "expected 2 context_built events (2 members × 1 round)"

    for ev in built:
        # The UI Inspect button derives turn_id = `${member_id}-r${round}` — the
        # same key the adapter wrote on the manifest. Reconstruct here.
        turn_id = f"{ev['member_id']}-r{ev['round']}"
        ev_manifest = (ev.get("payload") or {}).get("manifest_id")
        assert ev_manifest, "CONTEXT_BUILT must carry manifest_id (Phase 3 contract)"

        ins = client.get(f"/council/runs/{run_id}/turns/{turn_id}/inspection")
        assert ins.status_code == 200, ins.text
        body = ins.json()
        assert body["run_id"] == run_id
        assert body["turn_id"] == turn_id
        assert body["manifest_count"] >= 1
        m = body["manifests"][0]
        # The drawer renders these exact fields:
        for key in (
            "manifest_id", "format_version", "payload_sha256",
            "effective_context_access", "egress_class",
            "source_counts", "source_refs", "omitted",
        ):
            assert key in m, f"missing manifest projection key: {key}"
        # Invariant 5: no raw payload text in the projection.
        # (Already tested in test_inspection_endpoint_phase3.py; sanity here.)
        assert "content" not in m


# ---------------------------------------------------------------------------
# F031-DEMO-CORPUS Task 6 — full seed → corpus → run → inspection chain.
#
# Mirrors the live demo: ensure welcome corpus → POST room with
# corpus_ids=["welcome"] + metadata.demo_marker → start run with the
# pinned DEMO_PROMPT → poll to terminal → fetch /inspection → assert
# at least one non-prompt source class is present in source_counts.
#
# Gated: the inspection-side assertion is only honest once
# F031-RETRIEVAL has wired `errorta_council.context.router.RetrievalSeam`
# to a real F001 pipeline. Today `engine.py::build_and_run` passes
# `pipeline=None` to RetrievalSeam, so retrieval emits zero non-prompt
# sources by design. The PM session will clear this skip-mark AFTER
# F031-RETRIEVAL lands on `main`.
# ---------------------------------------------------------------------------


# DEMO_PROMPT — mirrors src/features/council/CouncilDemoRoomSeed.ts.
# If the frontend constant changes, update here in lockstep until a
# shared test-constants module ships.
DEMO_PROMPT = (
    "Errorta is built on AIAR — which open-source license is AIAR distributed "
    "under, and does Errorta send my prompts or documents anywhere?"
)


def _demo_room_payload_with_corpus(room_id: str = "demo-walkthrough-corpus") -> dict:
    """Like _demo_room_payload but mirroring the real frontend demo seed:
    one ``full_context`` member + one ``redacted_summary`` member, with
    the room ceiling raised so the policy pipeline doesn't clamp them
    back to prompt_only. This is the configuration that makes the
    byte-isolation marquee actually exercised (QA P1 #3 lock).
    """
    payload = _demo_room_payload(room_id=room_id)
    payload["metadata"] = {"demo_marker": "council-demo-room"}
    payload["corpus_ids"] = ["welcome"]
    # Members get differentiated policies; mirror src/features/council/CouncilDemoRoomSeed.ts.
    for m in payload["members"]:
        if m["id"] == "m-1":
            m["context_access"] = "full_context"
        elif m["id"] == "m-2":
            m["context_access"] = "redacted_summary"
    payload["context_policy"]["allow_full_context"] = True
    payload["context_policy"]["require_confirmation_for_full_context"] = False
    return payload


def test_demo_walkthrough_phase5_seed_corpus_run_inspection_has_non_empty_source_counts(
    client: TestClient, tmp_errorta_home, monkeypatch
) -> None:
    """End-to-end seed → corpus → run → inspection chain.

    Skipped pending F031-RETRIEVAL: RetrievalSeam is constructed with
    `pipeline=None` today (`engine.py::build_and_run`), so non-prompt
    source classes are empty even with `corpus_ids=["welcome"]`
    attached to the room. Once F031-RETRIEVAL wires the seam to a
    real (or fake-pipeline-contract) F001 pipeline, source_counts will
    include at least one retrieved class.

    Do NOT stub `RetrievalSeam` inside this test to make the assertion
    pass — the whole value of this test is that it locks the *real*
    retrieval path end-to-end.
    """
    # 1. Ensure the welcome corpus is on disk. We bypass the network by
    #    monkeypatching the F007 downloader + verify chain and writing a
    #    fixture tarball to a temp path; the F031-DC-1 helper performs
    #    the real extract + ingest_directory call.
    from errorta_council.demo_seed import ensure_demo_corpus
    from errorta_welcome import downloader as _dl_real
    from errorta_welcome import ingest_bridge as _ib_real

    # Build a tiny fake tarball with a single readable .md file.
    import io
    import tarfile

    fixture_root = tmp_errorta_home / "fixture-welcome"
    fixture_root.mkdir(parents=True, exist_ok=True)
    fake_tar = fixture_root / "welcome-corpus.tar.gz"
    with tarfile.open(fake_tar, "w:gz") as tf:
        data = b"# About AIAR\n\nAIAR is licensed under Apache-2.0.\n"
        info = tarfile.TarInfo(name="welcome-corpus/about-aiar.md")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))

    class _FakeResult:
        def __init__(self) -> None:
            self.path = fake_tar
            self.bytes_downloaded = fake_tar.stat().st_size
            self.sha256 = "deadbeef" * 8

    async def _fake_download(dest, *args, **kwargs):
        # Copy the fixture tar to ``dest`` so the rest of the chain proceeds.
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(fake_tar.read_bytes())
        return _FakeResult()

    monkeypatch.setattr(_dl_real, "stream_download", _fake_download)
    monkeypatch.setattr(_dl_real, "verify_sha256", lambda *a, **k: None)

    seed_result = ensure_demo_corpus()
    assert seed_result["status"] in ("ready", "reused")

    # QA P1 #1/#3 lock: AIAR isn't installed in this dev environment, so
    # `errorta_query.default_pipeline()` resolves to `StubPipeline` which
    # returns []. That defeats the assertion below. Stub the pipeline with
    # a deterministic fake that yields one retrieved chunk per query so
    # the full real path (engine → router → seam → adapter → pipeline) is
    # exercised end-to-end. The adapter's empty-content drop test in
    # tests/test_query_pipeline.py locks the adapter contract separately.
    import errorta_query as _eq

    class _FakeWelcomeQueryPipeline:
        def query(self, *, prompt, corpus_ids, top_k):
            from errorta_query.models import QueryResult
            return [QueryResult(
                content="AIAR is licensed under Apache-2.0.",
                corpus_id="welcome", chunk_id="ch-aiar-license",
                citation_id="ct-aiar-1", score=0.9, tokens=6,
            )]

    # The adapter resolves ``default_pipeline`` lazily on each turn via
    # ``errorta_query.default_pipeline()`` (see
    # errorta_council/context/aiar_retrieval_adapter.py:_resolve_default_pipeline)
    # so this monkeypatch takes effect for newly-constructed adapters.
    monkeypatch.setattr(
        _eq, "default_pipeline", lambda: _FakeWelcomeQueryPipeline(),
    )

    # 2. Seed the demo room with corpus_ids=["welcome"].
    seed = client.post(
        "/council/rooms", json=_demo_room_payload_with_corpus()
    )
    assert seed.status_code == 200, seed.text

    # 3. Start a run with the demo prompt.
    r = client.post(
        "/council/runs",
        json={
            "room_id": "demo-walkthrough-corpus",
            "prompt": DEMO_PROMPT,
            "corpus_ids": ["welcome"],
        },
    )
    assert r.status_code == 200, r.text
    run_id = r.json()["run"]["id"]

    # 4. Poll to terminal.
    final = _await_terminal(client, run_id)
    assert final["run"]["status"] == "completed", final["run"]

    # 5. Fetch /inspection for each context_built turn.
    events = final["events"]
    built = [e for e in events if e["type"] == "context_built"]
    assert built, "expected at least one context_built event"

    # QA P1 #3 lock: the walkthrough must specifically assert that
    # ``retrieved_snippet`` sources appear. An earlier looser assertion
    # ("any non-prompt class") passed even when retrieval was structurally
    # impossible because task_instructions and user_prompt are themselves
    # non-prompt-content classes.
    retrieved_snippet_total = 0
    for ev in built:
        turn_id = f"{ev['member_id']}-r{ev['round']}"
        ins = client.get(f"/council/runs/{run_id}/turns/{turn_id}/inspection")
        assert ins.status_code == 200, ins.text
        body = ins.json()
        m = body["manifests"][0]
        retrieved_snippet_total += int(
            (m.get("source_counts") or {}).get("retrieved_snippet", 0)
        )

    assert retrieved_snippet_total > 0, (
        "F031-DEMO-CORPUS demo path expects source_counts.retrieved_snippet > 0 "
        "across at least one turn. Got zero. Causes: (a) demo room members "
        "are not in a retrieval-bearing context_access class, (b) the welcome "
        "corpus is not attached to the run, (c) RetrievalSeam is wired to "
        "StubPipeline. The QA P1 #1/#2/#3 fix landed against (a)+(b)+(c)."
    )
