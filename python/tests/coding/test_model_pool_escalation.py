from pathlib import Path

from errorta_council.coding.autonomy import (
    CodingAutonomyPolicy,
    LoopCounters,
    TurnOutcome,
    _handle_unproductive,
)
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.model_assignment import make_assignment
from errorta_council.coding.model_availability import RouteAvailability
from errorta_council.coding.topology import DEV, PM, Assign


def test_in_pool_escalation_precedes_member_exclusion(tmp_path: Path, monkeypatch) -> None:
    store = LedgerStore("f129-escalate", root=tmp_path)
    store.create_project(north_star="n", definition_of_done="d", target="new",
                         repo_path=None)
    pool = ["anthropic.haiku", "openai.gpt-5", "anthropic.opus"]
    monkeypatch.setattr(
        "errorta_council.coding.model_availability.resolve_route_availability",
        lambda routes: {
            route: RouteAvailability(route, route.split(".", 1)[0], True, "")
            for route in routes
        },
    )
    monkeypatch.setattr("errorta_council.coding.performance_corpus.digest", lambda: {})
    assignment = make_assignment(
        task_id="placeholder", member_id="m-dev", route_id="anthropic.haiku",
        task_type="implementation", difficulty_tier="light", rationale="cheap",
        source="selector",
    )
    task = store.add_task(
        title="implement", role=DEV, difficulty_tier="light",
        preferred_member_id="m-dev", model_assignment=assignment.to_dict(),
    )
    assignment = make_assignment(
        task_id=task.task_id, member_id="m-dev", route_id="anthropic.haiku",
        task_type="implementation", difficulty_tier="light", rationale="cheap",
        source="selector",
    )
    store.update_task(
        task.task_id, model_assignment=assignment.to_dict(), model_pool_snapshot=pool,
    )
    action = Assign(member_id="m-dev", task_id=task.task_id, role=DEV)
    outcome = TurnOutcome(
        kind="noop", unproductive=True, member_id="m-dev", member_role=DEV,
        member_route="anthropic.haiku", reason="turn_non_json",
    )
    counters = LoopCounters()
    policy = CodingAutonomyPolicy(worker_unproductive_limit=2, model_escalation_limit=2)
    members = [("m-dev", DEV), ("other", DEV), ("pm", PM)]
    assert _handle_unproductive(store, action, outcome, counters, policy, members) is None
    assert _handle_unproductive(store, action, outcome, counters, policy, members) is None
    updated = next(item for item in store.list_tasks() if item.task_id == task.task_id)
    assert updated.model_assignment["route_id"] == "openai.gpt-5"
    assert not (updated._extras.get("excluded_member_ids") or [])
    assert counters.model_escalations == 1
