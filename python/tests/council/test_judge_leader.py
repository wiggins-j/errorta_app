"""F080 — the neutral leader-judge.

End-to-end through ``build_and_run`` with a scripted gateway. The judge:
  * never takes a deliberation turn (excluded from the speaker order),
  * holds no opinion of its own (its persona is replaced by the neutral
    judge prompt — it only returns a structured verdict),
  * can stop the run early when the members converge, and
  * can break a tie at the round/budget limit.
Also locks that a member's configured persona reaches its model request.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from errorta_council.context.manifest_store import ContextManifestStore
from errorta_council.context.retrieval import RetrievalSeam
from errorta_council.context.router import ContextRouter
from errorta_council.context.transforms.pipeline import TransformPipeline
from errorta_council.context.transforms.redaction import (
    REDACTION_VERSION,
    RedactionPipeline,
)
from errorta_council.context.transforms.store import TransformStore
from errorta_council.context.transforms.summarization import SummaryPipeline
from errorta_council.engine import _build_snapshot_loader, build_and_run
from errorta_council.gateway_local import (
    LocalCouncilModelResult,
    LocalGateway,
)
from errorta_council.limits import SchedulerPolicy
from errorta_council.paths import council_root
from errorta_council.run_store import RunStore
from errorta_council.schema import EventType


PERSONA_SENTINEL = "ZQ_STUBBORN_SKEPTIC_PERSONA_alpha42"


class _ScriptGateway(LocalGateway):
    """Gateway whose judge replies are scripted; members answer generically."""

    def __init__(self, judge_replies: list[str], member_reply: str | None = None) -> None:
        super().__init__()
        self.requests = []
        self._judge_replies = list(judge_replies)
        self._judge_idx = 0
        self._member_reply = member_reply

    async def call(self, request):
        self.requests.append(request)
        if request.metadata.get("judge"):
            reply = (
                self._judge_replies[self._judge_idx]
                if self._judge_idx < len(self._judge_replies)
                else self._judge_replies[-1]
            )
            self._judge_idx += 1
            content = reply
        elif self._member_reply is not None:
            content = self._member_reply
        else:
            content = f"position_from_{request.metadata.get('member_id')}"
        return LocalCouncilModelResult(
            content=content, provider="fake", provider_class="local",
            model=request.model, input_tokens=None, output_tokens=None,
            duration_ms=0, raw_usage_available=False,
        )

    async def is_reachable(self) -> bool:
        return False


class _FakeGatewayMeta:
    async def is_reachable(self) -> bool:
        return True

    async def list_installed_models(self) -> list[str]:
        return ["stub-model"]


def _router(*, run_store, run_meta, gateway) -> ContextRouter:
    root = council_root()
    transforms = TransformPipeline(
        redaction=RedactionPipeline(version=REDACTION_VERSION),
        summary=SummaryPipeline(
            gateway=gateway, route_id="local.summary",
            allow_extractive_fallback=True,
        ),
        store=TransformStore(root=root / "transforms"),
    )
    base_loader = _build_snapshot_loader(run_store=run_store, run_meta=run_meta)

    def loader(run_id):
        snap = base_loader(run_id)
        snap["room"]["transcript_access_ceiling"] = "all_messages"
        snap["topology"]["transcript_access_ceiling"] = "all_messages"
        return snap

    return ContextRouter(
        retrieval=RetrievalSeam(pipeline=None),
        transforms=transforms,
        manifest_store=ContextManifestStore(root=root / "context-manifests"),
        run_snapshot_loader=loader,
    )


def _member(mid, role="member", system_prompt=""):
    return {
        "id": mid, "enabled": True, "role": role,
        "provider": "fake", "model": "stub-model",
        "context_access": "prompt_only", "transcript_access": "all_messages",
        "gateway_route_id": "fake.local.deterministic",
        "system_prompt": system_prompt,
    }


def _snapshot(members, judge_policy=None):
    snap = {
        "id": "rm-judge",
        "context_access_ceiling": "full_context",
        "transcript_access_ceiling": "all_messages",
        "allow_full_context": True,
        "members": members,
    }
    if judge_policy is not None:
        snap["judge_policy"] = judge_policy
    return snap


async def _run(store, meta, gateway, *, max_rounds):
    return await asyncio.wait_for(
        build_and_run(
            run_store=store, run_meta=meta,
            policy=SchedulerPolicy(
                max_rounds=max_rounds, max_messages_per_member=max_rounds,
                per_turn_timeout_seconds=5,
            ),
            gateway_meta=_FakeGatewayMeta(), hardware_scan_present=True,
            gateway=gateway,
            context_router=_router(run_store=store, run_meta=meta, gateway=gateway),
        ),
        timeout=8.0,
    )


def _events(store, run_id):
    _, events = store.read_run(run_id)
    return events


@pytest.mark.asyncio
async def test_judge_stops_early_on_verdict_reached(tmp_errorta_home, runs_dir_path):
    store = RunStore(runs_dir=runs_dir_path)
    members = [_member("m-1"), _member("m-2"), _member("m-judge", role="judge")]
    meta = store.create_run(
        room_id="rm-judge",
        room_snapshot=_snapshot(members, judge_policy={
            "enabled": True, "judge_member_id": "m-judge", "start_round": 1,
        }),
        prompt="best cache eviction policy?", corpus_ids=[],
    )
    reached = json.dumps({
        "verdict": "reached", "answer": "Use an LRU cache.",
        "agreed_member_ids": ["m-1", "m-2"], "dissenting_member_ids": [],
        "reason": "both members converged on LRU",
    })
    gw = _ScriptGateway(judge_replies=[reached])
    final = await _run(store, meta, gw, max_rounds=4)

    assert final.status == "completed"
    assert final.terminal_reason == "verdict_reached"
    events = _events(store, meta.id)
    # The judge NEVER takes a deliberation turn.
    member_msgs = [e for e in events if e.type == EventType.MEMBER_MESSAGE]
    assert all(e.member_id != "m-judge" for e in member_msgs)
    # Stopped after round 1 (2 members spoke once) — rounds 2..4 never ran.
    assert len(member_msgs) == 2
    # A judge verdict was recorded.
    verdicts = [e for e in events if e.type == EventType.JUDGE_VERDICT]
    assert verdicts and verdicts[-1].payload["verdict"] == "reached"
    # The final answer is the judge's neutral synthesis of the members' answer.
    finals = [e for e in events if e.type == EventType.FINAL_ANSWER]
    assert finals and finals[-1].payload["content"] == "Use an LRU cache."
    assert finals[-1].payload.get("synthesis_mode") == "judge"
    assert finals[-1].payload.get("judge", {}).get("verdict") == "reached"


@pytest.mark.asyncio
async def test_judge_breaks_tie_at_limit(tmp_errorta_home, runs_dir_path):
    store = RunStore(runs_dir=runs_dir_path)
    members = [_member("m-1"), _member("m-2"), _member("m-judge", role="judge")]
    meta = store.create_run(
        room_id="rm-judge",
        room_snapshot=_snapshot(members, judge_policy={
            "enabled": True, "judge_member_id": "m-judge", "start_round": 1,
        }),
        prompt="tabs or spaces?", corpus_ids=[],
    )
    keep_going = json.dumps({"verdict": "continue", "reason": "still disputing"})
    decide = json.dumps({
        "verdict": "decide", "answer": "Spaces.",
        "chosen_member_id": "m-1", "reason": "m-1 had the stronger case",
    })
    # Round-1 boundary → continue; final tie-break → decide.
    gw = _ScriptGateway(judge_replies=[keep_going, decide])
    final = await _run(store, meta, gw, max_rounds=2)

    assert final.status == "completed"
    events = _events(store, meta.id)
    member_msgs = [e for e in events if e.type == EventType.MEMBER_MESSAGE]
    # Full deliberation ran (2 members × 2 rounds) since round 1 said continue.
    assert len(member_msgs) == 4
    verdicts = [e for e in events if e.type == EventType.JUDGE_VERDICT]
    assert verdicts[-1].payload["verdict"] == "decide"
    finals = [e for e in events if e.type == EventType.FINAL_ANSWER]
    assert finals[-1].payload["content"] == "Spaces."
    assert finals[-1].payload.get("synthesis_mode") == "judge"


@pytest.mark.asyncio
async def test_no_judge_runs_when_policy_disabled(tmp_errorta_home, runs_dir_path):
    store = RunStore(runs_dir=runs_dir_path)
    members = [_member("m-1"), _member("m-2")]
    meta = store.create_run(
        room_id="rm-judge", room_snapshot=_snapshot(members),
        prompt="x", corpus_ids=[],
    )
    gw = _ScriptGateway(judge_replies=["{}"])
    final = await _run(store, meta, gw, max_rounds=2)
    assert final.status == "completed"
    events = _events(store, meta.id)
    assert not [e for e in events if e.type == EventType.JUDGE_VERDICT]
    assert all(not r.metadata.get("judge") for r in gw.requests)


@pytest.mark.asyncio
async def test_member_persona_reaches_its_request(tmp_errorta_home, runs_dir_path):
    store = RunStore(runs_dir=runs_dir_path)
    members = [
        _member("m-1", system_prompt=PERSONA_SENTINEL),
        _member("m-2"),
    ]
    meta = store.create_run(
        room_id="rm-judge", room_snapshot=_snapshot(members),
        prompt="defend your view", corpus_ids=[],
    )
    gw = _ScriptGateway(judge_replies=["{}"])
    final = await _run(store, meta, gw, max_rounds=1)
    assert final.status == "completed"
    m1_reqs = [r for r in gw.requests if r.metadata.get("member_id") == "m-1"]
    assert m1_reqs, "m-1 never called"
    blob = json.dumps(m1_reqs[0].messages, sort_keys=True)
    assert PERSONA_SENTINEL in blob, "member persona did not reach its request"


# ---- review fixes: P2 #1/#2/#3/#4 -----------------------------------------

def test_judge_role_exempt_from_deliberation_dialect():
    """P2 #1: the judge must be treated like a finalizer so the digest_v1 /
    telegraphic deliberation prompts are NOT appended to its strict-JSON turn."""
    from errorta_council.context.router import _is_finalizer_member
    snap = {"room": {}, "members": [
        {"id": "m-1", "role": "member"},
        {"id": "j", "role": "judge"},
    ]}
    assert _is_finalizer_member(snap, "j") is True
    assert _is_finalizer_member(snap, "m-1") is False


@pytest.mark.asyncio
async def test_credibility_judge_waits_for_credidation_round(
    tmp_errorta_home, runs_dir_path,
):
    """P2 #3: in credibility mode the judge must NOT end the run after round 1
    (claim phase only) — peer credidation happens in round 2."""
    store = RunStore(runs_dir=runs_dir_path)
    members = [_member("m-1"), _member("m-2"), _member("m-judge", role="judge")]
    snap = _snapshot(members, judge_policy={
        "enabled": True, "judge_member_id": "m-judge", "start_round": 1,
    })
    snap["finalization_policy"] = {"mode": "credibility_report",
                                   "finalizer_member_id": None}
    meta = store.create_run(room_id="rm-judge", room_snapshot=snap,
                            prompt="x", corpus_ids=[])
    # The judge always says "reached" — but the credibility floor must keep it
    # from firing until round 2 has run.
    gw = _ScriptGateway(judge_replies=[json.dumps({"verdict": "reached",
                                                   "answer": "stop"})])
    final = await _run(store, meta, gw, max_rounds=3)
    assert final.status == "completed"
    events = _events(store, meta.id)
    member_msgs = [e for e in events if e.type == EventType.MEMBER_MESSAGE]
    # Round 2 (credidation) ran: members spoke in round 2.
    assert any(e.round == 2 for e in member_msgs), "judge stopped before credidation"
    verdicts = [e for e in events if e.type == EventType.JUDGE_VERDICT]
    # The judge's first verdict is at round 2, never round 1.
    assert verdicts and verdicts[0].round == 2


@pytest.mark.asyncio
async def test_judge_tiebreak_skips_when_consensus_reached(tmp_errorta_home, runs_dir_path):
    """P2 #2: the tie-break must NOT run when the run already reached genuine
    consensus (it would burn a call and override the consensus answer). The
    guard returns before touching the gateway — on a bare instance that proves
    it via the absence of an AttributeError from the (unset) gateway."""
    from errorta_council.scheduler import TurnScheduler
    store = RunStore(runs_dir=runs_dir_path)
    members = [_member("m-1"), _member("m-2"), _member("m-judge", role="judge")]
    snap = _snapshot(members, judge_policy={
        "enabled": True, "judge_member_id": "m-judge", "start_round": 1,
    })
    meta = store.create_run(room_id="rm-judge", room_snapshot=snap,
                            prompt="x", corpus_ids=[])
    sched = TurnScheduler.__new__(TurnScheduler)
    sched._meta = meta
    sched._judge_answer = None
    sched._last_judged_round = 0
    # consensus_reached → early return, no gateway call, no answer override.
    await sched._maybe_judge_final("consensus_reached")
    assert sched._judge_answer is None


def test_enabled_member_ids_for_round_excludes_judge(tmp_errorta_home, runs_dir_path):
    """P2 #4: the steward round-completion roster must exclude the judge (it
    never emits a MEMBER_MESSAGE, so it would wedge the steward forever)."""
    from errorta_council.scheduler import TurnScheduler
    store = RunStore(runs_dir=runs_dir_path)
    members = [_member("m-1"), _member("m-2"), _member("m-judge", role="judge")]
    snap = _snapshot(members, judge_policy={
        "enabled": True, "judge_member_id": "m-judge", "start_round": 1,
    })
    meta = store.create_run(room_id="rm-judge", room_snapshot=snap,
                            prompt="x", corpus_ids=[])
    # _enabled_member_ids_for_round + _judge_member_id only read
    # _meta.room_snapshot — construct a bare instance to unit-test the roster.
    sched = TurnScheduler.__new__(TurnScheduler)
    sched._meta = meta
    ids = sched._enabled_member_ids_for_round()
    assert "m-judge" not in ids
    assert set(ids) == {"m-1", "m-2"}
