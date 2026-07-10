from pathlib import Path
from errorta_council.coding.ledger import LedgerStore


def _store(tmp_path: Path) -> LedgerStore:
    s = LedgerStore("p", root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    return s


def test_add_and_list_tasks(tmp_path: Path) -> None:
    s = _store(tmp_path)
    t = s.add_task(title="write parser", role="dev")
    assert t.state == "todo" and t.role == "dev" and t.task_id
    assert [x.title for x in s.list_tasks()] == ["write parser"]


def test_update_task_persists_last_state(tmp_path: Path) -> None:
    s = _store(tmp_path)
    t = s.add_task(title="x", role="dev")
    s.update_task(t.task_id, state="doing", assignee_member_id="m-dev")
    got = LedgerStore("p", root=tmp_path).list_tasks(state="doing")
    assert len(got) == 1 and got[0].assignee_member_id == "m-dev"


def test_next_task_skips_blocked_doing_and_unmet_deps(tmp_path: Path) -> None:
    s = _store(tmp_path)
    a = s.add_task(title="a", role="dev")
    b = s.add_task(title="b", role="dev", depends_on=[a.task_id])
    s.add_task(title="c", role="reviewer")
    assert s.next_task("dev").task_id == a.task_id
    s.update_task(a.task_id, state="done")
    assert s.next_task("dev").task_id == b.task_id
    s.update_task(b.task_id, state="doing")
    assert s.next_task("dev") is None
