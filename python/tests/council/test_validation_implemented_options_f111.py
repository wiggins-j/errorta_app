"""F111 — the room editor must not offer topologies / finalization modes that the
engine silently ignores. validate_room now rejects a kind/mode that is "known but
not implemented" (it used to be accepted then silently fall back to round_robin /
transcript_only). A canary keeps the IMPLEMENTED_* sets honest against the real
engine/scheduler dispatch.
"""
from __future__ import annotations

from dataclasses import replace

from errorta_council.gateway_meta import FakeGatewayMeta
from errorta_council.validation import (
    _ALLOWED_FINALIZATION_MODES,
    _ALLOWED_TOPOLOGY_KINDS,
    IMPLEMENTED_FINALIZATION_MODES,
    IMPLEMENTED_TOPOLOGY_KINDS,
    validate_room,
)


def _gw() -> FakeGatewayMeta:
    return FakeGatewayMeta(known_routes={}, catalog_version="2026-06-11")


def _codes(room) -> set[str]:
    return {e["code"] for e in validate_room(room, _gw()).errors}


# Inert = known to the allowlist but with no executed path in the engine.
_INERT_TOPOLOGIES = sorted(_ALLOWED_TOPOLOGY_KINDS - IMPLEMENTED_TOPOLOGY_KINDS)
_INERT_FINALIZATIONS = sorted(_ALLOWED_FINALIZATION_MODES - IMPLEMENTED_FINALIZATION_MODES)


def test_inert_topologies_are_rejected(seed_room_full) -> None:
    base = seed_room_full(member_count=2, provider="fake", model="stub-model", max_rounds=1)
    assert _INERT_TOPOLOGIES, "expected some known-but-unimplemented topologies"
    for kind in _INERT_TOPOLOGIES:
        room = replace(base, topology=replace(base.topology, kind=kind))
        assert "topology_kind_unimplemented" in _codes(room), kind


def test_implemented_topologies_pass(seed_room_full) -> None:
    base = seed_room_full(member_count=2, provider="fake", model="stub-model", max_rounds=1)
    # build_review is executed but not offered in the editor; Coding Team sets it
    # programmatically, so validation must still accept it.
    for kind in (
        "round_robin",
        "consensus_deliberation",
        "credibility",
        "build_review",
    ):
        room = replace(base, topology=replace(base.topology, kind=kind))
        assert "topology_kind_unimplemented" not in _codes(room), kind
        assert "unknown_topology_kind" not in _codes(room), kind


def test_inert_finalization_modes_are_rejected(seed_room_full) -> None:
    base = seed_room_full(member_count=2, provider="fake", model="stub-model", max_rounds=1)
    assert _INERT_FINALIZATIONS, "expected some known-but-unimplemented finalization modes"
    for mode in _INERT_FINALIZATIONS:
        room = replace(base, finalization_policy=replace(base.finalization_policy, mode=mode))
        assert "finalization_mode_unimplemented" in _codes(room), mode


def test_implemented_finalization_modes_pass(seed_room_full) -> None:
    base = seed_room_full(member_count=2, provider="fake", model="stub-model", max_rounds=1)
    for mode in ("transcript_only", "consensus_report", "summary", "credibility_report"):
        room = replace(base, finalization_policy=replace(base.finalization_policy, mode=mode))
        assert "finalization_mode_unimplemented" not in _codes(room), mode


def test_implemented_sets_are_subsets_of_allowed() -> None:
    # build_review is engine-executed but intentionally not offered in the room
    # editor. It still belongs in validation's allowlist because Coding Team sets
    # it programmatically.
    assert IMPLEMENTED_TOPOLOGY_KINDS <= _ALLOWED_TOPOLOGY_KINDS
    assert IMPLEMENTED_FINALIZATION_MODES <= _ALLOWED_FINALIZATION_MODES


def test_canary_implemented_sets_match_engine_dispatch() -> None:
    """Drift guard: these are the kinds/modes the engine/scheduler actually run.
    If you implement another topology in engine.py or another finalization branch
    in scheduler.py, update these sets (and the editor) together — that's the point
    of this test. Source of truth:
      * engine.py `_build_*`/topology dispatch -> round_robin / consensus_deliberation
        / credibility / build_review (else RoundRobinTopology()).
      * scheduler.py finalize -> transcript_only (baseline) / single_finalizer
        (`_last_finalizer_answer`) / consensus_report (`_maybe_synthesize_consensus`)
        / summary (`_maybe_synthesize_summary`, F031-28)
        / credibility_report (`_maybe_synthesize_credibility_report`).
    """
    assert IMPLEMENTED_TOPOLOGY_KINDS == {
        "round_robin", "consensus_deliberation", "credibility", "build_review",
    }
    assert IMPLEMENTED_FINALIZATION_MODES == {
        "transcript_only", "single_finalizer", "consensus_report", "summary",
        "credibility_report",
    }
