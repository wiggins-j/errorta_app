"""SPEC-42 — the coding turn's output budget must fit the model.

`gateway_member_caller` hardcoded `max_output_tokens=2048`. A reasoning model spends
its budget on a hidden trace BEFORE the visible answer: measured on the RX 9060 XT
reference box, `qwen3.5:9b` averages **2197** generated tokens on a reviewer verdict
(`docs/coding/LOCAL_MODEL_SELECTION_RX9060XT.md` §4.1a). 2197 > 2048, so every such
turn truncated — and the empty `content` then reached the council disguised as an
answer via `gateway_local`'s `THINKING_TRACE_MARKER` substitution.

The scheduler has had the mitigation since F127 (`REASONING_MAX_OUTPUT_TOKENS`), but
the coding council never dispatches through it.
"""
from __future__ import annotations

from typing import Any

import pytest

from errorta_council.coding.runner import gateway_member_caller
from errorta_council.reasoning_budget import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    REASONING_MAX_OUTPUT_TOKENS,
    REASONING_TIMEOUT_FLOOR_SECONDS,
    is_reasoning_model,
    resolve_turn_budget,
)


class _CapturingGateway:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def call(self, request: Any) -> Any:
        self.requests.append(request)

        class _R:
            content = "{}"
            provider_class = "local"
            model = request.model
            input_tokens = output_tokens = None
            cache_read_input_tokens = cache_write_input_tokens = None
            raw_usage_available = False
            num_turns = None
            duration_ms = 0
        return _R()


def _member(route: str, **extra: Any) -> dict[str, Any]:
    m = {"id": "pm-1", "role": "answerer", "metadata": {"coding_role": "pm"},
         "gateway_route_id": route}
    m.update(extra)
    return m


def _sent(member: dict[str, Any]) -> Any:
    gw = _CapturingGateway()
    gateway_member_caller(gw)(member, "hello")
    return gw.requests[0]


# --------------------------------------------------------------------------- #
# The resolver
# --------------------------------------------------------------------------- #
def test_reasoning_model_gets_the_larger_budget() -> None:
    tokens, timeout = resolve_turn_budget("qwen3.5:9b", default_timeout_seconds=600)
    assert tokens == REASONING_MAX_OUTPUT_TOKENS
    assert timeout == 600  # already above the 300s floor; max() does not lower it


def test_non_reasoning_model_is_unchanged() -> None:
    assert resolve_turn_budget("qwen2.5-coder:7b",
                               default_timeout_seconds=600) == (
        DEFAULT_MAX_OUTPUT_TOKENS, 600)


def test_the_timeout_floor_raises_a_low_default() -> None:
    _, timeout = resolve_turn_budget("qwen3.5:9b", default_timeout_seconds=120)
    assert timeout == REASONING_TIMEOUT_FLOOR_SECONDS


def test_explicit_always_wins() -> None:
    """The operator typed it. The member editor's help text tells them to raise it
    for reasoning models, so second-guessing a persisted value overrides a human."""
    assert resolve_turn_budget(
        "qwen3.5:9b", explicit={"max_output_tokens": 4096})[0] == 4096
    # ...including a value SMALLER than the derived default.
    assert resolve_turn_budget(
        "qwen3.5:9b", explicit={"max_output_tokens": 512})[0] == 512


def test_disabled_falls_through_and_never_imposes_literals() -> None:
    """THE ESCAPE-HATCH REGRESSION.

    An earlier spec revision had `False` stamp `{max_output_tokens: 2048,
    timeout_seconds: 600}` into turn_limits. Two live teams persist 8192/6144 and no
    timeout_seconds, so that would have been a 4x DEMOTION of a deliberate operator
    budget. `False` must SUPPRESS the derived default, never impose one.
    """
    assert resolve_turn_budget("qwen3.5:9b", enabled=False,
                               default_timeout_seconds=600) == (
        DEFAULT_MAX_OUTPUT_TOKENS, 600)
    # An operator's 8192 survives with the hatch off.
    assert resolve_turn_budget(
        "qwen3.5:9b", explicit={"max_output_tokens": 8192}, enabled=False)[0] == 8192


# --------------------------------------------------------------------------- #
# The seam
# --------------------------------------------------------------------------- #
def test_local_reasoning_route_now_gets_8192() -> None:
    """The headline fix: this turn used to go out at 2048 against a 2197 mean."""
    assert _sent(_member("local.qwen3.5:9b")).max_output_tokens == \
        REASONING_MAX_OUTPUT_TOKENS


