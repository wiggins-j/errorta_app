"""A room with subscription-CLI members must validate once the editor lifts
the remote-call budget (regression for the 'can't save after adding Claude /
ChatGPT/Cursor CLI' bug).

The backend classifies claude_cli.* / codex_cli.* / cursor_cli.* routes as remote, so a room
with CLI members requires max_remote_calls_per_run > 0. The editor counts
those providers as remote and auto-bumps the cap on save; this locks both the
blocked-at-zero and valid-with-headroom states.
"""
from __future__ import annotations

from dataclasses import replace

from errorta_council.gateway_meta import RealGatewayMeta
from errorta_council.validation import validate_room


def _cli_room(sample_room, member_factory, remote_cap):
    cli = [
        member_factory("Claude", gateway_route_id="claude_cli.opus",
                       provider_kind="claude_cli"),
        member_factory("GPT", gateway_route_id="codex_cli.default",
                       provider_kind="codex_cli"),
        member_factory("Cursor", gateway_route_id="cursor_cli.gpt-5",
                       provider_kind="cursor_cli"),
    ]
    topo = replace(sample_room.topology, speaker_order=["Claude", "GPT", "Cursor"])
    budget = replace(sample_room.budget_policy,
                     max_remote_calls_per_run=remote_cap,
                     max_total_model_calls=3)
    return replace(sample_room, members=cli, topology=topo, budget_policy=budget)


def test_cli_members_blocked_at_zero_remote_budget(sample_room, member_factory):
    result = validate_room(_cli_room(sample_room, member_factory, 0),
                           RealGatewayMeta())
    assert any(e["code"] == "remote_member_zero_budget" for e in result.errors)


def test_cli_members_validate_with_remote_headroom(sample_room, member_factory):
    result = validate_room(_cli_room(sample_room, member_factory, 3),
                           RealGatewayMeta())
    assert result.status == "ready", result.errors
    assert not any(e["code"] == "remote_member_zero_budget" for e in result.errors)
