"""QA P1 review-finding lock — gateway boundary re-check on live path.

`verify_payload_route_alignment` exists and is unit-tested. The review
caught that `LocalGateway.call()` never actually invokes it, so an
inconsistent payload would happily reach Ollama. This test fires the
real `call()` method with a deliberately mismatched payload (local
egress, remote destination, mismatched route) and asserts FatalError —
before any provider HTTP would have been attempted.
"""
from __future__ import annotations

import pytest

from errorta_council.gateway_local import (
    FatalError,
    LocalCouncilModelRequest,
    LocalGateway,
)


@pytest.mark.asyncio
async def test_call_rejects_mismatched_egress_destination() -> None:
    """Local-class egress + remote destination must FatalError before HTTP."""
    gw = LocalGateway()
    req = LocalCouncilModelRequest(
        role="member", route_id="local/ollama/x",
        provider="local", model="stub",
        messages=[{"role": "user", "content": "hi"}],
        max_output_tokens=64, temperature=0.0, timeout_seconds=5,
        metadata={
            "context_id": "ctx-1", "member_id": "m-1",
            "destination_scope": "remote",
            "egress_class": "local",
        },
    )
    with pytest.raises(FatalError) as excinfo:
        await gw.call(req)
    assert "payload_route_mismatch" in str(excinfo.value)


@pytest.mark.asyncio
async def test_call_rejects_unknown_destination_scope() -> None:
    gw = LocalGateway()
    req = LocalCouncilModelRequest(
        role="member", route_id="local/ollama/x",
        provider="local", model="stub",
        messages=[{"role": "user", "content": "hi"}],
        max_output_tokens=64, temperature=0.0, timeout_seconds=5,
        metadata={
            "context_id": "ctx-1", "member_id": "m-1",
            "destination_scope": "teleporting",
            "egress_class": "local",
        },
    )
    with pytest.raises(FatalError) as excinfo:
        await gw.call(req)
    assert "unknown_destination" in str(excinfo.value)


@pytest.mark.asyncio
async def test_call_rejects_unknown_egress_class() -> None:
    gw = LocalGateway()
    req = LocalCouncilModelRequest(
        role="member", route_id="local/ollama/x",
        provider="local", model="stub",
        messages=[{"role": "user", "content": "hi"}],
        max_output_tokens=64, temperature=0.0, timeout_seconds=5,
        metadata={
            "context_id": "ctx-1", "member_id": "m-1",
            "destination_scope": "local",
            "egress_class": "experimental_v9",
        },
    )
    with pytest.raises(FatalError) as excinfo:
        await gw.call(req)
    assert "unknown_egress_class" in str(excinfo.value)


@pytest.mark.asyncio
async def test_call_accepts_aligned_payload_then_dispatches() -> None:
    """Aligned local payload passes the re-check and dispatches to fake."""
    gw = LocalGateway()
    req = LocalCouncilModelRequest(
        role="member", route_id="fake.local.deterministic",
        provider="fake", model="stub",
        messages=[{"role": "user", "content": "hi"}],
        max_output_tokens=64, temperature=0.0, timeout_seconds=5,
        metadata={
            "context_id": "ctx-1", "member_id": "m-1",
            "destination_scope": "fake",
            "egress_class": "local",
        },
    )
    result = await gw.call(req)
    assert result.provider == "fake"
