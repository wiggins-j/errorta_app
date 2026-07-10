"""Council room configuration matrix — drives build_and_run end-to-end across
every room setting and validates the resulting behavior with scripted models.

This is the deterministic counterpart to clicking through the Room Editor: for
each configuration (topology, finalization, context-efficiency, budget) it runs
the real engine with a ScriptedGateway that returns chosen content per member
per round, then asserts the outcome — number of turns, the FINAL_ANSWER, the
terminal reason (consensus vs limits), output-token caps, and byte isolation.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import pytest

from errorta_council.context.manifest_store import ContextManifestStore
from errorta_council.context.retrieval import RetrievalSeam
from errorta_council.context.router import ContextRouter
from errorta_council.context.transforms.schema import SourceEnvelope, TransformResult
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


# ── Scripted gateway ────────────────────────────────────────────────────────

class ScriptedGateway(LocalGateway):
    """Returns chosen content per member per call; records every request.

    scripts maps member_id -> list of contents (one per call; the last entry is
    reused if the member is called more times than scripted).
    """

    def __init__(self, scripts: dict[str, list[str]]) -> None:
        super().__init__()
        self._scripts = scripts
        self.requests: list[LocalCouncilModelRequest] = []
        self._counts: dict[str, int] = {}

    async def call(self, request: LocalCouncilModelRequest) -> LocalCouncilModelResult:
        self.requests.append(request)
        mid = request.metadata.get("member_id", "")
        i = self._counts.get(mid, 0)
        self._counts[mid] = i + 1
        seq = self._scripts.get(mid, ["ok"])
        content = seq[min(i, len(seq) - 1)]
        return LocalCouncilModelResult(
            content=content, provider="fake", provider_class="local",
            model=request.model, input_tokens=None, output_tokens=None,
            duration_ms=0, raw_usage_available=False,
        )

    async def is_reachable(self) -> bool:
        return False

    async def list_installed_models(self) -> list[str]:
        return ["stub-model"]

    # convenience
    def requests_for(self, member_id: str) -> list[LocalCouncilModelRequest]:
        return [r for r in self.requests if r.metadata.get("member_id") == member_id]


class _FakeMeta:
    async def is_reachable(self) -> bool:
        return True

    async def list_installed_models(self) -> list[str]:
        # Includes the model names the matrix references so the resource guard's
        # installed-model check admits them.
        return ["stub-model", "qwen3.5:9b", "gemma3:27b", "mistral-small3.1:latest"]


def _digest(position: str, delta: Any) -> str:
    return json.dumps({
        "v": "digest_v1", "position": position,
        "claims": [], "agree": [], "dispute": [],
        "delta": delta, "open": [], "answer_fragment": position,
    })


def _member(mid: str, *, role: str = "member", context_access: str = "prompt_only",
            transcript_access: str = "all_messages", max_output: int | None = None) -> dict:
    m: dict[str, Any] = {
        "id": mid, "enabled": True, "role": role,
        "provider": "fake", "model": "stub-model",
        "context_access": context_access,
        "transcript_access": transcript_access,
        "gateway_route_id": "fake.local.deterministic",
    }
    if max_output is not None:
        m["turn_limits"] = {"max_output_tokens": max_output}
        m["max_output_tokens"] = max_output
    return m


def _room(room_id: str, members: list[dict], *, topology_kind: str = "round_robin",
          consensus_threshold: int | None = None,
          finalization: dict | None = None,
          efficiency: dict | None = None) -> dict:
    topo: dict[str, Any] = {"kind": topology_kind}
    if consensus_threshold is not None:
        topo["consensus_threshold"] = consensus_threshold
    room: dict[str, Any] = {
        "id": room_id,
        "context_access_ceiling": "full_context",
        "transcript_access_ceiling": "all_messages",
        "allow_full_context": True,
        "members": members,
        "topology": topo,
        "finalization_policy": finalization or {"mode": "transcript_only", "finalizer_member_id": None},
    }
    if efficiency is not None:
        room["context_efficiency"] = efficiency
    return room


async def _run(room: dict, *, prompt: str, runs_dir, gateway: ScriptedGateway,
               max_rounds: int = 1, max_messages_per_member: int = 1,
               max_total: int | None = None, context_router=None):
    store = RunStore(runs_dir=runs_dir)
    meta = store.create_run(
        room_id=room["id"], room_snapshot=room, prompt=prompt, corpus_ids=[],
    )
    policy = SchedulerPolicy(
        max_rounds=max_rounds,
        max_messages_per_member=max_messages_per_member,
        max_total_member_messages=max_total,
        per_turn_timeout_seconds=5,
    )
    final = await asyncio.wait_for(
        build_and_run(
            run_store=store, run_meta=meta, policy=policy,
            gateway_meta=_FakeMeta(), hardware_scan_present=True,
            gateway=gateway, context_router=context_router,
        ),
        timeout=10.0,
    )
    _, events = store.read_run(meta.id)
    return final, events


def _by_type(events, t):
    return [e for e in events if e.type == t]


def _terminal_reason(events) -> str:
    done = _by_type(events, EventType.RUN_COMPLETED) or _by_type(events, EventType.RUN_FAILED)
    return (done[-1].payload or {}).get("reason", "") if done else ""


def _final_answer(events):
    fa = _by_type(events, EventType.FINAL_ANSWER)
    return fa[-1] if fa else None


# ── Topology: round robin ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_round_robin_one_round(tmp_errorta_home, runs_dir_path):
    gw = ScriptedGateway({"m-1": ["Answer one."], "m-2": ["Answer two."]})
    room = _room("rr1", [_member("m-1"), _member("m-2")])
    final, events = await _run(room, prompt="q", runs_dir=runs_dir_path, gateway=gw)
    assert final.status == "completed"
    msgs = _by_type(events, EventType.MEMBER_MESSAGE)
    assert [m.member_id for m in msgs] == ["m-1", "m-2"]
    fa = _final_answer(events)
    assert fa is not None and fa.payload["content"] == "Answer two."  # last speaker
    assert _terminal_reason(events) == "limits_exhausted"


@pytest.mark.asyncio
async def test_round_robin_multi_round(tmp_errorta_home, runs_dir_path):
    gw = ScriptedGateway({
        "m-1": ["r1-a", "r2-a"], "m-2": ["r1-b", "r2-b"],
    })
    room = _room("rr2", [_member("m-1"), _member("m-2")])
    final, events = await _run(
        room, prompt="q", runs_dir=runs_dir_path, gateway=gw,
        max_rounds=2, max_messages_per_member=2,
    )
    assert final.status == "completed"
    msgs = _by_type(events, EventType.MEMBER_MESSAGE)
    assert len(msgs) == 4
    assert _final_answer(events).payload["content"] == "r2-b"


# ── Topology: consensus deliberation ──────────────────────────────────────

@pytest.mark.asyncio
async def test_consensus_reached_when_all_hold(tmp_errorta_home, runs_dir_path):
    # Every member emits a digest with delta=null in round 1 → all "no change".
    gw = ScriptedGateway({
        "m-1": [_digest("Kettle.", None)],
        "m-2": [_digest("Kettle.", None)],
        "m-3": [_digest("Kettle.", None)],
    })
    room = _room("cd1", [_member("m-1"), _member("m-2"), _member("m-3")],
                 topology_kind="consensus_deliberation")
    final, events = await _run(
        room, prompt="best chip?", runs_dir=runs_dir_path, gateway=gw,
        max_rounds=3, max_messages_per_member=3,
    )
    assert final.status == "completed"
    assert _terminal_reason(events) == "consensus_reached"
    assert _final_answer(events) is not None


@pytest.mark.asyncio
async def test_consensus_not_reached_runs_to_limit(tmp_errorta_home, runs_dir_path):
    # Members keep revising (delta="revised") → never converge → hit the cap.
    gw = ScriptedGateway({
        "m-1": [_digest("A.", "revised"), _digest("A2.", "revised")],
        "m-2": [_digest("B.", "revised"), _digest("B2.", "revised")],
    })
    room = _room("cd2", [_member("m-1"), _member("m-2")],
                 topology_kind="consensus_deliberation")
    final, events = await _run(
        room, prompt="best chip?", runs_dir=runs_dir_path, gateway=gw,
        max_rounds=2, max_messages_per_member=2,
    )
    assert final.status == "completed"
    assert _terminal_reason(events) == "limits_exhausted"


# ── Finalization ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_single_finalizer_message_is_final_answer(tmp_errorta_home, runs_dir_path):
    gw = ScriptedGateway({
        "m-1": ["finalizer's verdict"], "m-2": ["a regular member"],
    })
    # m-1 is the finalizer; even though m-2 speaks after it, m-1 wins.
    room = _room(
        "fin1",
        [_member("m-1", role="finalizer"), _member("m-2")],
        finalization={"mode": "single_finalizer", "finalizer_member_id": "m-1"},
    )
    final, events = await _run(room, prompt="q", runs_dir=runs_dir_path, gateway=gw)
    assert final.status == "completed"
    fa = _final_answer(events)
    assert fa.payload["member_id"] == "m-1"
    assert fa.payload["content"] == "finalizer's verdict"


# ── Context efficiency: telegraphic / output caps ─────────────────────────

@pytest.mark.asyncio
async def test_telegraphic_caps_intermediate_output(tmp_errorta_home, runs_dir_path):
    gw = ScriptedGateway({"m-1": ["x"], "m-2": ["y"]})
    room = _room(
        "eff1", [_member("m-1", max_output=4096), _member("m-2", max_output=4096)],
        efficiency={
            "deliberation_style": "telegraphic",
            "intermediate_max_output_tokens": 128,
        },
    )
    final, events = await _run(room, prompt="q", runs_dir=runs_dir_path, gateway=gw)
    assert final.status == "completed"
    # Telegraphic + intermediate cap → each member's request is capped to 128
    # even though its own budget is 4096.
    for req in gw.requests:
        assert req.max_output_tokens == 128, (
            f"expected cap 128, got {req.max_output_tokens} for "
            f"{req.metadata.get('member_id')}"
        )


@pytest.mark.asyncio
async def test_natural_style_uses_member_budget(tmp_errorta_home, runs_dir_path):
    gw = ScriptedGateway({"m-1": ["x"], "m-2": ["y"]})
    room = _room(
        "eff2", [_member("m-1", max_output=4096), _member("m-2", max_output=4096)],
        efficiency={"deliberation_style": "natural"},
    )
    final, events = await _run(room, prompt="q", runs_dir=runs_dir_path, gateway=gw)
    assert final.status == "completed"
    for req in gw.requests:
        assert req.max_output_tokens == 4096


@pytest.mark.asyncio
async def test_default_output_budget_is_2048(tmp_errorta_home, runs_dir_path):
    gw = ScriptedGateway({"m-1": ["x"]})
    room = _room("eff3", [_member("m-1"), _member("m-2")])  # no max_output set
    final, events = await _run(room, prompt="q", runs_dir=runs_dir_path, gateway=gw)
    assert final.status == "completed"
    assert gw.requests[0].max_output_tokens == 2048


@pytest.mark.asyncio
async def test_reasoning_model_gets_higher_default_budget(tmp_errorta_home, runs_dir_path):
    # A reasoning model (qwen3.x) with no explicit budget defaults to 8192 so it
    # doesn't thinking-burn; a non-reasoning peer stays at 2048. An explicit
    # per-member budget always wins.
    def rmember(mid, model, max_output=None):
        m = {
            "id": mid, "enabled": True, "role": "member", "provider": "local",
            "model": model, "gateway_route_id": f"local.ollama.{model}",
            "context_access": "prompt_only", "transcript_access": "all_messages",
        }
        if max_output is not None:
            m["turn_limits"] = {"max_output_tokens": max_output}
            m["max_output_tokens"] = max_output
        return m

    gw = ScriptedGateway({"reasoner": ["x"], "plain": ["y"], "explicit": ["z"]})
    room = _room("eff5", [
        rmember("reasoner", "qwen3.5:9b"),
        rmember("plain", "gemma3:27b"),
        rmember("explicit", "qwen3.5:9b", max_output=512),
    ])
    final, events = await _run(
        room, prompt="q", runs_dir=runs_dir_path, gateway=gw,
        max_rounds=1, max_messages_per_member=1, max_total=3,
    )
    assert final.status == "completed"
    assert gw.requests_for("reasoner")[0].max_output_tokens == 8192
    assert gw.requests_for("plain")[0].max_output_tokens == 2048
    assert gw.requests_for("explicit")[0].max_output_tokens == 512


def test_reasoning_model_gets_longer_timeout_floor():
    # A reasoning model gets at least the 300s floor even when the policy
    # timeout is short; a non-reasoning model uses the policy value. Verified
    # directly on the scheduler helper (no live model needed).
    from errorta_council.scheduler import TurnScheduler
    from errorta_council.limits import SchedulerPolicy

    sched = TurnScheduler.__new__(TurnScheduler)
    sched._policy = SchedulerPolicy(
        max_rounds=1, max_messages_per_member=1, per_turn_timeout_seconds=120,
    )
    assert sched._per_turn_timeout_for({"model": "qwen3.5:9b"}) == 300
    assert sched._per_turn_timeout_for({"model": "deepseek-r1:8b"}) == 300
    assert sched._per_turn_timeout_for({"model": "gemma3:27b"}) == 120
    # A policy already longer than the floor wins.
    sched._policy = SchedulerPolicy(
        max_rounds=1, max_messages_per_member=1, per_turn_timeout_seconds=600,
    )
    assert sched._per_turn_timeout_for({"model": "qwen3.5:9b"}) == 600


@pytest.mark.asyncio
async def test_consensus_not_reached_when_a_member_thinking_burns(
    tmp_errorta_home, runs_dir_path
):
    # One member holds (digest delta=null), the other only emits a thinking-burn.
    # The burn is NOT agreement, so the run must NOT declare consensus — it runs
    # to the round limit instead.
    from errorta_council.gateway_local import THINKING_TRACE_MARKER
    gw = ScriptedGateway({
        "m-hold": [_digest("Kettle.", None), _digest("Kettle.", None)],
        "m-burn": [f"{THINKING_TRACE_MARKER} thinking...",
                   f"{THINKING_TRACE_MARKER} still thinking..."],
    })
    room = _room("cd-burn", [_member("m-hold"), _member("m-burn")],
                 topology_kind="consensus_deliberation")
    final, events = await _run(
        room, prompt="best chip?", runs_dir=runs_dir_path, gateway=gw,
        max_rounds=2, max_messages_per_member=2,
    )
    assert final.status == "completed"
    assert _terminal_reason(events) == "limits_exhausted"


# ── Context efficiency: digest dialect block + finalizer exemption ─────────

@pytest.mark.asyncio
async def test_digest_dialect_block_present_for_members_not_finalizer(
    tmp_errorta_home, runs_dir_path
):
    gw = ScriptedGateway({"m-1": [_digest("A", None)], "m-fin": ["final"]})
    room = _room(
        "eff4",
        [_member("m-1"), _member("m-fin", role="finalizer")],
        finalization={"mode": "single_finalizer", "finalizer_member_id": "m-fin"},
        efficiency={"deliberation_dialect": "digest_v1"},
    )
    final, events = await _run(room, prompt="q", runs_dir=runs_dir_path, gateway=gw)
    assert final.status == "completed"

    def joined(member_id: str) -> str:
        reqs = gw.requests_for(member_id)
        return json.dumps([r.messages for r in reqs])

    # The digest_v1 instruction reaches a normal member but NOT the finalizer.
    assert "digest_v1" in joined("m-1")
    assert "digest_v1" not in joined("m-fin")


# ── Byte isolation across context_access levels ───────────────────────────

class _SentinelRetrieval:
    SENTINEL = "ZQ_MATRIX_SENTINEL classified=alpha provider_token_sk-xyz"

    def fetch(self, *, member_id, prompt, corpus_ids, transcript_cursor, top_k=8):
        if member_id == "m-full":
            return [SourceEnvelope(
                class_="retrieved_snippet", corpus_id="c", chunk_id="ch1",
                citation_id="ct1", content=self.SENTINEL,
                content_sha256=hashlib.sha256(self.SENTINEL.encode()).hexdigest(),
                tokens=len(self.SENTINEL.split()), sensitivity="may_contain_corpus",
            )]
        return []


class _RedactingTransforms:
    async def transform(self, request):
        summary = "Summary: corpus referenced; details redacted."
        return TransformResult(
            status="allowed", artifact_id="a1", artifact_kind="redacted_summary",
            content=summary, content_sha256=hashlib.sha256(summary.encode()).hexdigest(),
            egress_class="local", destination_scope=request.destination_scope,
            token_estimate={"input": 5, "output": 4}, manifest_id="m1",
            blocked_reason=None, message_code=None, warnings=[],
        )


class _PlainSnippetRetrieval:
    TEXT = "Kettle chips are kettle-cooked for extra crunch."

    def fetch(self, *, member_id, prompt, corpus_ids, transcript_cursor, top_k=8):
        return [SourceEnvelope(
            class_="retrieved_snippet", corpus_id="c", chunk_id="ch1",
            citation_id="ct1", content=self.TEXT,
            content_sha256=hashlib.sha256(self.TEXT.encode()).hexdigest(),
            tokens=len(self.TEXT.split()), sensitivity="public",
        )]


class _PassthroughTransforms:
    async def transform(self, request):  # pragma: no cover - not exercised
        raise AssertionError("no transform expected for full_context")


@pytest.mark.asyncio
async def test_citation_references_prefix_snippet(tmp_errorta_home, runs_dir_path):
    gw = ScriptedGateway({"m-1": ["ans"]})
    room = _room(
        "cit1", [_member("m-1", context_access="full_context"), _member("m-2")],
        efficiency={"citation_references": True},
    )
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(room_id="cit1", room_snapshot=room,
                            prompt="best chip?", corpus_ids=["c"])
    root = council_root()
    router = ContextRouter(
        retrieval=_PlainSnippetRetrieval(),
        transforms=_PassthroughTransforms(),
        manifest_store=ContextManifestStore(root=root / "context-manifests"),
        run_snapshot_loader=_build_snapshot_loader(run_store=store, run_meta=meta),
    )
    final = await asyncio.wait_for(
        build_and_run(
            run_store=store, run_meta=meta,
            policy=SchedulerPolicy(max_rounds=1, max_messages_per_member=1,
                                   per_turn_timeout_seconds=5),
            gateway_meta=_FakeMeta(), hardware_scan_present=True,
            gateway=gw, context_router=router,
        ),
        timeout=10.0,
    )
    assert final.status == "completed"
    full_req = gw.requests_for("m-1")[0]
    blob = json.dumps(full_req.messages)
    # With citation_references on, the snippet is prefixed with a [c:<alias>]
    # marker (registry-assigned, e.g. c1) so the member can cite it by reference.
    assert "[c:" in blob
    assert "kettle-cooked" in blob  # the snippet content is still present


@pytest.mark.asyncio
async def test_compaction_runs_clean_over_multiple_rounds(tmp_errorta_home, runs_dir_path):
    # Smoke: a compaction-enabled room completes across several rounds without
    # error and still produces a final answer.
    gw = ScriptedGateway({
        "m-1": ["a1", "a2", "a3"], "m-2": ["b1", "b2", "b3"],
    })
    room = _room(
        "cmp1", [_member("m-1", transcript_access="all_messages"),
                 _member("m-2", transcript_access="all_messages")],
        efficiency={
            "transcript_compaction": {
                "enabled": True, "full_rounds_window": 1, "segment_size_rounds": 2,
            },
        },
    )
    final, events = await _run(
        room, prompt="q", runs_dir=runs_dir_path, gateway=gw,
        max_rounds=3, max_messages_per_member=3,
    )
    assert final.status == "completed"
    assert _final_answer(events) is not None
    assert len(_by_type(events, EventType.MEMBER_MESSAGE)) == 6


@pytest.mark.asyncio
async def test_byte_isolation_redacted_member(tmp_errorta_home, runs_dir_path):
    gw = ScriptedGateway({"m-full": ["full ans"], "m-redacted": ["red ans"]})
    room = _room(
        "iso1",
        [
            _member("m-full", context_access="full_context"),
            _member("m-redacted", context_access="redacted_summary",
                    transcript_access="none"),
        ],
    )
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(room_id="iso1", room_snapshot=room,
                            prompt="what?", corpus_ids=["c"])
    root = council_root()
    router = ContextRouter(
        retrieval=_SentinelRetrieval(),
        transforms=_RedactingTransforms(),
        manifest_store=ContextManifestStore(root=root / "context-manifests"),
        run_snapshot_loader=_build_snapshot_loader(run_store=store, run_meta=meta),
    )
    final = await asyncio.wait_for(
        build_and_run(
            run_store=store, run_meta=meta,
            policy=SchedulerPolicy(max_rounds=1, max_messages_per_member=1,
                                   per_turn_timeout_seconds=5),
            gateway_meta=_FakeMeta(), hardware_scan_present=True,
            gateway=gw, context_router=router,
        ),
        timeout=10.0,
    )
    assert final.status == "completed"
    sentinel = _SentinelRetrieval.SENTINEL.encode()
    full_req = gw.requests_for("m-full")[0]
    red_req = gw.requests_for("m-redacted")[0]
    full_bytes = json.dumps(full_req.messages).encode()
    red_bytes = json.dumps(red_req.messages).encode()
    assert sentinel in full_bytes, "fixture sanity: full member must see the sentinel"
    assert sentinel not in red_bytes, "Invariant 5: redacted member must not"
