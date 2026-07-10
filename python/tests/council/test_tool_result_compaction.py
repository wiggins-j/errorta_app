"""F044 tool-result compaction coverage."""
from __future__ import annotations

import json

import pytest

from errorta_council.context.manifest_store import ContextManifestStore
from errorta_council.context.router import ContextBuildRequest, ContextRouter
from errorta_council.paths import council_root
from errorta_tools.gateway import ToolCallRequest, ToolCallResult
from errorta_tools.result_store import ToolResultStore


class _NoopRetrieval:
    def fetch(self, **kw):
        return []


class _NoopTransforms:
    async def transform(self, request):  # pragma: no cover - not used here
        raise AssertionError("transform not expected")


OLD_TOOL_BYTES = "OLD_TOOL_BYTES should not reach later model context"
NEW_TOOL_BYTES = "NEW_TOOL_BYTES may remain recent"


def _write_tool_result(*, run_id: str, call_id: str, content: str) -> dict:
    request = ToolCallRequest(
        call_id=call_id,
        run_id=run_id,
        turn_id="turn-1",
        member_id="m-tool",
        tool_id="web_fetch",
        arguments={"url": f"https://example.test/{call_id}"},
    )
    result = ToolCallResult.from_content(
        request=request,
        content=content,
        duration_ms=3,
        egress_class="remote_eligible",
    )
    ToolResultStore(root=council_root() / "tool-results").write(
        run_id=run_id,
        result=result,
    )
    return {
        "id": f"ev-{call_id}",
        "type": "tool_call_completed",
        "sequence": 1 if call_id == "tc-old" else 2,
        "payload": {
            **request.without_raw_arguments(),
            **result.audit_projection(),
            "result_ref": {
                "store": "tool_results_v1",
                "run_id": run_id,
                "call_id": call_id,
            },
        },
    }


def _loader(*, events: list[dict], context_efficiency: dict):
    def load(run_id: str):
        return {
            "run_id": run_id,
            "events": list(events),
            "members": [
                {
                    "member_id": "m-full",
                    "id": "m-full",
                    "role": "member",
                    "provider_class": "fake",
                }
            ],
            "room": {
                "context_access_ceiling": "full_context",
                "transcript_access_ceiling": "all_messages",
                "allow_full_context": True,
                "context_efficiency": context_efficiency,
            },
            "topology": {
                "context_access_ceiling": "full_context",
                "transcript_access_ceiling": "all_messages",
            },
            "residency": {"destination_scope": "local"},
            "corpus_policy": {"max_egress_class": "remote_eligible"},
        }

    return load


def _request(*, force: bool = False) -> ContextBuildRequest:
    return ContextBuildRequest(
        run_id="run-f044",
        turn_id="m-full-r2",
        room_id="room-f044",
        member_id="m-full",
        round=2,
        sequence=3,
        prompt={
            "display_text": "Use available context.",
            "normalized_text": "Use available context.",
            "signature": "sig",
        },
        corpus_ids=[],
        requested_context_access="full_context",
        requested_transcript_access="none",
        destination_scope="local",
        max_input_tokens=8192,
        transcript_cursor=2,
        summary_cursor=0,
        gateway_route_id="fake.local.deterministic",
        metadata={"force_tool_result_compaction": "aggressive"} if force else {},
    )


@pytest.mark.asyncio
async def test_old_tool_result_compacts_to_ref_and_manifest_records_omission(
    tmp_path,
    tmp_errorta_home,
) -> None:
    events = [
        _write_tool_result(run_id="run-f044", call_id="tc-old", content=OLD_TOOL_BYTES),
        _write_tool_result(run_id="run-f044", call_id="tc-new", content=NEW_TOOL_BYTES),
    ]
    router = ContextRouter(
        retrieval=_NoopRetrieval(),
        transforms=_NoopTransforms(),
        manifest_store=ContextManifestStore(root=tmp_path / "manifests"),
        run_snapshot_loader=_loader(
            events=events,
            context_efficiency={
                "tool_result_compaction": {
                    "enabled": True,
                    "recent_results_window": 1,
                    "max_raw_tool_result_tokens": 999,
                }
            },
        ),
    )

    payload = await router.build(_request())

    message_blob = json.dumps(payload.messages, sort_keys=True)
    assert OLD_TOOL_BYTES not in message_blob
    assert NEW_TOOL_BYTES in message_blob
    assert "Tool result ref" in message_blob
    refs = [r for r in payload.source_refs if r.class_ == "tool_result_ref"]
    assert refs and refs[0].tool_call_id == "tc-old"
    assert refs[0].result_ref == {
        "store": "tool_results_v1",
        "run_id": "run-f044",
        "call_id": "tc-old",
    }

    [manifest_path] = list((tmp_path / "manifests").glob("*.json"))
    manifest = json.loads(manifest_path.read_text())
    assert manifest["source_counts"]["tool_result_ref"] == 1
    assert manifest["source_counts"]["tool_result"] == 1
    omitted = [
        o for o in manifest["omitted"]
        if o.get("reason") == "compacted_to_tool_result_ref"
    ]
    assert omitted and omitted[0]["tool_call_id"] == "tc-old"
    assert manifest["compaction"]["tool_results"]["omitted_raw_blocks"] == 1

    stored = ToolResultStore(root=council_root() / "tool-results").read(
        run_id="run-f044",
        call_id="tc-old",
    )
    assert stored["content"] == OLD_TOOL_BYTES


