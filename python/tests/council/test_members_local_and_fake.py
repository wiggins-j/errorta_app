from __future__ import annotations

import asyncio

import pytest

from errorta_council.gateway_local import (
    FatalError,
    LocalCouncilModelRequest,
    LocalGateway,
    RetryableError,
)
from errorta_council.members.fake import (
    FakeBehavior,
    FakeCouncilMember,
    fake_completion_async,
    register_fake_member,
    reset_fake_members,
)
from errorta_council.members.local import LocalMemberAdapter


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_fake_members()
    yield
    reset_fake_members()


def test_phase0_construction_still_works() -> None:
    """Fix 6 additivity gate: instantiate FakeCouncilMember WITHOUT the new
    behavior= parameter and verify Phase 0 happy-path semantics are preserved."""
    m = FakeCouncilMember(member_id="m1")
    assert m.member_id == "m1"
    assert m.provider_class == "fake"
    assert m.canned_content == "deterministic fake reply"
    # The new behavior field defaults to SUCCESS.
    assert m.behavior is FakeBehavior.SUCCESS


def test_fake_behavior_enum_vocabulary() -> None:
    """Locks the FakeBehavior vocabulary."""
    assert FakeBehavior.SUCCESS.value == "success"
    assert FakeBehavior.TIMEOUT.value == "timeout"
    assert FakeBehavior.CANCELLED.value == "cancelled"
    assert FakeBehavior.MISSING_MODEL.value == "missing_model"
    assert FakeBehavior.MALFORMED_OUTPUT.value == "malformed_output"
    assert FakeBehavior.PROVIDER_ERROR.value == "provider_error"
    assert FakeBehavior.NULLABLE_USAGE.value == "nullable_usage"


def test_register_fake_member_success_variant() -> None:
    register_fake_member("m1", FakeBehavior.SUCCESS, content="predefined")
    m = FakeCouncilMember(member_id="m1")
    assert m.behavior is FakeBehavior.SUCCESS


@pytest.mark.asyncio
async def test_register_fake_member_timeout_variant_raises_via_gateway() -> None:
    register_fake_member("m1", FakeBehavior.TIMEOUT)
    gw = LocalGateway()
    req = LocalCouncilModelRequest(
        role="member", route_id="r", provider="fake", model="m",
        messages=[{"role": "user", "content": "hi"}],
        max_output_tokens=64, temperature=0.0, timeout_seconds=5,
        metadata={"member_id": "m1"},
    )
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(gw.call(req), timeout=0.05)


@pytest.mark.asyncio
async def test_register_fake_member_provider_error_variant() -> None:
    register_fake_member("m1", FakeBehavior.PROVIDER_ERROR)
    gw = LocalGateway()
    req = LocalCouncilModelRequest(
        role="member", route_id="r", provider="fake", model="m",
        messages=[{"role": "user", "content": "hi"}],
        max_output_tokens=64, temperature=0.0, timeout_seconds=5,
        metadata={"member_id": "m1"},
    )
    with pytest.raises(RetryableError):
        await gw.call(req)


@pytest.mark.asyncio
async def test_register_fake_member_missing_model_variant() -> None:
    register_fake_member("m1", FakeBehavior.MISSING_MODEL)
    gw = LocalGateway()
    req = LocalCouncilModelRequest(
        role="member", route_id="r", provider="fake", model="nope",
        messages=[{"role": "user", "content": "hi"}],
        max_output_tokens=64, temperature=0.0, timeout_seconds=5,
        metadata={"member_id": "m1"},
    )
    with pytest.raises(FatalError) as exc:
        await gw.call(req)
    assert "model_not_found" in str(exc.value)


def test_local_member_adapter_has_protocol_fields() -> None:
    member = LocalMemberAdapter(member_id="m1", model="llama3.2:1b", route_id="r1")
    assert member.member_id == "m1"
    assert member.provider_class == "local"
