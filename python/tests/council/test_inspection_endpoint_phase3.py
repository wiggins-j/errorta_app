"""Phase 3 Task 12b — positive inspection-endpoint coverage.

After a real engine-backed run, /runs/{run_id}/turns/{turn_id}/inspection
returns the ContextManifest(s) the router wrote. The endpoint is the
F031-08 inspection-drawer feed; the UI surfaces effective access,
egress class, source counts/refs, and omitted-block reasons from it.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from errorta_app.server import app
from errorta_council.engine import build_and_run
from errorta_council.limits import SchedulerPolicy
from errorta_council.run_store import RunStore
from errorta_council.schema import EventType


class _FakeGatewayMeta:
    async def is_reachable(self) -> bool:
        return True
    async def list_installed_models(self) -> list[str]:
        return ["stub-model"]


@pytest.fixture
def client(tmp_errorta_home) -> TestClient:
    return TestClient(app)


def _seed_and_run(store: RunStore) -> tuple[str, list[str]]:
    meta = store.create_run(
        room_id="rm-i3",
        room_snapshot={
            "id": "rm-i3",
            "members": [
                {"id": "m1", "enabled": True, "role": "member",
                 "provider": "fake", "model": "stub-model"},
                {"id": "m2", "enabled": True, "role": "member",
                 "provider": "fake", "model": "stub-model"},
            ],
        },
        prompt="hello council",
        corpus_ids=[],
    )
    asyncio.run(
        asyncio.wait_for(
            build_and_run(
                run_store=store,
                run_meta=meta,
                policy=SchedulerPolicy(
                    max_rounds=1, max_messages_per_member=1,
                    per_turn_timeout_seconds=5,
                ),
                gateway_meta=_FakeGatewayMeta(),
                hardware_scan_present=True,
            ),
            timeout=5.0,
        )
    )
    _, events = store.read_run(meta.id)
    built = [e for e in events if e.type == EventType.CONTEXT_BUILT]
    turn_ids = [e.id for e in built]
    return meta.id, turn_ids


def test_inspection_returns_manifest_for_real_turn(
    client: TestClient, runs_dir_path,
) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    run_id, _turn_event_ids = _seed_and_run(store)

    # The router keys manifests by the adapter's synthesized turn_id
    # ("<member_id>-r<round>"). The /inspection endpoint reads from
    # ContextManifestStore.list_by_run() filtered to turn_id, so to
    # assert a positive match we look up the manifest_id stamped on
    # the CONTEXT_BUILT event payload, then probe that turn_id.
    _, events = store.read_run(run_id)
    built = [e for e in events if e.type == EventType.CONTEXT_BUILT]
    assert built, "engine should emit CONTEXT_BUILT events"

    from errorta_council.context.manifest_store import ContextManifestStore
    from errorta_council.paths import council_root

    store_manifests = ContextManifestStore(
        root=council_root() / "context-manifests"
    )
    all_for_run = store_manifests.list_by_run(run_id)
    assert all_for_run, "router should have written ≥1 manifest"
    sample_turn_id = all_for_run[0]["turn_id"]

    r = client.get(f"/council/runs/{run_id}/turns/{sample_turn_id}/inspection")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run_id"] == run_id
    assert body["turn_id"] == sample_turn_id
    assert body["manifest_count"] >= 1
    assert body["manifests"], "manifest list must be non-empty"
    m = body["manifests"][0]
    # The manifest projection must surface enough for the UI drawer:
    for key in (
        "manifest_id", "format_version", "payload_sha256",
        "effective_context_access", "effective_transcript_access",
        "destination_scope", "egress_class",
        "source_counts", "source_refs", "omitted",
    ):
        assert key in m, f"missing manifest projection key: {key}"


def test_inspection_404_on_unknown_turn(
    client: TestClient, runs_dir_path,
) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    run_id, _ = _seed_and_run(store)
    r = client.get(f"/council/runs/{run_id}/turns/no-such-turn/inspection")
    assert r.status_code == 404
    assert r.json()["detail"] == "turn_manifest_not_found"


def test_inspection_payload_carries_only_hashes_no_raw_text(
    client: TestClient, runs_dir_path,
) -> None:
    """Invariant 5 backstop: the inspection response must not expose raw
    payload text — only sha256s and counts.
    """
    store = RunStore(runs_dir=runs_dir_path)
    prompt = "ZQ_INSPECTION_RAW_TEXT_DO_NOT_LEAK_v1"
    meta = store.create_run(
        room_id="rm-i4",
        room_snapshot={
            "id": "rm-i4",
            "members": [
                {"id": "m1", "enabled": True, "role": "member",
                 "provider": "fake", "model": "stub-model"},
                {"id": "m2", "enabled": True, "role": "member",
                 "provider": "fake", "model": "stub-model"},
            ],
        },
        prompt=prompt,
        corpus_ids=[],
    )
    asyncio.run(
        asyncio.wait_for(
            build_and_run(
                run_store=store, run_meta=meta,
                policy=SchedulerPolicy(
                    max_rounds=1, max_messages_per_member=1,
                    per_turn_timeout_seconds=5,
                ),
                gateway_meta=_FakeGatewayMeta(), hardware_scan_present=True,
            ),
            timeout=5.0,
        )
    )
    from errorta_council.context.manifest_store import ContextManifestStore
    from errorta_council.paths import council_root

    all_for_run = ContextManifestStore(
        root=council_root() / "context-manifests"
    ).list_by_run(meta.id)
    turn_id = all_for_run[0]["turn_id"]
    r = client.get(
        f"/council/runs/{meta.id}/turns/{turn_id}/inspection"
    )
    assert r.status_code == 200
    body_bytes = json.dumps(r.json()).encode("utf-8")
    assert prompt.encode() not in body_bytes, (
        "Invariant 5 violation: inspection endpoint must not surface raw "
        "prompt text — only sha256s and counts"
    )


# ---------------------------------------------------------------------------
# QA P1 #1 — round-level inspection endpoint.
# ---------------------------------------------------------------------------


def test_round_inspection_returns_all_member_manifests_for_round(
    client: TestClient, runs_dir_path,
) -> None:
    """QA P1 #1 lock: the new /rounds/{N}/inspection route returns ALL
    manifests for the round, regardless of member_id. This is the route
    the UI uses so the compare view (manifests.length >= 2) is reachable
    on a normal Inspect click.
    """
    store = RunStore(runs_dir=runs_dir_path)
    run_id, _ = _seed_and_run(store)

    # Both round-1 members should appear.
    r = client.get(f"/council/runs/{run_id}/rounds/1/inspection")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run_id"] == run_id
    assert body["round"] == 1
    assert body["manifest_count"] >= 2, (
        f"expected >=2 manifests for a 2-member round-robin run; got "
        f"{body['manifest_count']}"
    )
    member_ids = sorted({m["member_id"] for m in body["manifests"]})
    assert member_ids == ["m1", "m2"]


def test_round_inspection_404_on_unknown_run(
    client: TestClient, runs_dir_path,
) -> None:
    r = client.get("/council/runs/does_not_exist/rounds/1/inspection")
    assert r.status_code == 404
    assert r.json()["detail"] == "run_not_found"


def test_round_inspection_404_on_unknown_round(
    client: TestClient, runs_dir_path,
) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    run_id, _ = _seed_and_run(store)
    r = client.get(f"/council/runs/{run_id}/rounds/99/inspection")
    assert r.status_code == 404
    assert r.json()["detail"] == "round_manifests_not_found"
