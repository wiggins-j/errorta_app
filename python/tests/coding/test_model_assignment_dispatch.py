from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.model_availability import RouteAvailability
from errorta_council.coding.runner import build_run_turn, members_by_coding_role
from errorta_council.coding.topology import DEV, Assign


def test_coding_dispatch_receives_bound_route(tmp_path: Path, monkeypatch) -> None:
    store = LedgerStore("f129-dispatch", root=tmp_path)
    store.create_project(north_star="n", definition_of_done="d", target="new",
                         repo_path=None)
    task = store.add_task(title="inspect", role=DEV, difficulty_tier="mid")
    pool = ["local.ollama.qwen:7b", "claude_cli.opus"]
    monkeypatch.setattr(
        "errorta_council.coding.model_availability.resolve_route_availability",
        lambda routes: {
            route: RouteAvailability(route, route.split(".", 1)[0], True, "")
            for route in routes
        },
    )
    monkeypatch.setattr("errorta_council.coding.performance_corpus.digest", lambda *a, **k: {})
    seen: list[dict] = []

    def caller(member, _prompt):
        seen.append(dict(member))
        return "{}"

    members = [{
        "id": "m-dev", "enabled": True, "model_mode": "multi", "model_pool": pool,
        "metadata": {"coding_role": DEV},
    }]
    run_turn = build_run_turn(
        store, None, members_by_coding_role(members), caller, guardrail_enabled=True,
    )
    run_turn(Assign("m-dev", task.task_id, DEV), store)
    assert seen
    assert {member["gateway_route_id"] for member in seen} == {"local.ollama.qwen:7b"}
    assert all(member["provider_kind"] == "local" for member in seen)
    persisted = next(item for item in store.list_tasks() if item.task_id == task.task_id)
    assert persisted.model_assignment["route_id"] == "local.ollama.qwen:7b"
