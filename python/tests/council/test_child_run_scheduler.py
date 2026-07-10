"""F042 scheduler integration for child runs and async inbox."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from errorta_council.children import AsyncInbox, ChildRunStore
from errorta_council.context.manifest_store import ContextManifestStore
from errorta_council.context.router import ContextRouter
from errorta_council.context.transforms.schema import TransformResult
from errorta_council.engine import _build_snapshot_loader, build_and_run
from errorta_council.gateway_local import (
    LocalCouncilModelRequest,
    LocalCouncilModelResult,
    LocalGateway,
)
from errorta_council.limits import SchedulerPolicy
from errorta_council.paths import council_root
from errorta_council.run_store import RunStore
from errorta_council.schema import EventType

CHILD_SENTINEL = "F042_CHILD_SENTINEL raw child output"


class _ScriptedGateway(LocalGateway):
    def __init__(self, *, deny_output: bool = False) -> None:
        super().__init__()
        self.requests: list[LocalCouncilModelRequest] = []
        self.deny_output = deny_output

    async def call(self, request: LocalCouncilModelRequest) -> LocalCouncilModelResult:
        self.requests.append(request)
        member_id = str(request.metadata.get("member_id") or "")
        if member_id == "m-child":
            content = json.dumps({
                "child_task": {
                    "task_kind": "researcher",
                    "title": "Inspect source",
                    "prompt": "Read the source material",
                    "worker_kind": "scripted",
                    "result": CHILD_SENTINEL,
                }
            })
        else:
            content = f"observer from {member_id}"
        return LocalCouncilModelResult(
            content=content,
            provider="fake",
            provider_class="local",
            model=request.model,
            input_tokens=None,
            output_tokens=None,
            duration_ms=1,
            raw_usage_available=False,
        )

    async def is_reachable(self) -> bool:
        return True


class _FakeGatewayMeta:
    async def is_reachable(self) -> bool:
        return True

    async def list_installed_models(self) -> list[str]:
        return ["stub-model"]


class _EmptyRetrieval:
    def fetch(self, *, member_id, prompt, corpus_ids, transcript_cursor, top_k=8):
        return []


class _RedactingTransforms:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def transform(self, request):
        self.calls.append(request)
        content = "Child summary redacted."
        return TransformResult(
            status="allowed",
            artifact_id="sa-child",
            artifact_kind="redacted_summary",
            content=content,
            content_sha256="sha-child-redacted",
            egress_class="local",
            destination_scope=request.destination_scope,
            token_estimate={"input": 4, "output": 3},
            manifest_id="tm-child",
        )


def _room(*, output_policy: str = "allow", second_access: str = "full_context"):
    return {
        "id": "rm-f042",
        "context_access_ceiling": "full_context",
        "transcript_access_ceiling": "all_messages",
        "allow_full_context": True,
        "child_run_policy": {
            "enabled": True,
            "max_children_per_run": 2,
            "creation_policy": "allow",
            "output_policy": output_policy,
        },
        "members": [
            {
                "id": "m-child",
                "enabled": True,
                "role": "member",
                "provider": "fake",
                "model": "stub-model",
                "context_access": "prompt_only",
                "transcript_access": "none",
                "gateway_route_id": "fake.local.deterministic",
            },
            {
                "id": "m-observer",
                "enabled": True,
                "role": "member",
                "provider": "fake",
                "model": "stub-model",
                "context_access": second_access,
                "transcript_access": "none",
                "gateway_route_id": "fake.local.deterministic",
            },
        ],
        "topology": {
            "kind": "round_robin",
            "max_rounds": 1,
            "max_messages_per_member": 1,
            "max_total_turns": 2,
            "speaker_order": ["m-child", "m-observer"],
        },
    }


def _router(*, store: RunStore, meta, transforms) -> ContextRouter:
    return ContextRouter(
        retrieval=_EmptyRetrieval(),
        transforms=transforms,
        manifest_store=ContextManifestStore(root=council_root() / "context-manifests"),
        run_snapshot_loader=_build_snapshot_loader(run_store=store, run_meta=meta),
    )


@pytest.mark.asyncio
async def test_scripted_child_run_completes_and_enters_next_context(
    tmp_errorta_home, runs_dir_path
) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm-f042",
        room_snapshot=_room(),
        prompt="delegate work",
        corpus_ids=[],
    )
    gateway = _ScriptedGateway()

    final = await asyncio.wait_for(
        build_and_run(
            run_store=store,
            run_meta=meta,
            policy=SchedulerPolicy(max_rounds=1, max_messages_per_member=1),
            gateway_meta=_FakeGatewayMeta(),
            hardware_scan_present=True,
            gateway=gateway,
            context_router=_router(store=store, meta=meta, transforms=_RedactingTransforms()),
        ),
        timeout=5.0,
    )

    assert final.status == "completed"
    child_records = ChildRunStore(runs_dir=runs_dir_path).list(meta.id)
    assert len(child_records) == 1
    assert child_records[0].status == "completed"
    [message] = AsyncInbox(runs_dir=runs_dir_path).list(
        meta.id, child_records[0].child_run_id
    )
    assert message.payload_preview == CHILD_SENTINEL

    _, events = store.read_run(meta.id)
    event_types = [e.type for e in events]
    assert EventType.CHILD_RUN_STARTED in event_types
    assert EventType.CHILD_RUN_INBOX_MESSAGE in event_types
    assert EventType.CHILD_RUN_COMPLETED in event_types

    observer_request = next(
        r for r in gateway.requests if r.metadata.get("member_id") == "m-observer"
    )
    request_blob = json.dumps(observer_request.messages, sort_keys=True)
    assert "Child run summary" in request_blob
    assert CHILD_SENTINEL in request_blob


@pytest.mark.asyncio
async def test_child_output_policy_denial_keeps_summary_out_of_parent_context(
    tmp_errorta_home, runs_dir_path
) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm-f042-deny",
        room_snapshot=_room(output_policy="deny"),
        prompt="delegate work",
        corpus_ids=[],
    )
    gateway = _ScriptedGateway()

    final = await asyncio.wait_for(
        build_and_run(
            run_store=store,
            run_meta=meta,
            policy=SchedulerPolicy(max_rounds=1, max_messages_per_member=1),
            gateway_meta=_FakeGatewayMeta(),
            hardware_scan_present=True,
            gateway=gateway,
            context_router=_router(store=store, meta=meta, transforms=_RedactingTransforms()),
        ),
        timeout=5.0,
    )

    assert final.status == "completed"
    child_records = ChildRunStore(runs_dir=runs_dir_path).list(meta.id)
    assert child_records[0].status == "failed"
    assert child_records[0].failure["reason_code"] == "child_output_policy_denied"
    _, events = store.read_run(meta.id)
    assert EventType.CHILD_RUN_COMPLETED not in [e.type for e in events]
    observer_request = next(
        r for r in gateway.requests if r.metadata.get("member_id") == "m-observer"
    )
    assert CHILD_SENTINEL not in json.dumps(observer_request.messages, sort_keys=True)


@pytest.mark.asyncio
async def test_parent_cancel_cancels_outstanding_child_runs(
    tmp_errorta_home, runs_dir_path
) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm-f042-cancel",
        room_snapshot={"members": [], "topology": {"kind": "round_robin"}},
        prompt="cancel",
        corpus_ids=[],
    )
    child_store = ChildRunStore(runs_dir=runs_dir_path)
    child = child_store.create(
        parent_run_id=meta.id,
        member_id="m-1",
        task_kind="tester",
        title="Run tests",
        prompt="pytest",
    )
    child_store.mark_running(child)
    store.merge_meta_fields(meta.id, cancel_requested_at="2026-06-14T00:00:00Z")

    final = await asyncio.wait_for(
        build_and_run(
            run_store=store,
            run_meta=meta,
            policy=SchedulerPolicy(max_rounds=1, max_messages_per_member=1),
            gateway_meta=_FakeGatewayMeta(),
            hardware_scan_present=True,
            gateway=_ScriptedGateway(),
            context_router=_router(store=store, meta=meta, transforms=_RedactingTransforms()),
        ),
        timeout=5.0,
    )

    assert final.status == "cancelled"
    [cancelled] = child_store.list(meta.id)
    assert cancelled.status == "cancelled"
    _, events = store.read_run(meta.id)
    assert EventType.CHILD_RUN_CANCELLED in [e.type for e in events]
