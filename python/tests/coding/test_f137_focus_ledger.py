"""F137 — Current Focus goals: ledger model, CRUD, lifecycle, migration."""
from __future__ import annotations

from pathlib import Path

import pytest

from errorta_council.coding.ledger import Focus, LedgerError, LedgerStore


def _project(project_id: str = "proj", *, work_request: str = "") -> LedgerStore:
    store = LedgerStore(project_id)
    store.create_project(north_star="ns", definition_of_done="d",
                         target="existing", repo_path="/tmp/x",
                         work_request=work_request)
    return store


def test_add_and_list_focuses_ordered(tmp_errorta_home: Path) -> None:
    store = _project()
    a = store.add_focus(title="first")
    b = store.add_focus(title="second", body="detail")
    active = store.active_focuses()
    assert [f.title for f in active] == ["first", "second"]
    assert active[0].order == 0 and active[1].order == 1
    assert active[1].body == "detail"
    assert a.status == "active" and b.origin == "user"


def test_add_focus_requires_title(tmp_errorta_home: Path) -> None:
    store = _project()
    with pytest.raises(LedgerError):
        store.add_focus(title="   ")


def test_reorder_focuses(tmp_errorta_home: Path) -> None:
    store = _project()
    a = store.add_focus(title="a")
    b = store.add_focus(title="b")
    c = store.add_focus(title="c")
    store.reorder_focuses([c.id, a.id, b.id])
    assert [f.title for f in store.active_focuses()] == ["c", "a", "b"]


def test_reorder_partial_keeps_omitted_after(tmp_errorta_home: Path) -> None:
    store = _project()
    a = store.add_focus(title="a")
    b = store.add_focus(title="b")
    c = store.add_focus(title="c")
    # only rank c explicitly; a,b keep relative order after it
    store.reorder_focuses([c.id])
    assert [f.title for f in store.active_focuses()] == ["c", "a", "b"]
    assert b.id and a.id  # ids stable


def test_propose_then_accept_archives_without_completing_project(
        tmp_errorta_home: Path) -> None:
    store = _project()
    f = store.add_focus(title="ship it")
    completed = store.propose_focus_complete(f.id, "merged PR #9")
    assert completed.status == "completed"
    assert completed.completed_at and completed.completion_summary == "merged PR #9"
    # still active-visible? no — it's completed, not active
    assert [x.title for x in store.active_focuses()] == []
    accepted = store.accept_focus(f.id)
    assert accepted.status == "archived"
    assert accepted.accepted_at and accepted.archived_at
    # F137 D6: accepting a focus must NOT complete the project
    assert store.get_project().status == "active"
    # archived focus is history, never active
    assert store.list_focuses(status="archived")[0].id == f.id
    assert store.active_focuses() == []


def test_archived_focus_cannot_be_resurrected(tmp_errorta_home: Path) -> None:
    store = _project()
    f = store.add_focus(title="drop me")
    store.update_focus(f.id, status="archived")
    # neither the PM propose path nor the human accept path may revive it
    with pytest.raises(LedgerError):
        store.propose_focus_complete(f.id, "sneaky")
    with pytest.raises(LedgerError):
        store.accept_focus(f.id)
    with pytest.raises(LedgerError):
        store.update_focus(f.id, status="active")
    with pytest.raises(LedgerError):
        store.update_focus(f.id, title="rewritten history")
    assert store.list_focuses(status="archived")[0].status == "archived"


def test_read_migration_is_best_effort_on_write_failure(
        tmp_errorta_home: Path, monkeypatch) -> None:
    """A read (list_focuses) on a read-only / remote mount must not crash — it
    returns the in-memory migrated seed even when persistence fails."""
    store = _project(work_request="fix the header")

    def boom(_focuses):
        raise OSError("read-only file system")

    monkeypatch.setattr(store, "_write_focuses", boom)
    focuses = store.active_focuses()  # must not raise
    assert [f.title for f in focuses] == ["fix the header"]
    assert focuses[0].origin == "work_request_migration"


def test_direct_archive_drops_focus(tmp_errorta_home: Path) -> None:
    store = _project()
    f = store.add_focus(title="drop me")
    updated = store.update_focus(f.id, status="archived")
    assert updated.status == "archived" and updated.archived_at
    assert store.active_focuses() == []


