"""F038 deterministic Steward Packet artifacts."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from errorta_council.engine import build_and_run
from errorta_council.context.manifest_store import ContextManifestStore
from errorta_council.gateway_local import (
    LocalCouncilModelRequest,
    LocalCouncilModelResult,
    LocalGateway,
)
from errorta_council.limits import SchedulerPolicy
from errorta_council.paths import council_root
from errorta_council.run_store import RunStore
from errorta_council.schema import EventStatus, EventType
from errorta_council.steward.packet import (
    StewardPacketError,
    build_deterministic_packet,
    packet_content_sha256,
    validate_steward_packet,
)
from errorta_council.steward.store import StewardPacketStore


@pytest.fixture
def client(tmp_errorta_home: Path) -> TestClient:
    from errorta_app.server import app
    return TestClient(app)


class ScriptedGateway(LocalGateway):
    def __init__(self, scripts: dict[str, list[str]]) -> None:
        super().__init__()
        self._scripts = scripts
        self._counts: dict[str, int] = {}

    async def call(self, request: LocalCouncilModelRequest) -> LocalCouncilModelResult:
        mid = request.metadata.get("member_id", "")
        i = self._counts.get(mid, 0)
        self._counts[mid] = i + 1
        options = self._scripts.get(mid, ["ok"])
        content = options[min(i, len(options) - 1)]
        return LocalCouncilModelResult(
            content=content,
            provider="fake",
            provider_class="local",
            model=request.model,
            input_tokens=None,
            output_tokens=None,
            duration_ms=0,
            raw_usage_available=False,
        )

    async def is_reachable(self) -> bool:
        return True

    async def list_installed_models(self) -> list[str]:
        return ["stub-model"]


class _FakeMeta:
    async def is_reachable(self) -> bool:
        return True

    async def list_installed_models(self) -> list[str]:
        return ["stub-model"]


def _digest(position: str, *, claim: str, dispute: str = "", open_q: str = "") -> str:
    return json.dumps({
        "v": "digest_v1",
        "position": position,
        "claims": [{"id": "c1", "text": claim, "confidence": "high"}],
        "agree": [],
        "dispute": [{"topic": dispute}] if dispute else [],
        "delta": "revised",
        "open": [open_q] if open_q else [],
        "answer_fragment": position,
    })


def _member(mid: str) -> dict[str, Any]:
    return {
        "id": mid,
        "enabled": True,
        "role": "member",
        "provider": "fake",
        "model": "stub-model",
        "gateway_route_id": "fake.local.deterministic",
        "context_access": "prompt_only",
        "transcript_access": "all_messages",
        "force_deliberation_dialect": "digest_v1",
    }


def _room() -> dict[str, Any]:
    return {
        "id": "steward-room",
        "context_access_ceiling": "full_context",
        "transcript_access_ceiling": "all_messages",
        "allow_full_context": True,
        "members": [_member("m-1"), _member("m-2")],
        "topology": {
            "kind": "round_robin",
            "speaker_order": ["m-1", "m-2"],
        },
        "finalization_policy": {"mode": "transcript_only", "finalizer_member_id": None},
        "context_efficiency": {"deliberation_dialect": "digest_v1"},
        "steward_policy": {
            "enabled": True,
            "assignment": {"mode": "external", "gateway_route_id": "local.summary-model"},
            "cadence": "after_each_round",
            "recent_full_messages": 1,
            "max_packet_tokens": 1200,
        },
    }


@pytest.mark.asyncio
async def test_scheduler_creates_packet_after_each_round(
    tmp_errorta_home,
    runs_dir_path,
) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="steward-room",
        room_snapshot=_room(),
        prompt="Pick a safe implementation plan.",
        corpus_ids=[],
    )
    gateway = ScriptedGateway({
        "m-1": [_digest("Manual callout first.", claim="Manual path is safer.")],
        "m-2": [_digest(
            "Agree, but keep auto later.",
            claim="Auto escalation needs benchmarks.",
            dispute="automatic escalation timing",
            open_q="When should auto escalation become default?",
        )],
    })

    final = await asyncio.wait_for(
        build_and_run(
            run_store=store,
            run_meta=meta,
            policy=SchedulerPolicy(
                max_rounds=1,
                max_messages_per_member=1,
                max_total_member_messages=None,
                per_turn_timeout_seconds=5,
            ),
            gateway_meta=_FakeMeta(),
            hardware_scan_present=True,
            gateway=gateway,
        ),
        timeout=10,
    )

    assert final.status == "completed"
    _, events = store.read_run(meta.id)
    requested = [e for e in events if e.type == EventType.STEWARD_PACKET_REQUESTED]
    created = [e for e in events if e.type == EventType.STEWARD_PACKET_CREATED]
    assert len(requested) == 1
    assert len(created) == 1

    packets = StewardPacketStore(runs_dir=runs_dir_path).list(meta.id)
    assert len(packets) == 1
    packet = packets[0]
    assert packet["packet_id"] == created[0].payload["packet_id"]
    assert packet["coverage"]["to_sequence"] == created[0].payload["coverage"]["to_sequence"]
    assert [p["member_id"] for p in packet["member_positions"]] == ["m-1", "m-2"]
    assert packet["open_disagreements"][0]["topic"] == "automatic escalation timing"
    assert packet["open_questions"][0]["question"] == (
        "When should auto escalation become default?"
    )


def test_packet_hash_is_stable_for_same_inputs(runs_dir_path) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="room",
        room_snapshot={},
        prompt="What matters?",
        corpus_ids=[],
    )
    writer = store.acquire_writer(meta.id)
    try:
        store.append_event(
            meta.id,
            type=EventType.RUN_STARTED,
            status=EventStatus.RUNNING,
            payload={"started_at": "2026-06-13T00:00:00Z"},
            writer=writer,
        )
        store.append_event(
            meta.id,
            type=EventType.MEMBER_MESSAGE,
            status=EventStatus.COMPLETED,
            payload={"content": "The important part is source refs."},
            member_id="m-1",
            round=1,
            writer=writer,
        )
    finally:
        store.release_writer(writer)
    meta, events = store.read_run(meta.id)
    a = build_deterministic_packet(
        run_meta=meta,
        events=events,
        created_at="2026-06-13T00:00:01Z",
    )
    b = build_deterministic_packet(
        run_meta=meta,
        events=events,
        created_at="2026-06-13T00:00:01Z",
    )
    assert a["content_sha256"] == b["content_sha256"]
    assert a["packet_id"] == b["packet_id"]
    assert packet_content_sha256(a) == a["content_sha256"]


def test_packet_validation_rejects_claim_without_source_ref() -> None:
    packet = {
        "format": "errorta.council_steward_packet.v1",
        "coverage": {"source_event_ids": ["evt-1"]},
        "user_goal": {"text": "goal", "source_event_ids": ["evt-1"]},
        "current_consensus": {"text": "consensus", "source_event_ids": ["evt-1"]},
        "member_positions": [
            {"member_id": "m-1", "stance": "unsupported", "confidence": "medium"}
        ],
        "open_disagreements": [],
        "open_questions": [],
        "risk_flags": [],
        "next_best_action": {"text": "continue", "source_event_ids": ["evt-1"]},
        "callout_recommendation": {"recommended": False, "source_event_ids": []},
    }
    with pytest.raises(StewardPacketError):
        validate_steward_packet(packet)


def test_rebuild_writes_new_immutable_packet(runs_dir_path) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="room",
        room_snapshot={},
        prompt="Goal",
        corpus_ids=[],
    )
    writer = store.acquire_writer(meta.id)
    try:
        store.append_event(
            meta.id,
            type=EventType.RUN_STARTED,
            status=EventStatus.RUNNING,
            payload={"started_at": "2026-06-13T00:00:00Z"},
            writer=writer,
        )
        store.append_event(
            meta.id,
            type=EventType.MEMBER_MESSAGE,
            status=EventStatus.COMPLETED,
            payload={"content": "First packet source."},
            member_id="m-1",
            round=1,
            writer=writer,
        )
    finally:
        store.release_writer(writer)
    meta, events = store.read_run(meta.id)
    packets = StewardPacketStore(runs_dir=runs_dir_path)
    first = build_deterministic_packet(
        run_meta=meta,
        events=events,
        created_at="2026-06-13T00:00:01Z",
    )
    second = build_deterministic_packet(
        run_meta=meta,
        events=events,
        created_at="2026-06-13T00:00:02Z",
    )
    packets.write(meta.id, first)
    packets.write(meta.id, second)
    assert first["packet_id"] != second["packet_id"]
    assert {p["packet_id"] for p in packets.list(meta.id)} == {
        first["packet_id"],
        second["packet_id"],
    }


def test_steward_packet_routes_list_read_and_rebuild(
    client: TestClient,
    runs_dir_path,
) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="room",
        room_snapshot={},
        prompt="Route rebuild goal",
        corpus_ids=[],
    )
    writer = store.acquire_writer(meta.id)
    try:
        store.append_event(
            meta.id,
            type=EventType.RUN_STARTED,
            status=EventStatus.RUNNING,
            payload={"started_at": "2026-06-13T00:00:00Z"},
            writer=writer,
        )
        store.append_event(
            meta.id,
            type=EventType.MEMBER_MESSAGE,
            status=EventStatus.COMPLETED,
            payload={"content": "Route packet source."},
            member_id="m-1",
            round=1,
            writer=writer,
        )
    finally:
        store.release_writer(writer)

    rebuilt = client.post(
        f"/council/runs/{meta.id}/steward-packets/rebuild",
        headers={"x-errorta-origin": "tauri-ui"},
    )
    assert rebuilt.status_code == 200, rebuilt.text
    packet_id = rebuilt.json()["packet"]["packet_id"]

    listed = client.get(f"/council/runs/{meta.id}/steward-packets")
    assert listed.status_code == 200
    assert listed.json()["packet_count"] == 1
    assert listed.json()["packets"][0]["packet_id"] == packet_id

    fetched = client.get(f"/council/runs/{meta.id}/steward-packets/{packet_id}")
    assert fetched.status_code == 200
    assert fetched.json()["packet"]["packet_id"] == packet_id


@pytest.mark.asyncio
async def test_round_two_context_uses_packet_instead_of_older_transcript(
    tmp_errorta_home,
    runs_dir_path,
) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="steward-room",
        room_snapshot=_room(),
        prompt="Pick a safe implementation plan.",
        corpus_ids=[],
    )
    gateway = ScriptedGateway({
        "m-1": [
            _digest("Manual callout first.", claim="Manual path is safer."),
            _digest("Still manual first.", claim="Packet context is enough."),
        ],
        "m-2": [
            _digest("Agree, keep auto later.", claim="Auto needs benchmarks."),
            _digest("Agree.", claim="No open concern remains."),
        ],
    })

    final = await asyncio.wait_for(
        build_and_run(
            run_store=store,
            run_meta=meta,
            policy=SchedulerPolicy(
                max_rounds=2,
                max_messages_per_member=2,
                max_total_member_messages=None,
                per_turn_timeout_seconds=5,
            ),
            gateway_meta=_FakeMeta(),
            hardware_scan_present=True,
            gateway=gateway,
        ),
        timeout=10,
    )

    assert final.status == "completed"
    _, events = store.read_run(meta.id)
    used = [e for e in events if e.type == EventType.STEWARD_PACKET_USED]
    assert used

    manifests = ContextManifestStore(
        root=council_root() / "context-manifests"
    ).list_by_run(meta.id)
    m1_r2 = next(m for m in manifests if m["turn_id"] == "m-1-r2")
    assert m1_r2["steward"]["fallback"] is False
    assert m1_r2["steward"]["packet_id"].startswith("sp_")
    assert m1_r2["source_counts"]["steward_packet"] == 1
    assert any(r["class_"] == "steward_packet" for r in m1_r2["source_refs"])
    assert any(
        o["reason"] == "replaced_by_steward_packet"
        for o in m1_r2["omitted"]
    )


@pytest.mark.asyncio
async def test_packet_missing_fallback_emits_failed_event(
    tmp_errorta_home,
    runs_dir_path,
) -> None:
    room = _room()
    room["steward_policy"] = {
        **room["steward_policy"],
        "cadence": "on_demand",
        "recent_full_messages": 0,
    }
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="steward-room",
        room_snapshot=room,
        prompt="Pick a safe implementation plan.",
        corpus_ids=[],
    )
    gateway = ScriptedGateway({
        "m-1": ["First answer.", "Second answer."],
        "m-2": ["First response.", "Second response."],
    })

    final = await asyncio.wait_for(
        build_and_run(
            run_store=store,
            run_meta=meta,
            policy=SchedulerPolicy(
                max_rounds=2,
                max_messages_per_member=2,
                max_total_member_messages=None,
                per_turn_timeout_seconds=5,
            ),
            gateway_meta=_FakeMeta(),
            hardware_scan_present=True,
            gateway=gateway,
        ),
        timeout=10,
    )

    assert final.status == "completed"
    _, events = store.read_run(meta.id)
    failed = [
        e for e in events
        if e.type == EventType.STEWARD_PACKET_FAILED
        and e.payload.get("reason") == "packet_missing"
    ]
    assert failed
    assert failed[0].payload["recipient_member_id"] in {"m-1", "m-2"}
