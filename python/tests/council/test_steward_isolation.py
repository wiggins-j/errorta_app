"""F038 invariant-5 lock: a Steward Packet is built once from RAW member
content and is byte-identical for every recipient, so it must NOT be applied
to members whose transcript_access redacts/summarizes peer content. Only
``all_messages`` recipients (who already see the full raw transcript) receive
the packet. This regression-locks the 2026-06-13 architect-review blocker fix.
"""
from __future__ import annotations

import pytest

from errorta_council.context.router import (
    _STEWARD_SHARED_TRANSCRIPT_ACCESS,
    _apply_steward_packet,
)
from errorta_council.context.tokens import HeuristicEstimator


def _blocks():
    return [
        {
            "class_": "transcript_event", "content": "SECRET peer position",
            "tokens": 5, "content_sha256": "a" * 64,
            "transcript_event_id": "evt_1", "sequence": 1, "member_id": "m-1",
        },
        {
            "class_": "transcript_event", "content": "another position",
            "tokens": 4, "content_sha256": "b" * 64,
            "transcript_event_id": "evt_2", "sequence": 2, "member_id": "m-2",
        },
    ]


def test_only_all_messages_is_a_shared_transcript_access():
    # The set is the single source of truth for which recipients may receive
    # a shared deterministic packet. Redacted/summary modes must be excluded.
    assert _STEWARD_SHARED_TRANSCRIPT_ACCESS == frozenset({"all_messages"})
    assert "redacted_summary" not in _STEWARD_SHARED_TRANSCRIPT_ACCESS
    assert "summary_only" not in _STEWARD_SHARED_TRANSCRIPT_ACCESS


@pytest.mark.parametrize("access", ["redacted_summary", "summary_only"])
def test_packet_not_applied_to_redacting_recipients(tmp_errorta_home, access):
    blocks, meta, omitted = _apply_steward_packet(
        run_id="run-x",
        transcript_blocks=_blocks(),
        steward_policy_raw={"enabled": True, "recent_full_messages": 0},
        effective_transcript_access=access,
        estimator=HeuristicEstimator(),
    )
    # Blocks returned unchanged — no steward_packet block injected.
    assert all(b.get("class_") == "transcript_event" for b in blocks)
    assert not any(b.get("class_") == "steward_packet" for b in blocks)
    assert meta["fallback"] is True
    assert meta["reason"] == "transcript_access_not_shared"
    assert omitted == []


def test_disabled_policy_is_noop(tmp_errorta_home):
    blocks, meta, omitted = _apply_steward_packet(
        run_id="run-x",
        transcript_blocks=_blocks(),
        steward_policy_raw={"enabled": False},
        effective_transcript_access="all_messages",
        estimator=HeuristicEstimator(),
    )
    assert len(blocks) == 2
    assert meta == {}
    assert omitted == []
