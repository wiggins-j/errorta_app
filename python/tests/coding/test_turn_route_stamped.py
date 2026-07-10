"""F143-01 Slice A — the resolved route is stamped as a first-class turn field.

Before this slice, the turn record stored the route only inside ``model_assignment``.
PM/review/test turns run outside the F129 assignment gate and so carried no route,
rolling up as ``unknown``. Now ``record_turn`` takes an explicit ``route_id`` kwarg,
writes it as a first-class field, and ``rollup_turns`` prefers it — so a turn that
used a route never reads back as ``unknown``/``""``.
"""
from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.usage_rollup import rollup_turns


def _new_store(tmp_path: Path, pid: str) -> LedgerStore:
    store = LedgerStore(pid, root=tmp_path)
    store.create_project(north_star="x", definition_of_done="d",
                         target="new", repo_path=None)
    return store


def test_record_turn_stamps_route_as_first_class_field(tmp_path: Path) -> None:
    store = _new_store(tmp_path, "rtstamp")
    # A PM turn with NO model_assignment (skips the F129 gate) but a resolved route.
    store.record_turn(
        role="pm", member_id="m-pm", task_id="plan",
        prompt="p", response="r", outcome="noop",
        route_id="claude_cli.sonnet")

    turns = store.list_turns()
    assert len(turns) == 1
    assert turns[0]["route_id"] == "claude_cli.sonnet"
    # No assignment was passed, so the record must not invent one.
    assert "model_assignment" not in turns[0]


def test_blank_route_is_not_written(tmp_path: Path) -> None:
    store = _new_store(tmp_path, "rtblank")
    store.record_turn(role="pm", member_id="m-pm", task_id="plan",
                      prompt="p", response="r", outcome="noop", route_id="")
    store.record_turn(role="pm", member_id="m-pm", task_id="plan",
                      prompt="p", response="r", outcome="noop", route_id="   ")
    store.record_turn(role="pm", member_id="m-pm", task_id="plan",
                      prompt="p", response="r", outcome="noop")  # route_id omitted

    for turn in store.list_turns():
        assert "route_id" not in turn


def test_rollup_buckets_under_first_class_route_not_unknown() -> None:
    # A turn with a first-class route_id and NO model_assignment must bucket under
    # the real route, never "unknown".
    turns = [
        {"member_id": "m-pm", "route_id": "cursor_cli.composer-2.5",
         "usage": {"measured": True, "input_tokens": 10, "output_tokens": 5}},
    ]
    r = rollup_turns(turns)
    assert "cursor_cli.composer-2.5" in r["by_route"]
    assert "unknown" not in r["by_route"]
    assert r["by_route"]["cursor_cli.composer-2.5"]["input"] == 10


def test_first_class_route_preferred_over_model_assignment() -> None:
    # When both are present, the first-class resolved route wins (it's the route the
    # gateway actually dispatched to).
    turns = [
        {"member_id": "m-dev", "route_id": "resolved.route",
         "model_assignment": {"route_id": "assigned.route"},
         "usage": {"measured": True, "input_tokens": 3, "output_tokens": 2}},
    ]
    r = rollup_turns(turns)
    assert "resolved.route" in r["by_route"]
    assert "assigned.route" not in r["by_route"]


def test_falls_back_to_model_assignment_then_member_route() -> None:
    turns = [
        # No first-class route -> falls back to model_assignment.route_id.
        {"member_id": "m1", "model_assignment": {"route_id": "assigned.route"},
         "usage": {"measured": True, "input_tokens": 1, "output_tokens": 1}},
        # Neither -> falls back to member_route hint.
        {"member_id": "m2", "member_route": "hint.route",
         "usage": {"measured": True, "input_tokens": 1, "output_tokens": 1}},
        # Nothing at all -> stable "unknown" bucket (tokens never dropped).
        {"member_id": "m3",
         "usage": {"measured": True, "input_tokens": 1, "output_tokens": 1}},
    ]
    r = rollup_turns(turns)
    assert "assigned.route" in r["by_route"]
    assert "hint.route" in r["by_route"]
    assert "unknown" in r["by_route"]
