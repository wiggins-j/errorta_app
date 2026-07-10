"""F031-05 effective-policy matrix: min(member, room, topology, corpus, residency, caps).

Records both requested and effective so the manifest can show narrowing.
Invariant 4 (no silent degradation): unknown combinations BLOCK.
"""
from __future__ import annotations

from errorta_council.context.policy import EffectiveContextPolicy


def test_member_request_narrowed_by_room_ceiling():
    pol = EffectiveContextPolicy.compute(
        member_request={"context_access": "full_context", "transcript_access": "all_messages"},
        room={"context_access_ceiling": "redacted_summary", "transcript_access_ceiling": "all_messages",
              "allow_full_context": False},
        topology={"context_access_ceiling": "full_context"},
        corpus_policy={"max_egress_class": "remote_eligible"},
        residency={"destination_scope": "local"},
        token_caps={"max_input_tokens": 8192},
    )
    assert pol.requested_context_access == "full_context"
    assert pol.effective_context_access == "redacted_summary"
    assert pol.narrowed_by == ["room.context_access_ceiling"]


def test_corpus_egress_remote_local_only_blocks_by_default():
    """F031-05 P2 lock: ``remote`` + ``max_egress_class=local_only`` +
    a corpus-bearing access mode (retrieved_snippets / answer_context /
    full_context) must BLOCK by default (invariant 4). Earlier policy
    silently degraded to redacted_summary, which is only allowed under
    an explicit corpus_egress_fallback policy.
    """
    pol = EffectiveContextPolicy.compute(
        member_request={"context_access": "full_context", "transcript_access": "all_messages"},
        room={"context_access_ceiling": "full_context", "transcript_access_ceiling": "all_messages",
              "allow_full_context": True},
        topology={"context_access_ceiling": "full_context"},
        corpus_policy={"max_egress_class": "local_only"},
        residency={"destination_scope": "remote"},
        token_caps={"max_input_tokens": 8192},
    )
    assert pol.effective_context_access == "blocked"
    assert pol.blocked_reason == "corpus_egress_blocked"
    assert pol.egress_class == "blocked"


def test_corpus_egress_remote_local_only_degrades_when_explicit_fallback():
    """Same case but the corpus policy opts into a redacted_summary
    fallback — the router-spec carve-out for environments that prefer
    degradation over hard-block.
    """
    pol = EffectiveContextPolicy.compute(
        member_request={"context_access": "full_context", "transcript_access": "all_messages"},
        room={"context_access_ceiling": "full_context", "transcript_access_ceiling": "all_messages",
              "allow_full_context": True},
        topology={"context_access_ceiling": "full_context"},
        corpus_policy={
            "max_egress_class": "local_only",
            "corpus_egress_fallback": "redacted_summary",
        },
        residency={"destination_scope": "remote"},
        token_caps={"max_input_tokens": 8192},
    )
    assert pol.effective_context_access == "redacted_summary"
    assert pol.blocked_reason is None
    assert any("fallback=redacted_summary" in n for n in pol.narrowed_by)


def test_unknown_member_request_blocks():
    pol = EffectiveContextPolicy.compute(
        member_request={"context_access": "experimental_v3", "transcript_access": "all_messages"},
        room={"context_access_ceiling": "full_context", "transcript_access_ceiling": "all_messages",
              "allow_full_context": True},
        topology={"context_access_ceiling": "full_context"},
        corpus_policy={"max_egress_class": "remote_eligible"},
        residency={"destination_scope": "local"},
        token_caps={"max_input_tokens": 8192},
    )
    assert pol.effective_context_access == "blocked"
    assert pol.blocked_reason == "unknown_context_access"
