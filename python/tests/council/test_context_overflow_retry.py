"""F044 context-overflow classification and retry coverage."""
from __future__ import annotations

import asyncio
import json

import pytest

from errorta_council.context.manifest_store import ContextManifestStore
from errorta_council.context.overflow import classify_context_overflow
from errorta_council.context.router import ContextRouter
from errorta_council.engine import _build_snapshot_loader, build_and_run
from errorta_council.gateway_local import (
    LocalCouncilModelRequest,
    LocalCouncilModelResult,
    LocalGateway,
)
from errorta_council.limits import SchedulerPolicy
from errorta_council.paths import council_root
from errorta_council.run_store import RunStore
from errorta_council.schema import EventStatus, EventType
from errorta_tools.gateway import ToolCallRequest, ToolCallResult
from errorta_tools.result_store import ToolResultStore


class _NoopRetrieval:
    def fetch(self, **kw):
        return []


class _NoopTransforms:
    async def transform(self, request):  # pragma: no cover - not used here
        raise AssertionError("transform not expected")


class _GatewayMeta:
    async def is_reachable(self) -> bool:
        return True

    async def list_installed_models(self) -> list[str]:
        return ["stub-model"]


class _OverflowOnceGateway(LocalGateway):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[LocalCouncilModelRequest] = []

    async def call(self, request: LocalCouncilModelRequest) -> LocalCouncilModelResult:
        self.requests.append(request)
        if len(self.requests) == 1:
            raise RuntimeError(
                "OpenAI context_length_exceeded: maximum context length exceeded"
            )
        return LocalCouncilModelResult(
            content="retry success",
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


class _AlwaysOverflowGateway(_OverflowOnceGateway):
    async def call(self, request: LocalCouncilModelRequest) -> LocalCouncilModelResult:
        self.requests.append(request)
        raise RuntimeError("Codex CLI input exceeds context window")


def test_context_overflow_classifier_normalizes_provider_messages() -> None:
    cases = [
        ("Anthropic prompt is too long for this model", "anthropic"),
        ("OpenAI context_length_exceeded maximum context length", "openai"),
        ("Google input token count exceeds model limit", "google"),
        ("claude_cli failed: prompt is too long", "claude_cli"),
        ("codex_cli input exceeds context window", "codex_cli"),
        ("Cursor CLI input exceeds context window", "cursor_cli"),
        ("Ollama num_ctx exceeded maximum context", "local"),
    ]
    for message, provider_hint in cases:
        classified = classify_context_overflow(RuntimeError(message))
        assert classified is not None
        assert classified.reason_code == "context_window_exceeded"
        assert classified.provider_hint == provider_hint
        payload = classified.to_event_payload(retryable=True)
        assert message not in json.dumps(payload)
        assert payload["detail_sha256"]

    assert classify_context_overflow(RuntimeError("provider unavailable")) is None


def _room() -> dict:
    return {
        "id": "room-f044-retry",
        "context_access_ceiling": "full_context",
        "transcript_access_ceiling": "all_messages",
        "allow_full_context": True,
        "members": [
            {
                "id": "m-full",
                "enabled": True,
                "role": "member",
                "provider": "fake",
                "model": "stub-model",
                "context_access": "full_context",
                "transcript_access": "none",
                "gateway_route_id": "fake.local.deterministic",
            }
        ],
        "topology": {
            "kind": "round_robin",
            "max_rounds": 1,
            "max_messages_per_member": 1,
            "max_total_turns": 1,
            "speaker_order": ["m-full"],
        },
    }


def _write_tool_result(run_id: str) -> None:
    request = ToolCallRequest(
        call_id="tc-overflow",
        run_id=run_id,
        turn_id="t-1",
        member_id="m-tool",
        tool_id="web_fetch",
        arguments={"url": "https://example.test/overflow"},
    )
    result = ToolCallResult.from_content(
        request=request,
        content="RAW_OVERFLOW_TOOL_OUTPUT " * 200,
        duration_ms=1,
    )
    ToolResultStore(root=council_root() / "tool-results").write(
        run_id=run_id,
        result=result,
    )


def _seed_run_with_tool_result(runs_dir_path):
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="room-f044-retry",
        room_snapshot=_room(),
        prompt="Answer with tool context.",
        corpus_ids=[],
    )
    _write_tool_result(meta.id)
    writer = store.acquire_writer(meta.id)
    try:
        store.append_event(
            meta.id,
            type=EventType.TOOL_CALL_COMPLETED,
            status=EventStatus.COMPLETED,
            payload={
                "call_id": "tc-overflow",
                "tool_id": "web_fetch",
                "content_sha256": ToolResultStore(
                    root=council_root() / "tool-results"
                ).read(run_id=meta.id, call_id="tc-overflow")["content_sha256"],
                "result_ref": {
                    "store": "tool_results_v1",
                    "run_id": meta.id,
                    "call_id": "tc-overflow",
                },
            },
            writer=writer,
        )
    finally:
        store.release_writer(writer)
    return store, meta