@pytest.mark.asyncio
async def test_forced_aggressive_compaction_refs_every_tool_result(
    tmp_path,
    tmp_errorta_home,
) -> None:
    events = [
        _write_tool_result(run_id="run-f044", call_id="tc-old", content=OLD_TOOL_BYTES),
        _write_tool_result(run_id="run-f044", call_id="tc-new", content=NEW_TOOL_BYTES),
    ]
    router = ContextRouter(
        retrieval=_NoopRetrieval(),
        transforms=_NoopTransforms(),
        manifest_store=ContextManifestStore(root=tmp_path / "manifests"),
        run_snapshot_loader=_loader(events=events, context_efficiency={}),
    )

    payload = await router.build(_request(force=True))

    blob = json.dumps(payload.messages, sort_keys=True)
    assert OLD_TOOL_BYTES not in blob
    assert NEW_TOOL_BYTES not in blob
    assert blob.count("Tool result ref") == 2
    assert {r.class_ for r in payload.source_refs} >= {"tool_result_ref"}


class _RedactingTransforms:
    """Returns a fixed redacted summary; records that it was invoked."""

    def __init__(self) -> None:
        self.called = False

    async def transform(self, request):
        from errorta_council.context.transforms.schema import TransformResult

        self.called = True
        return TransformResult(
            status="allowed",
            artifact_id="sa-redacted",
            artifact_kind="redacted_summary",
            content="REDACTED SUMMARY — no raw tool bytes here.",
            content_sha256="sha-redacted",
            egress_class="local",
            destination_scope=request.destination_scope,
            token_estimate={"input": 4, "output": 3},
            manifest_id="tm-redacted",
            blocked_reason=None,
            message_code=None,
        )


@pytest.mark.asyncio
async def test_redacted_member_never_receives_raw_tool_bytes_even_with_compaction(
    tmp_path, tmp_errorta_home
) -> None:
    """Byte isolation (Invariant 5): a redacted_summary member's context is
    built through the transform pipeline and must never carry raw tool output,
    regardless of the tool-result compaction config."""
    events = [
        _write_tool_result(run_id="run-f044", call_id="tc-old", content=OLD_TOOL_BYTES),
        _write_tool_result(run_id="run-f044", call_id="tc-new", content=NEW_TOOL_BYTES),
    ]

    def load(run_id: str):
        return {
            "run_id": run_id,
            "events": list(events),
            "members": [
                {"member_id": "m-redacted", "id": "m-redacted", "role": "member",
                 "provider_class": "fake"}
            ],
            "room": {
                "context_access_ceiling": "full_context",
                "transcript_access_ceiling": "all_messages",
                "allow_full_context": True,
                "context_efficiency": {
                    "tool_result_compaction": {
                        "enabled": True, "recent_results_window": 1,
                        "max_raw_tool_result_tokens": 999,
                    }
                },
            },
            "topology": {
                "context_access_ceiling": "full_context",
                "transcript_access_ceiling": "all_messages",
            },
            "residency": {"destination_scope": "local"},
            "corpus_policy": {"max_egress_class": "remote_eligible"},
        }

    transforms = _RedactingTransforms()
    router = ContextRouter(
        retrieval=_NoopRetrieval(),
        transforms=transforms,
        manifest_store=ContextManifestStore(root=tmp_path / "manifests"),
        run_snapshot_loader=load,
    )
    req = ContextBuildRequest(
        run_id="run-f044", turn_id="m-redacted-r2", room_id="room-f044",
        member_id="m-redacted", round=2, sequence=3,
        prompt={"display_text": "x", "normalized_text": "x", "signature": "s"},
        corpus_ids=[], requested_context_access="redacted_summary",
        requested_transcript_access="none", destination_scope="local",
        max_input_tokens=8192, transcript_cursor=2, summary_cursor=0,
        gateway_route_id="fake.local.deterministic", metadata={},
    )

    payload = await router.build(req)
    blob = json.dumps(payload.messages, sort_keys=True)
    # Neither the "old" nor the "recent" raw tool bytes reach a redacted member.
    assert OLD_TOOL_BYTES not in blob
    assert NEW_TOOL_BYTES not in blob
    assert transforms.called is True  # went through the redaction pipeline
