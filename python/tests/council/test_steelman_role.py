"""F084 — steelman member role: quarantine + helpers."""
from __future__ import annotations

import pytest

from errorta_council.credibility import (
    member_is_steelman,
    run_credibility_pipeline,
    steelman_topic,
)
from errorta_council.credibility.evidence_store import EvidenceStore
from errorta_council.credibility.models import (
    Claim,
    ClaimPacket,
    CredidationReview,
    Source,
)
from errorta_council.schema import CredibilityPolicy


class _Meta:
    id = "r1"

    def __init__(self, snapshot):
        self.room_snapshot = snapshot


def _steelman_member(mid, topic="Existence of Santa"):
    return {"id": mid, "enabled": True,
            "metadata": {"steelman": True, "steelman_topic": topic}}


def _sched(members, cred):
    from errorta_council.scheduler import TurnScheduler
    s = TurnScheduler.__new__(TurnScheduler)
    s._meta = _Meta({"members": members, "credibility_policy": cred,
                     "finalization_policy": {"mode": "credibility_report"}})
    return s


def test_auto_opponent_skips_steelman():
    """A designated steelman is never auto-assigned the GENERIC opponent stance
    ('argue regardless of your own opinion') — its configured topic drives it."""
    s = _sched([{"id": "A", "enabled": True}, {"id": "B", "enabled": True},
                _steelman_member("C")],
               {"enabled": True, "rigor": "adversarial"})
    # C is last but is a steelman → opponent falls to B (last eligible non-steelman).
    assert s._credibility_opponent_id() == "B"
    assert s._credibility_steelman_member_ids() == {"C"}


@pytest.mark.asyncio
async def test_credibility_judge_answer_noop_without_judge():
    """F084: with no neutral judge enabled, the finalize-time judge-verdict pass
    is a no-op (the report keeps the leader's prose) — and never touches the
    gateway, so it's safe on a bare scheduler."""
    s = _sched([{"id": "A", "enabled": True}, {"id": "B", "enabled": True}],
               {"enabled": True, "rigor": "adversarial"})
    s._credibility_judge_answer = ""
    await s._run_credibility_judge_answer()
    assert s._credibility_judge_answer == ""


@pytest.mark.asyncio
async def test_credibility_judge_answer_noop_for_non_credibility_room():
    from errorta_council.scheduler import TurnScheduler
    s = TurnScheduler.__new__(TurnScheduler)
    s._meta = _Meta({
        "members": [{"id": "A", "enabled": True}],
        "credibility_policy": {},
        "finalization_policy": {"mode": "transcript_only"},
        "judge_policy": {"enabled": True, "judge_member_id": "A"},
    })
    s._credibility_judge_answer = ""
    await s._run_credibility_judge_answer()
    assert s._credibility_judge_answer == ""


def test_steelman_never_the_finalizer():
    """A steelman is backed out of the finalizer/synthesizer pool, even when it
    was the last member to answer — its advocacy must not become the verdict."""
    s = _sched([{"id": "A", "enabled": True}, {"id": "B", "enabled": True},
                _steelman_member("C")],
               {"enabled": True, "rigor": "adversarial"})
    s._last_answer = {"member_id": "C"}  # the steelman spoke last
    chosen = s._consensus_synthesizer_member()
    assert chosen is not None
    assert str(chosen["id"]) != "C"  # never the steelman


def test_finalizer_falls_back_when_only_steelmen():
    """Degenerate room (every non-judge member is a steelman) still yields a
    finalizer rather than None, so a report is produced."""
    s = _sched([_steelman_member("A"), _steelman_member("B")],
               {"enabled": True, "rigor": "adversarial"})
    s._last_answer = None
    chosen = s._consensus_synthesizer_member()
    assert chosen is not None


def test_steelman_round1_prompt_argues_topic_not_sources():
    s = _sched([{"id": "A", "enabled": True}, _steelman_member("C", "Existence of Santa")],
               {"enabled": True, "rigor": "adversarial"})
    s._credibility_findings = "(some fetched sources)"
    msgs = s._credibility_messages(
        [{"role": "user", "content": "Is Santa real?"}],
        member={"id": "C"}, proposal=type("P", (), {"round": 1})(),
    )
    injected = msgs[-1]["content"]
    assert "STEELMAN ADVOCATE" in injected
    assert "Existence of Santa" in injected
    assert "MAY construct supporting evidence" in injected
    # It is NOT told to cite ONLY the fetched URLs (the normal-member restriction).
    assert "Cite ONLY the fetched URLs" not in injected


def _store_with_one_source() -> EvidenceStore:
    store = EvidenceStore(run_id="r1")
    store.sources["s1"] = Source(
        source_id="s1", url="https://real.example/x",
        canonical_url="https://real.example/x", title="Real", source_type="reputable_news",
        fetched_at="2026-01-01T00:00:00Z",
    )
    return store


