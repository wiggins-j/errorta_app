from pathlib import Path
from errorta_council.coding.ledger import LedgerStore


def _store(tmp_path: Path) -> LedgerStore:
    s = LedgerStore("p", root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    return s


def test_decisions_append_and_list(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.record_decision(title="use sqlite", context="need storage", choice="sqlite",
                      rationale="simple", alternatives=["json"])
    got = LedgerStore("p", root=tmp_path).list_decisions()
    assert got[0]["choice"] == "sqlite" and got[0]["alternatives"] == ["json"]


def test_artifacts_upsert_last_per_path(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.upsert_artifact(path="src/a.py", status="created", last_task_id="t1",
                      content_sha256="a" * 64, summary="parser")
    s.upsert_artifact(path="src/a.py", status="modified", last_task_id="t2",
                      content_sha256="b" * 64)
    arts = LedgerStore("p", root=tmp_path).list_artifacts()
    assert len(arts) == 1 and arts[0]["status"] == "modified" and arts[0]["content_sha256"] == "b" * 64


def test_skill_uses_append(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.record_skill_use(member_id="m-dev", task_id="t1",
                       skill="test-driven-development", phase="write-failing-test")
    assert s.list_skill_uses()[0]["skill"] == "test-driven-development"


def test_tool_events_append_and_limit(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.record_tool_event(
        turn_id="turn-1", task_id="t1", member_id="m-dev", role="dev",
        tool="code_write", status="succeeded",
        intent={"path": "a.py"}, result={"path": "a.py", "head": "abc"},
    )
    s.record_tool_event(
        turn_id="turn-2", task_id="t2", member_id="m-dev", role="dev",
        tool="code_write", status="failed",
        intent={"path": "../x"}, error="unsafe path",
    )
    all_events = s.list_tool_events()
    assert [e["status"] for e in all_events] == ["succeeded", "failed"]
    assert all_events[0]["result"]["head"] == "abc"
    assert s.list_tool_events(limit=1)[0]["error"] == "unsafe path"