def test_accept_requires_completed_focus(tmp_errorta_home: Path) -> None:
    store = _project()
    f = store.add_focus(title="still active")
    with pytest.raises(LedgerError, match="must be completed"):
        store.accept_focus(f.id)
    assert store.active_focuses()[0].id == f.id


def test_update_focus_validates_fields_and_status(tmp_errorta_home: Path) -> None:
    store = _project()
    f = store.add_focus(title="x")
    with pytest.raises(LedgerError):
        store.update_focus(f.id, status="bogus")
    with pytest.raises(LedgerError):
        store.update_focus(f.id, order=5)  # not a patchable field
    with pytest.raises(LedgerError):
        store.update_focus("focus-missing", title="y")


def test_title_and_body_capped(tmp_errorta_home: Path) -> None:
    store = _project()
    f = store.add_focus(title="t" * 50_000, body="b" * 50_000)
    assert len(f.title) == LedgerStore._TURN_FIELD_CAP
    assert len(f.body) == LedgerStore._TURN_FIELD_CAP


def test_migrates_legacy_work_request_once(tmp_errorta_home: Path) -> None:
    store = _project(work_request="fix the header")
    assert not store._focus_path.exists()
    active = store.active_focuses()  # triggers migration
    assert [f.title for f in active] == ["fix the header"]
    assert active[0].origin == "work_request_migration"
    # idempotent — a second read does not double-seed
    assert len(store.active_focuses()) == 1
    # adding a real focus afterwards does not re-run migration
    store.add_focus(title="new work")
    assert [f.title for f in store.active_focuses()] == ["fix the header", "new work"]


def test_no_migration_without_work_request(tmp_errorta_home: Path) -> None:
    store = _project()
    assert store.active_focuses() == []


def test_set_work_request_upserts_primary_focus(tmp_errorta_home: Path) -> None:
    store = _project()
    store.set_work_request("focus one")
    assert [f.title for f in store.active_focuses()] == ["focus one"]
    # retitles the primary, does not append
    store.set_work_request("focus one revised")
    assert [f.title for f in store.active_focuses()] == ["focus one revised"]
    # with a second focus present, set_work_request only touches the primary
    store.add_focus(title="focus two")
    store.set_work_request("primary again")
    assert [f.title for f in store.active_focuses()] == ["primary again", "focus two"]


def test_directive_text_renders_active_set(tmp_errorta_home: Path) -> None:
    store = _project()
    assert store.current_focus_directive_text() == ""
    store.add_focus(title="a")
    store.add_focus(title="b", body="notes")
    text = store.current_focus_directive_text()
    assert "1. a" in text and "2. b — notes" in text


def test_focus_roundtrips_unknown_fields(tmp_errorta_home: Path) -> None:
    # forward-compat: an unknown field on disk survives a read/write cycle
    store = _project()
    f = store.add_focus(title="x")
    raw = f.to_dict()
    raw["future_field"] = 42
    restored = Focus.from_dict(raw)
    assert restored._extras == {"future_field": 42}
    assert restored.to_dict()["future_field"] == 42


def test_digest_surfaces_focus_goals_distinct_from_task(
        tmp_errorta_home: Path) -> None:
    store = _project()
    store.add_focus(title="collapsible panel")
    digest = store.regenerate_digest()
    # the long-standing "current_focus" (task-in-doing) stays; goals are separate
    assert digest["current_focus"] is None
    assert digest["current_focus_goals"] == ["collapsible panel"]


def test_concurrent_add_and_reorder_lose_nothing(tmp_errorta_home: Path) -> None:
    """Lock-held full-rewrite must not drop a racing add (mirror F135 review #1)."""
    import threading

    store = _project()
    n = 60

    def adder(prefix: str) -> None:
        for i in range(n):
            store.add_focus(title=f"{prefix}-{i}")

    t1 = threading.Thread(target=adder, args=("x",))
    t2 = threading.Thread(target=adder, args=("y",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    titles = [f.title for f in store.active_focuses()]
    assert len([t for t in titles if t.startswith("x-")]) == n
    assert len([t for t in titles if t.startswith("y-")]) == n
