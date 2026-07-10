"""F078 — live end-to-end: build_and_run drives a Credibility run with a scripted
model gateway + fake ToolGateway, and the finalizer emits a verified report.

Per-member turn script (the scheduler runs one message per turn):
  turn 1 (round 1): web_fetch tool call   → CREDIBILITY_SOURCE_CAPTURED
  turn 2 (round 2): claim packet JSON      → cites the fetched URL
  turn 3 (round 3): peer review JSON        → verifies the OTHER member's claim

Proves the marquee guarantee live: a claim backed by a fetched source + a
non-author verified review is admitted; a claim citing an unfetched URL is not.
"""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict

import pytest

from errorta_council.engine import build_and_run
from errorta_council.gateway_local import (
    LocalCouncilModelRequest,
    LocalCouncilModelResult,
    LocalGateway,
)
from errorta_council.limits import SchedulerPolicy
from errorta_council.run_store import RunStore
from errorta_council.schema import EventType
from errorta_tools.gateway import ToolCallRequest, ToolCallResult


class _FakeGatewayMeta:
    async def is_reachable(self) -> bool:
        return True

    async def list_installed_models(self) -> list[str]:
        return ["stub-model"]


def _other(mid: str) -> str:
    return "m2" if mid == "m1" else "m1"


class _CredibilityGateway(LocalGateway):
    """Scripts each member's three turns: fetch → claim → review."""

    def __init__(self, *, miscite: bool = False) -> None:
        super().__init__()
        self._calls: dict[str, int] = defaultdict(int)
        self._miscite = miscite

    def _content(self, mid: str, n: int) -> str:
        url = f"https://gov-{mid}.example/doc"
        if n == 0:
            return json.dumps({"tool_call": {
                "tool_id": "web_fetch", "arguments": {"url": url},
                "reason": "fetch a source"}})
        if n == 1:
            # m1 mis-cites an unfetched URL when miscite=True (marquee negative).
            cited = (
                "https://unfetched.example/x"
                if (self._miscite and mid == "m1") else url
            )
            return json.dumps({"answer_fragment": f"{mid} answer",
                               "claims": [{"claim_id": f"c-{mid}", "text": "A fact.",
                                           "kind": "factual", "source_ids": [cited]}]})
        if n == 2:
            # Review the OTHER member's claim by its MEMBER-NAMESPACED id
            # ("{member}:{claim_id}"), which is what the finalizer keys on.
            other = _other(mid)
            return json.dumps({"reviews": [{"claim_id": f"{other}:c-{other}",
                                            "status": "verified",
                                            "support_quality": "direct"}]})
        return f"{mid} done"

    async def call(self, request: LocalCouncilModelRequest) -> LocalCouncilModelResult:
        mid = str(request.metadata.get("member_id") or "")
        n = self._calls[mid]
        self._calls[mid] += 1
        return LocalCouncilModelResult(
            content=self._content(mid, n), provider="fake", provider_class="local",
            model=request.model, input_tokens=None, output_tokens=None,
            duration_ms=1, raw_usage_available=False,
        )

    async def is_reachable(self) -> bool:
        return False


class _EchoToolGateway:
    """Returns the fetched page; provenance.final_url echoes the requested URL
    so the scheduler can capture the source."""

    def __init__(self) -> None:
        self.requests: list[ToolCallRequest] = []

    async def invoke(self, request: ToolCallRequest) -> ToolCallResult:
        self.requests.append(request)
        url = str(request.arguments.get("url") or "")
        return ToolCallResult.from_content(
            request=request, content=f"Fetched body of {url}.", duration_ms=3,
            egress_class="remote", provenance={"final_url": url, "status": 200},
        )


def _room(*, miscite: bool = False) -> dict:
    return {
        "id": "rm-cred",
        "allow_full_context": True,
        "members": [
            {"id": "m1", "enabled": True, "role": "member", "provider": "fake",
             "model": "stub-model", "context_access": "prompt_only",
             "transcript_access": "all_messages", "gateway_route_id": "fake.local.deterministic"},
            {"id": "m2", "enabled": True, "role": "member", "provider": "fake",
             "model": "stub-model", "context_access": "prompt_only",
             "transcript_access": "all_messages", "gateway_route_id": "fake.local.deterministic"},
        ],
        "topology": {"kind": "credibility", "max_rounds": 3,
                     "max_messages_per_member": 3, "max_total_turns": 6,
                     "speaker_order": ["m1", "m2"]},
        "finalization_policy": {"mode": "credibility_report"},
        "tool_policy": {
            "web_search": {"enabled": True},
            "web_fetch": {"enabled": True},
            "budget": {"max_tool_calls_per_run": 4},
            "require_first_use_consent": False,
        },
        "credibility_policy": {"enabled": True, "strictness": "normal",
                               "require_search": False, "require_fetch": True},
    }


async def _run(store: RunStore, *, miscite: bool):
    meta = store.create_run(room_id="rm-cred", room_snapshot=_room(miscite=miscite),
                            prompt="Compare two caches.", corpus_ids=[])
    final = await asyncio.wait_for(
        build_and_run(
            run_store=store, run_meta=meta,
            policy=SchedulerPolicy(max_rounds=3, max_messages_per_member=3,
                                   per_turn_timeout_seconds=5),
            gateway_meta=_FakeGatewayMeta(), hardware_scan_present=True,
            gateway=_CredibilityGateway(miscite=miscite),
            tool_gateway=_EchoToolGateway(),
        ),
        timeout=10.0,
    )
    _, events = store.read_run(meta.id)
    return final, events


def _final_answer(events):
    fa = [e for e in events if e.type == EventType.FINAL_ANSWER]
    return fa[-1] if fa else None


@pytest.mark.asyncio
async def test_credibility_run_admits_verified_claims(tmp_errorta_home, runs_dir_path) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    final, events = await _run(store, miscite=False)

    assert final.status == "completed"
    captured = [e for e in events if e.type == EventType.CREDIBILITY_SOURCE_CAPTURED]
    assert len(captured) == 2  # both members fetched a source
    admitted = [e for e in events if e.type == EventType.CREDIBILITY_CLAIM_ADMITTED]
    assert {e.payload["claim_id"] for e in admitted} == {"m1:c-m1", "m2:c-m2"}

    report_ev = [e for e in events if e.type == EventType.CREDIBILITY_REPORT_CREATED]
    assert report_ev, "expected a credibility_report_created event"
    rep = report_ev[-1].payload
    assert set(rep["claims_used"]) == {"m1:c-m1", "m2:c-m2"}
    assert len(rep["source_map"]) == 2

    fa = _final_answer(events)
    assert fa is not None
    assert fa.payload.get("synthesis_mode") == "credibility"
    assert fa.payload.get("credibility_report", {}).get("confidence") in {"high", "medium"}


@pytest.mark.asyncio
async def test_credibility_run_excludes_unfetched_citation(tmp_errorta_home, runs_dir_path) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    final, events = await _run(store, miscite=True)

    assert final.status == "completed"
    report_ev = [e for e in events if e.type == EventType.CREDIBILITY_REPORT_CREATED]
    assert report_ev
    rep = report_ev[-1].payload
    # m1 cited an unfetched URL → excluded; m2 cited its fetched source → admitted.
    assert "m2:c-m2" in rep["claims_used"]
    assert "m1:c-m1" not in rep["claims_used"]
    excluded_ids = {e["claim_id"] for e in rep["excluded_claims"]}
    assert "m1:c-m1" in excluded_ids
    # The marquee guarantee: the unfetched URL never appears in the source map.
    assert all("unfetched.example" not in s["url"] for s in rep["source_map"])
