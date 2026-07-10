"""F078 Slice 4 (core) — reviewer assignment (no self-review, distribution)."""
from __future__ import annotations

from errorta_council.credibility.credidation import assign_reviewers
from errorta_council.credibility.models import Claim, ClaimPacket


def _packet(member_id: str, *claims: Claim) -> ClaimPacket:
    return ClaimPacket(packet_id=f"pkt_{member_id}", member_id=member_id,
                       claims=list(claims))


def _c(cid: str, **kw) -> Claim:
    base = dict(claim_id=cid, text="x", kind="factual", risk="normal")
    base.update(kw)
    return Claim(**base)


def test_never_self_review() -> None:
    packets = [_packet("m-1", _c("c1")), _packet("m-2", _c("c2"))]
    a = assign_reviewers(packets=packets, member_ids=["m-1", "m-2"])
    assert a["c1"] and "m-1" not in a["c1"]
    assert a["c2"] and "m-2" not in a["c2"]


def test_each_claim_gets_a_reviewer() -> None:
    packets = [_packet("m-1", _c("c1"), _c("c2")), _packet("m-2", _c("c3"))]
    a = assign_reviewers(packets=packets, member_ids=["m-1", "m-2", "m-3"])
    for cid in ("c1", "c2", "c3"):
        assert len(a[cid]) == 1


def test_distribution_is_spread() -> None:
    # Three claims by m-1 across a 3-member room should not all dump on one peer.
    packets = [_packet("m-1", _c("c1"), _c("c2"), _c("c3"))]
    a = assign_reviewers(packets=packets, member_ids=["m-1", "m-2", "m-3"])
    reviewers = [a["c1"][0], a["c2"][0], a["c3"][0]]
    assert set(reviewers) == {"m-2", "m-3"}  # both peers used, author excluded


def test_strict_key_claim_two_reviewers() -> None:
    packets = [_packet("m-1", _c("c1", risk="high"))]  # high ⇒ key
    a = assign_reviewers(packets=packets, member_ids=["m-1", "m-2", "m-3"],
                         strictness="strict")
    assert len(a["c1"]) == 2
    assert "m-1" not in a["c1"]


def test_two_reviewers_capped_when_only_one_peer() -> None:
    packets = [_packet("m-1", _c("c1", risk="high"))]
    a = assign_reviewers(packets=packets, member_ids=["m-1", "m-2"],
                         strictness="strict")
    assert a["c1"] == ["m-2"]  # only one non-author available


def test_uncited_observation_not_assigned() -> None:
    packets = [_packet("m-1", _c("c1", kind="uncited_observation"))]
    a = assign_reviewers(packets=packets, member_ids=["m-1", "m-2"])
    assert "c1" not in a


def test_deterministic() -> None:
    packets = [_packet("m-1", _c("c1"), _c("c2")), _packet("m-2", _c("c3"))]
    members = ["m-1", "m-2", "m-3"]
    assert assign_reviewers(packets=packets, member_ids=members) == \
        assign_reviewers(packets=packets, member_ids=members)
