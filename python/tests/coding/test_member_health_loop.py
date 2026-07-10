"""F120-02 marquee — a uniformly-failing member STOPS the run, fast, loudly.

The live incident: a `claude_cli` 401 returns exit 0 with `is_error: true`, so
`ClaudeCliHandler.call` raises `FatalError("claude_cli_error: API Error: 401 …
Please run /login")`. That FatalError was swallowed by the loop's
failure-isolation wrapper and the brainstorm PM turn re-ran 276 times in 27 min
with no error surfaced. These tests lock that this can't recur:

- the run reaches a terminal `member_unhealthy` state within `cap + 1` turns
  (NOT hundreds), in BOTH the sequential and concurrent loops;
- an open `source="member_health"` Problem names the member + `reason=auth_failed`
  + a redacted detail + a remediation;
- a transient single failure followed by an `ok` does NOT raise;
- `block_on_problems=off` auto-resolves the signal but keeps it listed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from errorta_council.coding import attention
from errorta_council.coding.autonomy import (
    CADENCE_OFF,
    MEMBER_UNHEALTHY,
    CodingAutonomyPolicy,
    LoopCounters,
    TurnOutcome,
    _account_member_outcome,
    run_coding_loop,
)
from errorta_council.coding.governance import GovernanceState, GovernanceStore
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.member_health import classify_member_failure
from errorta_council.coding.runner import build_run_turn, members_by_coding_role
from errorta_council.gateway_local import FatalError

# Verbatim live-incident string (async_claude_cli.py raises this for the 401).
CLAUDE_401 = (
    "claude_cli_error: API Error: 401 "
    '{"type":"error","error":{"type":"authentication_error",'
    '"message":"Please run /login"}}'
)

_MEMBERS = [
    {"id": "m-1", "enabled": True, "metadata": {"coding_role": "pm"},
     "gateway_route_id": "claude_cli.opus", "provider_kind": "claude_cli"},
    {"id": "m-2", "enabled": True, "metadata": {"coding_role": "dev"},
     "gateway_route_id": "claude_cli.opus", "provider_kind": "claude_cli"},
    {"id": "m-3", "enabled": True, "metadata": {"coding_role": "reviewer"},
     "gateway_route_id": "claude_cli.opus", "provider_kind": "claude_cli"},
    {"id": "m-4", "enabled": True, "metadata": {"coding_role": "tester"},
     "gateway_route_id": "claude_cli.opus", "provider_kind": "claude_cli"},
]
_MEMBER_PAIRS = [(m["id"], m["metadata"]["coding_role"]) for m in _MEMBERS]


def _governed_store(tmp_path: Path, *, block_on_problems: bool = True) -> LedgerStore:
    s = LedgerStore("mh", root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    GovernanceStore.for_ledger(s).save_state(
        GovernanceState(mode="light", phase="brainstorming",
                        block_on_problems=block_on_problems))
    return s


def _run(store: LedgerStore, caller, *, max_parallel_workers: int,
         member_failure_limit: int = 3):
    rt = build_run_turn(store, None, members_by_coding_role(_MEMBERS), caller,
                        guardrail_enabled=False)
    return run_coding_loop(
        store, _MEMBER_PAIRS,
        CodingAutonomyPolicy(
            checkpoint_cadence=CADENCE_OFF,
            member_failure_limit=member_failure_limit,
            max_iterations=200,
            max_parallel_workers=max_parallel_workers,
        ),
        run_turn=rt,
    )


@pytest.mark.parametrize("max_parallel_workers", [1, 2])
def test_uniformly_failing_member_stops_fast_with_problem(
    tmp_path: Path, max_parallel_workers: int,
):
    store = _governed_store(tmp_path)

    def caller(member, prompt):  # noqa: ANN001 — always the live 401
        raise FatalError(CLAUDE_401)

    res = _run(store, caller, max_parallel_workers=max_parallel_workers)

    # Terminal member_unhealthy — NOT a 200-iteration budget runaway.
    assert res.stop_reason == MEMBER_UNHEALTHY
    # auth_failed is terminal -> cap 1, so it stops within a couple of turns.
    assert res.counters.iterations <= 2, res.counters.iterations
    assert res.counters.iterations < 100  # categorically not "hundreds"

    problems = [s for s in attention.list_open("mh", store=store)
                if s.source == "member_health"]
    assert len(problems) == 1
    p = problems[0]
    assert p.kind == "problem" and p.blocking is True
    assert p.context.get("reason") == "auth_failed"
    assert p.context.get("member_id") in {"m-1", "m-2", "m-3", "m-4"}
    # Redacted detail recorded + remediation present.
    assert "401" in (p.context.get("detail") or "")
    assert "login" in (p.context.get("remediation") or "").lower()
    # A Problem must carry a non-empty pm_evaluation + >=1 suggestion (criterion #2).
    assert p.pm_evaluation
    assert len(p.suggestions) >= 1


@pytest.mark.parametrize("max_parallel_workers", [1, 2])
def test_transient_single_failure_does_not_raise(
    tmp_path: Path, max_parallel_workers: int,
):
    """A member that fails ONCE then succeeds must not raise a Problem and must
    not strand the run (criterion #8). We use a transient reason (timeout) so the
    cap is member_failure_limit (3), not 1."""
    store = _governed_store(tmp_path)
    calls = {"n": 0}

    def caller(member, prompt):  # noqa: ANN001
        from errorta_council.coding.governance_schemas import parse_governance_turn  # noqa: F401
        calls["n"] += 1
        if calls["n"] == 1:
            from errorta_council.gateway_local import RetryableError
            raise RetryableError("claude_cli_timeout")
        # Then return a valid PM brainstorm turn so the run makes progress.
        import json
        return json.dumps({
            "schema_version": "governance_turn.v1",
            "role": "pm",
            "intent": {
                "kind": "brainstorm_draft",
                "title": "Idea",
                "body_markdown": "## Idea\nDo the thing.",
            },
        })

    res = _run(store, caller, max_parallel_workers=max_parallel_workers)
    # The single timeout did not raise a member-health Problem.
    assert not [s for s in attention.list_all("mh", store=store)
                if s.source == "member_health"]
    assert res.stop_reason != MEMBER_UNHEALTHY


def test_block_off_auto_resolves_but_lists(tmp_path: Path):
    store = _governed_store(tmp_path, block_on_problems=False)

    def caller(member, prompt):  # noqa: ANN001
        raise FatalError(CLAUDE_401)

    res = _run(store, caller, max_parallel_workers=1)
    assert res.stop_reason == MEMBER_UNHEALTHY

    # block_on_problems off: no OPEN member-health problem, but it IS recorded as
    # auto_resolved and still visible.
    assert not [s for s in attention.list_open("mh", store=store)
                if s.source == "member_health"]
    all_mh = [s for s in attention.list_all("mh", store=store)
              if s.source == "member_health"]
    assert len(all_mh) == 1
    assert all_mh[0].state == "auto_resolved"
    assert all_mh[0].context.get("reason") == "auth_failed"


def test_member_failure_counter_resets_on_member_success() -> None:
    """A member's first successful output clears its prior transient streak."""
    c = LoopCounters()
    policy = CodingAutonomyPolicy(member_failure_limit=3)
    failure = classify_member_failure(FatalError("claude_cli_timeout"))

    first = TurnOutcome(
        kind="member_failed", member_id="m-1", member_failure=failure)
    second = TurnOutcome(
        kind="member_failed", member_id="m-1", member_failure=failure)
    success = TurnOutcome(kind="planned", member_id="m-1")

    assert _account_member_outcome(c, policy, first) is None
    assert c.member_fail_counts["m-1"] == 1
    assert _account_member_outcome(c, policy, second) is None
    assert c.member_fail_counts["m-1"] == 2

    assert _account_member_outcome(c, policy, success) is None
    assert c.member_fail_counts["m-1"] == 0

    # After reset, two more transient failures still do not hit the cap.
    assert _account_member_outcome(c, policy, first) is None
    assert _account_member_outcome(c, policy, second) is None
