"""Deterministic fake member + configuration matrix (invariant 10).

Fix 6: this module ADDITIVELY extends the Phase 0 FakeCouncilMember.
Existing Phase 0 call sites (FakeCouncilMember(member_id="m1")) continue
to work because the new `behavior` field has a default of FakeBehavior.SUCCESS.
The module-level _FAKE_REGISTRY allows tests to drive per-member behavior
without mutating the FakeCouncilMember instance.
"""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# Phase-0 cancel/budget/result types stay imported from the Phase 0 base module.
# (Phase 0 plan Task 5 placed these in members/base.py — keep that import.)
from errorta_council.members.base import (
    CancellationToken,
    ContextPayload,
    MemberTurnResult,
    TurnBudget,
)


class FakeBehavior(str, Enum):
    """Failure-mode matrix driving FakeCouncilMember + fake_completion_async."""

    SUCCESS = "success"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    MISSING_MODEL = "missing_model"
    MALFORMED_OUTPUT = "malformed_output"
    PROVIDER_ERROR = "provider_error"
    NULLABLE_USAGE = "nullable_usage"


# Module-level configuration registry.
@dataclass(frozen=True)
class _FakeConfig:
    behavior: FakeBehavior = FakeBehavior.SUCCESS
    content: str | None = None


_FAKE_REGISTRY: dict[str, _FakeConfig] = {}


def register_fake_member(
    member_id: str,
    behavior: FakeBehavior = FakeBehavior.SUCCESS,
    *,
    content: str | None = None,
) -> None:
    """Configure the per-member behavior for the next dispatch."""
    _FAKE_REGISTRY[member_id] = _FakeConfig(behavior=behavior, content=content)


def reset_fake_members() -> None:
    _FAKE_REGISTRY.clear()


@dataclass(frozen=True)
class FakeCouncilMember:
    """Phase 0 FakeCouncilMember additively extended.

    Phase 0 fields (member_id, provider_class, canned_content) are unchanged.
    Phase 1 adds the `behavior` field with a SUCCESS default so existing
    constructions still succeed.
    """

    member_id: str
    provider_class: str = "fake"
    canned_content: str = "deterministic fake reply"
    behavior: FakeBehavior = FakeBehavior.SUCCESS

    async def generate(
        self,
        payload: ContextPayload,
        *,
        budget: TurnBudget,
        cancel: CancellationToken,
    ) -> MemberTurnResult:
        # Phase 0 happy-path behavior preserved verbatim.
        if cancel.is_cancelled:
            return MemberTurnResult(
                content="", finish_reason="cancelled",
                usage={"input_tokens": 0, "output_tokens": 0},
            )
        return MemberTurnResult(
            content=f"{self.canned_content} [{self.member_id}]",
            finish_reason="stop",
            usage={"input_tokens": 0, "output_tokens": len(self.canned_content)},
        )


def fake_completion(*, model: str, messages: list[dict], member_id: str | None = None) -> str:
    """Synchronous deterministic happy-path completion."""
    cfg = _FAKE_REGISTRY.get(member_id or "", _FakeConfig())
    if cfg.behavior is FakeBehavior.SUCCESS and cfg.content is not None:
        return cfg.content
    seed = hashlib.sha256(
        (model + "|" + "|".join(m.get("content", "") for m in messages)).encode()
    ).hexdigest()[:8]
    return f"fake-answer ({seed})"


async def fake_completion_async(
    *, model: str, messages: list[dict], member_id: str | None
) -> str:
    """Async dispatch matching the gateway's RetryableError/FatalError seam."""
    from errorta_council.gateway_local import FatalError, RetryableError
    cfg = _FAKE_REGISTRY.get(member_id or "", _FakeConfig())
    behavior = cfg.behavior
    if behavior is FakeBehavior.SUCCESS:
        return fake_completion(model=model, messages=messages, member_id=member_id)
    if behavior is FakeBehavior.TIMEOUT:
        await asyncio.sleep(3600)
        return "unreachable"
    if behavior is FakeBehavior.CANCELLED:
        raise asyncio.CancelledError("fake_cancellation")
    if behavior is FakeBehavior.MISSING_MODEL:
        raise FatalError(f"model_not_found: {model}")
    if behavior is FakeBehavior.MALFORMED_OUTPUT:
        raise FatalError("malformed_response: fake_mode")
    if behavior is FakeBehavior.PROVIDER_ERROR:
        raise RetryableError("local_provider_5xx: fake_mode")
    if behavior is FakeBehavior.NULLABLE_USAGE:
        return "fake-nullable-usage"
    raise FatalError(f"unknown_fake_behavior: {behavior}")