@pytest.mark.asyncio
async def test_scheduler_retries_once_with_aggressive_tool_result_compaction(
    tmp_errorta_home,
    runs_dir_path,
) -> None:
    store, meta = _seed_run_with_tool_result(runs_dir_path)

    router = ContextRouter(
        retrieval=_NoopRetrieval(),
        transforms=_NoopTransforms(),
        manifest_store=ContextManifestStore(root=council_root() / "context-manifests"),
        run_snapshot_loader=_build_snapshot_loader(run_store=store, run_meta=meta),
    )
    gateway = _OverflowOnceGateway()

    final = await asyncio.wait_for(
        build_and_run(
            run_store=store,
            run_meta=meta,
            policy=SchedulerPolicy(
                max_rounds=1,
                max_messages_per_member=1,
                per_turn_timeout_seconds=5,
            ),
            gateway_meta=_GatewayMeta(),
            hardware_scan_present=True,
            gateway=gateway,
            context_router=router,
        ),
        timeout=5,
    )

    assert final.status == "completed"
    assert len(gateway.requests) == 2
    first_blob = json.dumps(gateway.requests[0].messages, sort_keys=True)
    retry_blob = json.dumps(gateway.requests[1].messages, sort_keys=True)
    assert "RAW_OVERFLOW_TOOL_OUTPUT" in first_blob
    assert "RAW_OVERFLOW_TOOL_OUTPUT" not in retry_blob
    assert "Tool result ref" in retry_blob
    assert gateway.requests[1].metadata["retry_reason"] == "context_window_exceeded"

    _, events = store.read_run(meta.id)
    failed = [
        e for e in events
        if e.type == EventType.MEMBER_FAILED
        and e.payload.get("reason") == "context_window_exceeded"
    ]
    assert len(failed) == 1
    assert failed[0].payload["retryable"] is True
    assert "maximum context length" not in json.dumps(failed[0].payload)


@pytest.mark.asyncio
async def test_scheduler_second_overflow_fails_without_raw_provider_text(
    tmp_errorta_home,
    runs_dir_path,
) -> None:
    store, meta = _seed_run_with_tool_result(runs_dir_path)
    router = ContextRouter(
        retrieval=_NoopRetrieval(),
        transforms=_NoopTransforms(),
        manifest_store=ContextManifestStore(root=council_root() / "context-manifests"),
        run_snapshot_loader=_build_snapshot_loader(run_store=store, run_meta=meta),
    )
    gateway = _AlwaysOverflowGateway()

    final = await asyncio.wait_for(
        build_and_run(
            run_store=store,
            run_meta=meta,
            policy=SchedulerPolicy(
                max_rounds=1,
                max_messages_per_member=1,
                per_turn_timeout_seconds=5,
                stop_behavior="stop",
            ),
            gateway_meta=_GatewayMeta(),
            hardware_scan_present=True,
            gateway=gateway,
            context_router=router,
        ),
        timeout=5,
    )

    assert final.status == "failed"
    assert final.terminal_reason == "context_window_exceeded"
    assert len(gateway.requests) == 2
    _, events = store.read_run(meta.id)
    failures = [
        e.payload for e in events
        if e.type == EventType.MEMBER_FAILED
        and e.payload.get("reason") == "context_window_exceeded"
    ]
    assert [p["retryable"] for p in failures] == [True, False]
    assert "Codex CLI input exceeds context window" not in json.dumps(failures)