def test_member_is_steelman_helpers():
    assert member_is_steelman({"metadata": {"steelman": True}}) is True
    assert member_is_steelman({"metadata": {"steelman": False}}) is False
    assert member_is_steelman({"metadata": {}}) is False
    assert member_is_steelman({}) is False
    assert member_is_steelman(None) is False
    assert steelman_topic({"metadata": {"steelman_topic": "  Existence of Santa  "}}) == "Existence of Santa"
    assert steelman_topic({"metadata": {}}) == ""
    assert steelman_topic(None) == ""


def test_steelman_claims_quarantined_not_admitted():
    """A steelman's claims never enter admission/claims_used/source_map, even
    when they cite a real fetched source — they are surfaced separately."""
    store = _store_with_one_source()
    steel_pkt = ClaimPacket(
        packet_id="pkt_adv", member_id="adv",
        claims=[Claim(claim_id="adv:c1", text="Santa is real.", kind="factual",
                      source_ids=["https://real.example/x"])],
    )
    report = run_credibility_pipeline(
        packets=[steel_pkt], reviews=[], store=store,
        policy=CredibilityPolicy.from_dict({}), leader_answer="",
        steelman_member_ids={"adv"},
        steelman_topics={"adv": "Existence of Santa"},
    )
    # Quarantined: nothing admitted, nothing source-supported.
    assert report.claims_used == []
    assert report.admissions == []
    assert report.source_map == []
    assert report.caveats == []
    # Surfaced separately, labeled with the topic, citation preserved verbatim.
    assert len(report.steelman_claims) == 1
    s = report.steelman_claims[0]
    assert s["claim_id"] == "adv:c1"
    assert s["member_id"] == "adv"
    assert s["topic"] == "Existence of Santa"
    assert s["cited"] == ["https://real.example/x"]
    assert "steelman_claims" in report.to_dict()


def test_steelman_fabricated_url_never_promoted():
    """A constructed URL the steelman cites is NOT in the fetched store, so it
    never reaches source_map and is never treated as a real source."""
    store = _store_with_one_source()
    steel_pkt = ClaimPacket(
        packet_id="pkt_adv", member_id="adv",
        claims=[Claim(claim_id="adv:c1", text="The simulation admin confirmed it.",
                      kind="factual", source_ids=["https://totally-made-up.invalid/santa"])],
    )
    report = run_credibility_pipeline(
        packets=[steel_pkt], reviews=[], store=store,
        policy=CredibilityPolicy.from_dict({}), leader_answer="",
        steelman_member_ids={"adv"}, steelman_topics={"adv": "Existence of Santa"},
    )
    assert report.source_map == []
    assert report.steelman_claims[0]["cited"] == ["https://totally-made-up.invalid/santa"]
    # Confidence is computed from admitted claims only → low (none admitted).
    assert report.confidence == "low"


def test_format_answer_shows_unverified_steelman_section():
    """The deterministic text answer (mobile/text path) surfaces steelman claims
    in a clearly UNVERIFIED section with the topic."""
    from errorta_council.scheduler import _format_credibility_answer

    report = run_credibility_pipeline(
        packets=[ClaimPacket(
            packet_id="pkt_adv", member_id="adv",
            claims=[Claim(claim_id="adv:c1", text="Santa is real.", kind="factual",
                          source_ids=["https://made-up.invalid/y"])],
        )],
        reviews=[], store=_store_with_one_source(),
        policy=CredibilityPolicy.from_dict({}), leader_answer="The council is split.",
        steelman_member_ids={"adv"}, steelman_topics={"adv": "Existence of Santa"},
    )
    text = _format_credibility_answer(report)
    assert "Steelman arguments (UNVERIFIED" in text
    assert "Existence of Santa" in text
    assert "Santa is real." in text


def test_steelman_does_not_raise_confidence_for_real_claims():
    """A real member's well-supported claim still drives confidence; a steelman
    in the same run neither helps nor hurts that tally."""
    store = _store_with_one_source()
    real_pkt = ClaimPacket(
        packet_id="pkt_m1", member_id="m1",
        claims=[Claim(claim_id="m1:c1", text="X is documented.", kind="factual",
                      source_ids=["https://real.example/x"])],
    )
    steel_pkt = ClaimPacket(
        packet_id="pkt_adv", member_id="adv",
        claims=[Claim(claim_id="adv:c1", text="Santa is real.", kind="factual",
                      source_ids=["https://made-up.invalid/y"])],
    )
    review = CredidationReview(
        review_id="rev1", reviewer_member_id="m2", claim_id="m1:c1",
        status="verified", support_quality="direct", reason="source says so",
    )
    report = run_credibility_pipeline(
        packets=[real_pkt, steel_pkt], reviews=[review], store=store,
        policy=CredibilityPolicy.from_dict({}), leader_answer="",
        steelman_member_ids={"adv"}, steelman_topics={"adv": "Existence of Santa"},
    )
    assert report.claims_used == ["m1:c1"]
    assert len(report.steelman_claims) == 1
    # adv:c1 must not appear in admissions at all.
    assert all(a.claim_id != "adv:c1" for a in report.admissions)
