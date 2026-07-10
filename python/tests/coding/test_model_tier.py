"""F127 Workstream C — model-tier table (pure, table-driven)."""
from __future__ import annotations

import pytest

from errorta_council.coding import model_tier as mt


@pytest.mark.parametrize(
    "route, expected",
    [
        ("claude_cli.opus", mt.STRONG),
        ("anthropic.claude-opus-4-8", mt.STRONG),
        ("cursor_cli.gpt-5.3-codex-high", mt.STRONG),
        ("claude_cli.claude-4.5-sonnet-thinking", mt.STRONG),
        ("claude_cli.sonnet", mt.MID),
        ("cursor_cli.gpt-5.2", mt.MID),
        ("cursor_cli.gemini-3.1-pro", mt.MID),
        ("cursor_cli.composer-2.5", mt.MID),
        ("claude_cli.haiku", mt.LIGHT),
        ("cursor_cli.gpt-5.3-codex-low", mt.LIGHT),
        ("openai.gpt-5-mini", mt.LIGHT),
        ("cursor_cli.gemini-3-flash", mt.LIGHT),
        ("local.ollama.llama3.2:3b", mt.MID),  # never assume
        ("fake.local.deterministic", mt.MID),
        ("", mt.MID),
        ("weird.unknown-model-xyz", mt.MID),
    ],
)
def test_tier_for_route(route, expected) -> None:
    assert mt.tier_for_route(route) == expected


def test_rank_ordering() -> None:
    assert mt.tier_rank(mt.STRONG) > mt.tier_rank(mt.MID) > mt.tier_rank(mt.LIGHT)
    assert mt.tier_rank("nonsense") == mt.tier_rank(mt.MID)


def test_member_tier_override_wins() -> None:
    m = {"gateway_route_id": "claude_cli.haiku", "metadata": {"model_tier": "strong"}}
    assert mt.member_tier(m) == mt.STRONG
    assert mt.member_rank(m) == mt.tier_rank(mt.STRONG)


def test_member_tier_ignores_bogus_override() -> None:
    m = {"gateway_route_id": "claude_cli.opus", "metadata": {"model_tier": "ultra"}}
    assert mt.member_tier(m) == mt.STRONG  # falls back to derived


def test_member_tier_derived_when_no_override() -> None:
    assert mt.member_tier({"gateway_route_id": "claude_cli.haiku"}) == mt.LIGHT
