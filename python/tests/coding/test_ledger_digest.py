from pathlib import Path
from errorta_council.coding.ledger import LedgerStore


def test_regenerate_digest_counts_open_tasks(tmp_path: Path) -> None:
    s = LedgerStore("p", root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    a = s.add_task(title="a", role="dev")
    s.update_task(a.task_id, state="doing")
    s.add_task(title="b", role="dev")
    d = s.regenerate_digest()
    assert d["open_task_count"] == 2
    assert d["current_focus"] == "a"
