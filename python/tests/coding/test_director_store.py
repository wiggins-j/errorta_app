"""F118-01 — Director entity + store + ownership invariant + aggregation."""
from __future__ import annotations

import pytest

from errorta_council.coding import attention, director
from errorta_council.coding.governance import GovernanceState, GovernanceStore
from errorta_council.coding.ledger import LedgerStore

AGENT = {"gateway_route_id": "fake.local.deterministic", "provider_kind": "local",
         "model_display": "deterministic"}


def _project(pid: str) -> LedgerStore:
    store = LedgerStore(pid)
    store.create_project(north_star="n", definition_of_done="d", target="new",
                         repo_path=None)
    return store


def _problem(pid, store, stage="drafting_spec", title="t"):
    return attention.raise_signal(
        pid, kind="problem", source="pm", stage=stage, title=title, summary="s",
        pm_evaluation="e", suggestions=[{"id": "s1", "label": "x"}], store=store)


# --- CRUD + persistence -----------------------------------------------------
def test_create_get_list_round_trip(tmp_errorta_home):
    d = director.create_director(name="Boss", agent=AGENT, project_ids=["p1", "p2"])
    assert d.id.startswith("dir-")
    assert director.get_director(d.id).name == "Boss"
    assert [x.id for x in director.list_directors()] == [d.id]
    # survives reload (fresh process would re-read director.json)
    assert director.get_director(d.id).project_ids == ["p1", "p2"]


def test_duplicate_project_ids_rejected(tmp_errorta_home):
    with pytest.raises(director.DirectorError):
        director.create_director(name="x", project_ids=["p1", "p1"])


def test_invalid_project_ids_rejected(tmp_errorta_home):
    with pytest.raises(director.DirectorError):
        director.create_director(name="x", project_ids=["bad/project"])
    d = director.create_director(name="x", project_ids=[])
    with pytest.raises(director.DirectorError):
        director.update_director(d.id, project_ids=["bad\\project"])


# --- ownership invariant (<=1 director per project) -------------------------
def test_one_director_per_project_on_create(tmp_errorta_home):
    director.create_director(name="A", project_ids=["p1"])
    with pytest.raises(director.DirectorError):
        director.create_director(name="B", project_ids=["p1"])
    # a different project is fine
    assert director.create_director(name="B", project_ids=["p2"]) is not None


def test_ownership_enforced_on_update_remove_then_add(tmp_errorta_home):
    a = director.create_director(name="A", project_ids=["p1"])
    b = director.create_director(name="B", project_ids=["p2"])
    # B cannot grab p1 while A owns it
    with pytest.raises(director.DirectorError):
        director.update_director(b.id, project_ids=["p2", "p1"])
    # free p1 from A, then B can take it (remove-then-add ordering)
    director.update_director(a.id, project_ids=[])
    moved = director.update_director(b.id, project_ids=["p2", "p1"])
    assert set(moved.project_ids) == {"p1", "p2"}


def test_update_self_retain_does_not_self_conflict(tmp_errorta_home):
    a = director.create_director(name="A", project_ids=["p1", "p2"])
    # re-saving the same set must not trip the invariant against itself
    again = director.update_director(a.id, name="A2")
    assert again.project_ids == ["p1", "p2"] and again.name == "A2"


# --- delete -----------------------------------------------------------------
def test_delete_frees_projects_leaves_them_intact(tmp_errorta_home):
    a = director.create_director(name="A", project_ids=["p1"])
    assert director.delete_director(a.id) is True
    assert director.get_director(a.id) is None
    # p1 is now free for another director
    assert director.create_director(name="B", project_ids=["p1"]) is not None
    assert director.delete_director("dir-nope") is False


# --- aggregation ------------------------------------------------------------
def test_aggregate_attention_groups_problems_first_skips_missing(tmp_errorta_home):
    s1 = _project("proj-a")
    _problem("proj-a", s1, title="A-prob")
    attention.raise_signal("proj-a", kind="alert", source="reviewer",
                           stage="reviewing_build", title="A-alert", summary="s",
                           store=s1)
    d = director.create_director(name="A", project_ids=["proj-a", "ghost-proj"])
    queue = director.aggregate_attention(d.id)
    assert [g["project_id"] for g in queue] == ["proj-a"]  # ghost skipped (no signals)
    kinds = [sig["kind"] for sig in queue[0]["signals"]]
    assert kinds == ["problem", "alert"]  # Problems first


def test_aggregate_unknown_director_raises(tmp_errorta_home):
    with pytest.raises(director.DirectorError):
        director.aggregate_attention("dir-nope")


# --- briefing ---------------------------------------------------------------
def test_project_briefing_grounds_on_real_state(tmp_errorta_home):
    store = LedgerStore("brief-proj")
    store.create_project(north_star="n", definition_of_done="d", target="new",
                         repo_path=None)
    GovernanceStore.for_ledger(store).save_state(
        GovernanceState(mode="light", phase="drafting_spec"))
    _problem("brief-proj", store)
    b = director.project_briefing("brief-proj")
    assert b["project_id"] == "brief-proj"
    assert b["open_problems"] == 1 and b["open_alerts"] == 0
    assert b["running"] is False  # no live run
    assert "stage" in b and "status" in b


# --- chat -------------------------------------------------------------------
def test_chat_append_and_load(tmp_errorta_home):
    d = director.create_director(name="A", project_ids=[])
    director.append_chat(d.id, role="user", text="status?")
    director.append_chat(d.id, role="director", text="all healthy")
    chat = director.load_chat(d.id)
    assert [c["role"] for c in chat] == ["user", "director"]
    assert chat[1]["text"] == "all healthy"


def test_chat_unknown_director_rejected(tmp_errorta_home):
    with pytest.raises(director.DirectorError):
        director.append_chat("dir-missing", role="user", text="status?")
    with pytest.raises(director.DirectorError):
        director.load_chat("dir-missing")


def test_inbox_blocking_problems_first(tmp_errorta_home):
    sa = _project("ip1")
    sb = _project("ip2")
    attention.raise_signal("ip1", kind="alert", source="reviewer",
                           stage="reviewing_build", title="a1", summary="s", store=sa)
    _problem("ip2", sb, title="p2")  # blocking problem
    d = director.create_director(name="A", project_ids=["ip1", "ip2"])
    items = director.inbox(d.id)
    # the blocking Problem (ip2) ranks above the Alert (ip1)
    assert items[0]["project_id"] == "ip2"
    assert items[0]["signal"]["kind"] == "problem"
    assert {it["project_id"] for it in items} == {"ip1", "ip2"}
