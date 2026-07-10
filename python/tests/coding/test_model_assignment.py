from types import SimpleNamespace

from errorta_council.coding.model_assignment import bind_member_route, resolve_task_assignment
from errorta_council.coding.model_availability import RouteAvailability


def test_multi_assignment_overrides_out_of_pool_pm_pick(monkeypatch) -> None:
    pool = ["local.ollama.qwen:7b", "claude_cli.opus"]
    projection = {
        route: RouteAvailability(route, route.split(".", 1)[0], True, "") for route in pool
    }
    monkeypatch.setattr(
        "errorta_council.coding.model_availability.resolve_route_availability",
        lambda _routes: projection,
    )
    monkeypatch.setattr("errorta_council.coding.performance_corpus.digest", lambda: {})
    task = SimpleNamespace(
        task_id="t", task_type="implementation", difficulty_tier="mid",
        preferred_route_id="openai.not-in-pool", assignment_rationale="",
        model_assignment=None,
    )
    member = {"id": "m", "model_mode": "multi", "model_pool": pool}
    assignment, reason = resolve_task_assignment(task, member)
    assert assignment is not None
    assert assignment.route_id == "local.ollama.qwen:7b"
    assert assignment.source == "override"
    assert reason == "route_out_of_pool"
    assert bind_member_route(member, assignment)["provider_kind"] == "local"
