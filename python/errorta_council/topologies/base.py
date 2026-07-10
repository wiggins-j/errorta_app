"""Topology seam.

A topology proposes the next dispatch given run state and a transcript
view; it never calls members or stores directly (the scheduler in Phase 1
owns dispatch). Phase 0 ships the Protocol and minimal value types so
fake topologies are injectable from the first scheduler PR.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class RunState:
    run_id: str
    room_snapshot: dict[str, Any]
    last_sequence: int
    round: int
    finished: bool = False


@dataclass(frozen=True)
class TranscriptView:
    events: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class TurnProposal:
    member_id: str
    round: int
    turn_index: int
    reason: str


@dataclass(frozen=True)
class RunCompletion:
    reason: str   # "topology_exhausted" | "finalized" | "transcript_only" | "manual_finalize"


@runtime_checkable
class Topology(Protocol):
    def propose_next(
        self,
        run: RunState,
        transcript: TranscriptView,
    ) -> "TurnProposal | RunCompletion": ...