def test_local_non_reasoning_route_is_untouched() -> None:
    req = _sent(_member("local.qwen2.5-coder:7b"))
    assert req.max_output_tokens == DEFAULT_MAX_OUTPUT_TOKENS
    assert req.timeout_seconds == 600


@pytest.mark.parametrize("route", [
    "openai.o1", "openai.o3-mini", "anthropic.claude-3-7-sonnet-thinking",
    "google.gemini-2.0-flash-thinking-exp",
])
def test_hosted_reasoning_routes_do_not_get_the_raise(route: str) -> None:
    """THE HOSTED-LEAK REGRESSION.

    This seam is provider-agnostic and the marker list matches real hosted ids.
    Hosted handlers treat max_output_tokens as a HARD CAP (`async_anthropic.py:104`,
    `async_openai.py:63-69`), so raising it there silently changes paid-token
    behaviour that SPEC-42 puts out of scope. Worse, o-series models need
    `max_completion_tokens`, so the raise would not even take effect — it would just
    change which parameter is wrong.
    """
    assert is_reasoning_model(route.split(".", 1)[1]), \
        f"{route} must match a marker, or this test proves nothing"
    assert _sent(_member(route)).max_output_tokens == DEFAULT_MAX_OUTPUT_TOKENS


def test_explicit_member_budget_survives_at_the_seam() -> None:
    """The two live teams that persist 8192/6144 must be unaffected."""
    req = _sent(_member("local.qwen3.5:9b",
                        turn_limits={"max_output_tokens": 6144}))
    assert req.max_output_tokens == 6144


def test_seam_hatch_off_restores_the_old_number() -> None:
    assert _sent(_member("local.qwen3.5:9b",
                         reasoning_output_budget=False)).max_output_tokens == \
        DEFAULT_MAX_OUTPUT_TOKENS


def test_unbound_member_still_resolves_the_model_for_the_budget() -> None:
    """Ties SPEC-42 to the fix in 98a745b.

    A persisted member has no `model` key, so a budget resolver keying on
    `member["model"]` would miss exactly the PM/reviewer turns the benchmark
    measured. It must key on the resolved id.
    """
    m = _member("local.qwen3.5:9b")
    assert "model" not in m
    assert _sent(m).max_output_tokens == REASONING_MAX_OUTPUT_TOKENS


# --------------------------------------------------------------------------- #
# Move 4 — the interactive floor
# --------------------------------------------------------------------------- #
def test_interactive_reasoning_route_gets_a_floor_not_a_ceiling() -> None:
    """THE NO-OP REGRESSION.

    An earlier spec revision proposed changing `min(configured, 120)` to
    `min(configured, 240)`. That is a literal no-op: persisted coding members carry
    no turn_limits, so `configured` IS the 90/120 default, and min() cannot raise a
    value. The reasoning case needs a floor.
    """
    from errorta_app.routes.coding import _interactive_turn_timeout
    from errorta_council.reasoning_budget import (
        INTERACTIVE_REASONING_TIMEOUT_SECONDS,
    )
    m = {"gateway_route_id": "local.qwen3.5:9b"}
    assert _interactive_turn_timeout(m, 90) == INTERACTIVE_REASONING_TIMEOUT_SECONDS
    assert _interactive_turn_timeout(m, 120) == INTERACTIVE_REASONING_TIMEOUT_SECONDS
    # ...and the old min() would have produced the unchanged default.
    assert min(90, 240) == 90


def test_interactive_non_reasoning_route_keeps_todays_clamp() -> None:
    from errorta_app.routes.coding import _interactive_turn_timeout
    m = {"gateway_route_id": "local.qwen2.5-coder:7b"}
    assert _interactive_turn_timeout(m, 90) == 90
    assert _interactive_turn_timeout(m, 600) == 120       # ceiling still bites
    assert _interactive_turn_timeout(
        {"gateway_route_id": "anthropic.claude-sonnet-4-6"}, 120) == 120


def test_wizard_member_drops_the_hardcoded_budget() -> None:
    """The Wizard's synthetic PM must inherit the model-derived budget rather than
    pinning 2048 — explicit always wins, so a literal there would out-rank it."""
    from errorta_council.coding.wizard import _synthetic_member
    from errorta_council.reasoning_budget import (
        INTERACTIVE_REASONING_TIMEOUT_SECONDS,
    )
    reasoning = _synthetic_member("local.qwen3.5:9b")
    assert "max_output_tokens" not in reasoning["turn_limits"]
    assert reasoning["turn_limits"]["timeout_seconds"] == \
        INTERACTIVE_REASONING_TIMEOUT_SECONDS
    plain = _synthetic_member("local.qwen2.5-coder:7b")
    assert plain["turn_limits"]["timeout_seconds"] == 120
