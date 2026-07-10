"""F127 validation — the escalate-up ladder recovers a weak-worker wedge.

Deterministic (no live models): simulates the exact failure that wedged a
246/247-done run — a worker whose model keeps emitting agent tool-call markup
instead of the JSON turn. Drives the real `_handle_unproductive` ladder + the
real tier-aware scheduler and asserts the task is reassigned to a stronger member
(escalate-up, D1), and that when every member fails it a blocking attention
Problem is raised instead of a silent no_progress.

Run: ERRORTA_HOME=$(mktemp -d) python scripts/validate_f127.py

(A real-models mode — opus PM + haiku workers completing a toy project — is the
natural next step; it needs logged-in CLIs and is gated the same way the other
validate_*_live harnesses are. The deterministic path below proves the loop
logic that makes that configuration safe.)
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from errorta_council.coding import attention
from errorta_council.coding import model_tier as mt
from errorta_council.coding.autonomy import (
    CodingAutonomyPolicy,
    LoopCounters,
    TurnOutcome,
    _handle_unproductive,
)
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import build_run_turn, members_by_coding_role
from errorta_council.coding.schemas import TurnErrorCode, parse_coding_turn
from errorta_council.coding.topology import DEV, PM, REVIEWER, Assign, PMAssist, decide_next

HAIKU_MARKUP = (
    "I need to understand the context before proceeding. Let me examine the PR.\n"
    '<function_calls> <invoke name="Task">'
    '<parameter name="subagent_type">Explore</parameter></invoke> </function_calls>'
)


def _ok(label: str) -> None:
    print(f"  ✓ {label}")


def main() -> int:
    print("F127 validation — weak-worker wedge recovery\n")

    # 1) The verbatim Haiku output is now classified as tool-markup, retryable.
    parsed = parse_coding_turn("dev", "t-1", HAIKU_MARKUP)
    assert parsed.__class__.__name__ == "TurnParseError"
    assert parsed.code == TurnErrorCode.turn_tool_markup_only
    _ok("Haiku tool-call markup -> turn_tool_markup_only (recognized, not faked)")

    root = Path(tempfile.mkdtemp())
    store = LedgerStore("f127", root=root)
    store.create_project(north_star="n", definition_of_done="d", target="new",
                         repo_path=None)
    task = store.add_task(title="Main Loop Integration", role=DEV)
    members = [("onyx", DEV), ("vertex", DEV), ("nova", REVIEWER), ("yonder", PM)]
    tiers = {"onyx": mt.tier_rank(mt.LIGHT),    # haiku
             "vertex": mt.tier_rank(mt.STRONG),  # opus
             "nova": mt.tier_rank(mt.MID), "yonder": mt.tier_rank(mt.STRONG)}
    policy = CodingAutonomyPolicy(worker_unproductive_limit=2)
    c = LoopCounters()

    def unproductive(member: str) -> TurnOutcome:
        return TurnOutcome(kind="noop", unproductive=True, member_id=member,
                           member_role=DEV, member_route="claude_cli.haiku",
                           reason="turn_tool_markup_only")

    onyx_turn = Assign(member_id="onyx", task_id=task.task_id, role=DEV)

    # 2) Onyx (haiku) fails the task twice -> reassigned, Onyx excluded.
    _handle_unproductive(store, onyx_turn, unproductive("onyx"), c, policy, members)
    stop = _handle_unproductive(store, onyx_turn, unproductive("onyx"), c, policy, members)
    assert stop is None, "should reassign, not stop (Vertex is still eligible)"
    assert any(d["choice"] == "worker_excluded" for d in store.list_decisions())
    _ok("Onyx (haiku) failed 2x -> task reassigned, Onyx excluded")

    # 3) The scheduler routes the task to the STRONGER idle dev (escalate up).
    action = decide_next(store, members, tiers)
    assert isinstance(action, Assign) and action.member_id == "vertex"
    _ok("Scheduler routes the reassigned task to Vertex (opus) — escalate up")

    # 4) If Vertex also fails, the PM gets one bounded re-scope turn first.
    vertex_turn = Assign(member_id="vertex", task_id=task.task_id, role=DEV)
    _handle_unproductive(store, vertex_turn, unproductive("vertex"), c, policy, members)
    stop2 = _handle_unproductive(store, vertex_turn, unproductive("vertex"), c, policy, members)
    assert stop2 is None
    pm_action = decide_next(store, members, tiers)
    assert isinstance(pm_action, PMAssist)
    _ok("All devs exhausted -> PM-assist re-scope rung (not no_progress)")

    # 5) If the PM cannot produce a valid re-scope either, raise the Problem.
    team = [
        {"id": "yonder", "enabled": True, "metadata": {"coding_role": PM}},
        {"id": "onyx", "enabled": True, "metadata": {"coding_role": DEV}},
        {"id": "vertex", "enabled": True, "metadata": {"coding_role": DEV}},
    ]
    run_turn = build_run_turn(
        store,
        None,
        members_by_coding_role(team),
        lambda _member, _prompt: "still not JSON",
        guardrail_enabled=True,
    )
    pm_outcome = run_turn(pm_action, store)
    assert pm_outcome.kind == "pm_assist_exhausted"
    problems = [s for s in attention.list_open("f127", store=store)
                if s.source == "worker_unproductive"]
    assert len(problems) == 1 and "stronger model" in problems[0].summary.lower()
    _ok("PM assist exhausted -> blocking worker_unproductive Problem")

    print("\nALL CHECKS PASSED — a weak worker no longer wedges the run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
