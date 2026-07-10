"""Pin the Council seam contracts.

The Protocols are the F001-SEAM equivalent for Council. Phase 0 only
needs them importable and structurally satisfiable by a deterministic
fake; later phases inject real members and topologies through them.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from errorta_council.members.base import (
    CancellationToken,
    ContextPayload,
    CouncilMember,
    MemberTurnResult,
    TurnBudget,
)
from errorta_council.topologies.base import (
    RunCompletion,
    RunState,
    Topology,
    TranscriptView,
    TurnProposal,
)


def test_imports_succeed() -> None:
    # If any of these raise on import, Phase 0 cannot start.
    assert ContextPayload.__name__ == "ContextPayload"
    assert TurnBudget.__name__ == "TurnBudget"


def test_minimal_context_payload_phase_0_shape() -> None:
    """Spec OQ#1 resolution: Phase 0 ContextPayload = {context_id, messages}."""
    payload = ContextPayload(context_id="ctx-1", messages=[{"role": "user", "content": "hi"}])
    assert payload.context_id == "ctx-1"
    assert payload.messages[0]["role"] == "user"


@dataclass
class _DummyMember:
    member_id: str = "m-fake"
    provider_class: str = "fake"

    async def generate(
        self, payload: ContextPayload, *, budget: TurnBudget, cancel: CancellationToken
    ) -> MemberTurnResult:
        return MemberTurnResult(
            content="ok", finish_reason="stop", usage={"input_tokens": 0, "output_tokens": 1}
        )


def test_dummy_satisfies_council_member_protocol() -> None:
    """Structural-typing check: ``isinstance`` against a runtime_checkable Protocol."""
    dummy = _DummyMember()
    assert isinstance(dummy, CouncilMember)


def test_dummy_generate_returns_member_turn_result() -> None:
    dummy = _DummyMember()
    payload = ContextPayload(context_id="ctx-1", messages=[])
    out = asyncio.run(
        dummy.generate(
            payload,
            budget=TurnBudget(max_input_tokens=None, max_output_tokens=None),
            cancel=CancellationToken(),
        )
    )
    assert out.content == "ok"
    assert out.finish_reason == "stop"


class _DummyTopology:
    def propose_next(self, run: RunState, transcript: TranscriptView) -> "TurnProposal | RunCompletion":
        return RunCompletion(reason="topology_exhausted")


def test_dummy_satisfies_topology_protocol() -> None:
    assert isinstance(_DummyTopology(), Topology)
