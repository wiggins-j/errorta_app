"""F039 slice 7 — build_review topology ordering + sign-off + caps."""
from __future__ import annotations

from errorta_council.limits import ReasonCode, SchedulerPolicy
from errorta_council.state import RunCounters
from errorta_council.topologies.build_review import (
    REVIEW_SIGNED_OFF,
    BuildReviewTopology,
    _is_signoff,
)
from errorta_council.topologies.round_robin import RunCompletion, TurnProposal

MEMBERS = [
    {"id": "prog", "role": "programmer", "enabled": True},
    {"id": "rev1", "role": "member", "enabled": True},
    {"id": "rev2", "role": "member", "enabled": True},
]


def _counters(attempts: dict[str, int]) -> RunCounters:
    return RunCounters(
        completed_messages_by_member=dict(attempts),
        attempts_by_member=dict(attempts),
        total_messages_completed=sum(attempts.values()),
        round_index=min(attempts.values()) if attempts else 0,
    )


def _run(attempts, *, events=None, max_rounds=5, max_iterations=None):
    return {
        "members": MEMBERS,
        "counters": _counters({m["id"]: attempts.get(m["id"], 0) for m in MEMBERS}),
        "policy": SchedulerPolicy(max_rounds=max_rounds, max_messages_per_member=max_rounds),
        "events": events or [],
    }, BuildReviewTopology(max_iterations=max_iterations)


def _msg(member_id, content):
    return {"type": "member_message", "member_id": member_id,
            "payload": {"content": content}}


# --- ordering ---------------------------------------------------------------

def test_programmer_speaks_first():
    run, topo = _run({})
    p = topo.propose_next(run, [])
    assert isinstance(p, TurnProposal) and p.member_id == "prog" and p.round == 1


def test_reviewers_follow_programmer_in_order():
    # programmer done this iteration -> rev1 next.
    run, topo = _run({"prog": 1})
    assert topo.propose_next(run, []).member_id == "rev1"
    run, topo = _run({"prog": 1, "rev1": 1})
    assert topo.propose_next(run, []).member_id == "rev2"


def test_second_iteration_returns_to_programmer():
    # iteration 1 complete, reviewers requested changes -> programmer revises.
    events = [_msg("rev1", "request_changes"), _msg("rev2", "needs work")]
    run, topo = _run({"prog": 1, "rev1": 1, "rev2": 1}, events=events)
    p = topo.propose_next(run, [])
    assert isinstance(p, TurnProposal) and p.member_id == "prog" and p.round == 2


# --- sign-off ---------------------------------------------------------------

def test_signs_off_when_all_reviewers_approve():
    events = [_msg("rev1", "LGTM"), _msg("rev2", '{"review_verdict": "approve"}')]
    run, topo = _run({"prog": 1, "rev1": 1, "rev2": 1}, events=events)
    result = topo.propose_next(run, [])
    assert isinstance(result, RunCompletion) and result.reason == REVIEW_SIGNED_OFF


def test_no_signoff_if_one_reviewer_requests_changes():
    events = [_msg("rev1", "LGTM"), _msg("rev2", "request_changes please")]
    run, topo = _run({"prog": 1, "rev1": 1, "rev2": 1}, events=events)
    result = topo.propose_next(run, [])
    assert isinstance(result, TurnProposal) and result.member_id == "prog"


# --- caps -------------------------------------------------------------------

def test_iteration_cap_stops_the_loop():
    events = [_msg("rev1", "more"), _msg("rev2", "more")]
    run, topo = _run({"prog": 2, "rev1": 2, "rev2": 2}, events=events, max_iterations=2)
    result = topo.propose_next(run, [])
    assert isinstance(result, RunCompletion)
    assert result.reason == ReasonCode.LIMITS_EXHAUSTED.value


def test_no_eligible_members():
    run = {"members": [], "counters": _counters({}),
           "policy": SchedulerPolicy(max_rounds=3, max_messages_per_member=3), "events": []}
    result = BuildReviewTopology().propose_next(run, [])
    assert isinstance(result, RunCompletion)
    assert result.reason == ReasonCode.NO_ELIGIBLE_MEMBERS.value


# --- sign-off detection -----------------------------------------------------

def test_is_signoff_detection():
    assert _is_signoff("LGTM")
    assert _is_signoff("approve")
    assert _is_signoff('{"review_verdict": "approve"}')
    assert _is_signoff("Looks good.\nLGTM")
    assert not _is_signoff("I do not approve this yet")
    assert not _is_signoff("request_changes")
    assert not _is_signoff('{"review_verdict": "request_changes"}')
    assert not _is_signoff("")


# --- end-to-end: the engine drives a build_review run to sign-off -----------

import asyncio  # noqa: E402

import pytest  # noqa: E402

from errorta_council.engine import build_and_run  # noqa: E402
from errorta_council.gateway_local import (  # noqa: E402
    LocalCouncilModelRequest,
    LocalCouncilModelResult,
    LocalGateway,
)
from errorta_council.run_store import RunStore  # noqa: E402
from errorta_council.schema import EventType  # noqa: E402


class _BuildReviewGateway(LocalGateway):
    """programmer proposes; the reviewer signs off on the first review."""

    async def call(self, request: LocalCouncilModelRequest) -> LocalCouncilModelResult:
        member_id = str(request.metadata.get("member_id") or "")
        content = "Proposed the change." if member_id == "prog" else "LGTM"
        return LocalCouncilModelResult(
            content=content, provider="fake", provider_class="local",
            model=request.model, input_tokens=None, output_tokens=None,
            duration_ms=1, raw_usage_available=False,
        )

    async def is_reachable(self) -> bool:
        return True


class _FakeMeta:
    async def is_reachable(self) -> bool:
        return True

    async def list_installed_models(self) -> list[str]:
        return ["stub-model"]


@pytest.mark.asyncio
async def test_build_review_run_completes_on_signoff(tmp_errorta_home, runs_dir_path):
    room = {
        "id": "rm-br", "allow_full_context": True,
        "context_access_ceiling": "full_context",
        "transcript_access_ceiling": "all_messages",
        "members": [
            {"id": "prog", "role": "programmer", "enabled": True, "provider": "fake",
             "model": "stub-model", "context_access": "prompt_only",
             "transcript_access": "all_messages", "gateway_route_id": "fake.local.deterministic"},
            {"id": "rev1", "role": "member", "enabled": True, "provider": "fake",
             "model": "stub-model", "context_access": "prompt_only",
             "transcript_access": "all_messages", "gateway_route_id": "fake.local.deterministic"},
        ],
        "topology": {"kind": "build_review", "max_iterations": 5,
                     "speaker_order": ["prog", "rev1"]},
        "finalization_policy": {"mode": "transcript_only"},
    }
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(room_id="rm-br", room_snapshot=room,
                            prompt="add a function", corpus_ids=[])
    final = await asyncio.wait_for(
        build_and_run(
            run_store=store, run_meta=meta,
            policy=SchedulerPolicy(max_rounds=5, max_messages_per_member=5),
            gateway_meta=_FakeMeta(), hardware_scan_present=True,
            gateway=_BuildReviewGateway(),
        ),
        timeout=10.0,
    )
    assert final.status == "completed"
    _, events = store.read_run(meta.id)
    reason = next(
        (e.payload or {}).get("reason") for e in events
        if e.type == EventType.RUN_COMPLETED
    )
    assert reason == REVIEW_SIGNED_OFF
    # The programmer spoke before the reviewer signed off.
    msgs = [e for e in events if e.type == EventType.MEMBER_MESSAGE]
    assert msgs[0].member_id == "prog"
