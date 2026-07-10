"""Invariant 11 (schema additive): Phase 0 minimal ContextPayload still loads
into the Phase 3-extended dataclass without loss.

format_version stays at 1; Phase 0 callers that only pass {context_id, messages}
must keep working after Phase 3's extension (classes / egress_class /
source_refs / metadata are defaulted).
"""
from __future__ import annotations

from errorta_council.members.base import ContextPayload


def test_phase0_minimal_payload_still_constructs():
    """Only context_id is required; messages defaults to []."""
    p = ContextPayload(context_id="ctx-1")
    assert p.context_id == "ctx-1"
    assert p.messages == []
    # Phase 3 additions all default safely.
    assert p.classes == []
    assert p.egress_class is None
    assert p.source_refs == []
    assert p.metadata == {}


def test_phase0_messages_payload_round_trips_through_construction():
    p = ContextPayload(
        context_id="ctx-2",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert p.messages[0]["role"] == "user"
    assert p.classes == []
    assert p.source_refs == []


def test_phase3_extensions_are_independent_per_instance():
    """Fresh defaults — no shared mutable lists/dicts across instances (invariant 5)."""
    a = ContextPayload(context_id="ctx-a")
    b = ContextPayload(context_id="ctx-b")
    assert a.messages is not b.messages
    assert a.classes is not b.classes
    assert a.source_refs is not b.source_refs
    assert a.metadata is not b.metadata


def test_context_payload_is_frozen():
    p = ContextPayload(context_id="ctx-1")
    try:
        p.context_id = "mutated"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("ContextPayload must be frozen (invariant 5 friendliness)")
