import json
from pathlib import Path
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.orientation import build_orientation_packet


def _seed(tmp_path: Path) -> LedgerStore:
    s = LedgerStore("p", root=tmp_path)
    s.create_project(north_star="Build X", definition_of_done="tests pass",
                     target="new", repo_path=None)
    a = s.add_task(title="impl A", role="dev")
    s.update_task(a.task_id, state="doing", assignee_member_id="m-dev")
    s.add_task(title="impl B", role="dev")
    for i in range(20):
        s.record_decision(title=f"dec {i}", context="c", choice=f"ch{i}", rationale="r")
    return s


def test_packet_has_core_and_in_flight(tmp_path: Path) -> None:
    store = _seed(tmp_path)
    store.record_tool_event(
        turn_id="turn-1", task_id="t1", member_id="m-dev", role="dev",
        tool="code_write", status="succeeded", intent={"path": "a.py"},
        result={"path": "a.py"},
    )
    pkt = build_orientation_packet(store, token_budget=10_000)
    assert pkt.north_star == "Build X" and pkt.definition_of_done == "tests pass"
    assert [t["title"] for t in pkt.in_flight_tasks] == ["impl A"]
    assert any(t["title"] == "impl B" for t in pkt.next_tasks)
    assert pkt.recent_tool_events[0]["tool"] == "code_write"


def test_packet_trims_to_fit_when_it_can(tmp_path: Path) -> None:
    pkt = build_orientation_packet(_seed(tmp_path), token_budget=300)
    assert len(json.dumps(pkt.to_dict())) // 4 <= 300
    assert pkt.north_star == "Build X"
    assert [t["title"] for t in pkt.in_flight_tasks] == ["impl A"]
    assert pkt.truncated is True
    assert len(pkt.recent_decisions) < 20


def test_packet_keeps_core_even_when_core_exceeds_budget(tmp_path: Path) -> None:
    pkt = build_orientation_packet(_seed(tmp_path), token_budget=10)
    assert pkt.north_star == "Build X"
    assert [t["title"] for t in pkt.in_flight_tasks] == ["impl A"]
    assert pkt.recent_decisions == [] and pkt.next_tasks == []
    assert pkt.truncated is True


def test_packet_includes_member_parse_rates(tmp_path: Path) -> None:
    store = _seed(tmp_path)
    store.record_turn(
        role="dev", member_id="m-dev", task_id="t1",
        prompt="p", response="r", outcome="noop", parse_ok=False,
    )
    store.record_turn(
        role="dev", member_id="m-dev", task_id="t2",
        prompt="p", response="r", outcome="pr_opened", parse_ok=True,
    )

    pkt = build_orientation_packet(store, token_budget=10_000).to_dict()

    assert pkt["member_parse_rates"]["m-dev"] == {
        "member_id": "m-dev", "role": "dev", "ok": 1, "total": 2, "rate": 0.5,
    }
