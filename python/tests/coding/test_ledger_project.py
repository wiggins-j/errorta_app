from pathlib import Path
import pytest
from errorta_council.coding.ledger import LedgerStore, ProjectNotFound


def test_create_and_get_project_roundtrips(tmp_path: Path) -> None:
    store = LedgerStore("proj-1", root=tmp_path)
    p = store.create_project(north_star="Build a CLI todo app",
                             definition_of_done="todo add/list/done work + tests pass",
                             target="new", repo_path=None)
    assert p.id == "proj-1" and p.target == "new" and p.status == "active" and p.revision == 1
    again = LedgerStore("proj-1", root=tmp_path).get_project()
    assert again.north_star == "Build a CLI todo app"
    assert again.definition_of_done.endswith("tests pass")


def test_get_missing_project_raises(tmp_path: Path) -> None:
    with pytest.raises(ProjectNotFound):
        LedgerStore("ghost", root=tmp_path).get_project()


def test_project_id_traversal_rejected(tmp_path) -> None:
    from errorta_council.coding.ledger import LedgerError
    for bad in ["../escaped", "/abs/x", "a/b", "..", "x\\y", "x\x00y", ""]:
        try:
            LedgerStore(bad, root=tmp_path)
            assert False, f"expected LedgerError for {bad!r}"
        except LedgerError:
            pass


def test_project_dir_stays_under_root(tmp_path) -> None:
    s = LedgerStore("ok-proj_1.2", root=tmp_path)
    assert s.dir.resolve().is_relative_to(tmp_path.resolve())
