"""F145 Slice 4 — control-actions: grounded model reassignment + autonomy/governance."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from errorta_council.coding import control_actions as ca
from errorta_council.coding import pm_changes, pm_reference
from errorta_council.coding.autonomy import load_policy, policy_to_dict
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.workspace import CodingWorkspace

TAURI = {"x-errorta-origin": "tauri-ui"}
_AVAIL = [
    {"route_id": "local.qwen2.5-coder:7b", "family": "qwen", "provider_class": "local"},
    {"route_id": "anthropic.claude-sonnet-4.6", "family": "claude", "provider_class": "anthropic"},
    {"route_id": "anthropic.claude-opus-4.6", "family": "claude", "provider_class": "anthropic"},
]


def _team_project(pid: str) -> LedgerStore:
    store = LedgerStore(pid)
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    CodingWorkspace(pid, store).setup(target="new", repo_path=None)
    store.set_run_config(room_id=None, members=[
        {"id": "pm-1", "metadata": {"coding_role": "pm"}, "model_mode": "single",
         "gateway_route_id": "local.qwen2.5-coder:7b"},
        {"id": "dev-1", "metadata": {"coding_role": "dev"}, "model_mode": "single",
         "gateway_route_id": "local.qwen2.5-coder:7b"},
        {"id": "dev-2", "metadata": {"coding_role": "dev"}, "model_mode": "single",
         "gateway_route_id": "local.qwen2.5-coder:7b"},
        {"id": "rev-1", "metadata": {"coding_role": "reviewer"}, "model_mode": "single",
         "gateway_route_id": "local.qwen2.5-coder:7b"},
    ])
    return store


# --- name resolution (grounded-or-refuse) ---------------------------------
def test_resolve_route_exact_and_fuzzy():
    assert ca.resolve_route("anthropic.claude-sonnet-4.6", _AVAIL) == "anthropic.claude-sonnet-4.6"
    assert ca.resolve_route("sonnet", _AVAIL) == "anthropic.claude-sonnet-4.6"


def test_resolve_route_refuses_unavailable():
    with pytest.raises(ca.ControlActionError) as e:
        ca.resolve_route("Cursor Composer 2.5", _AVAIL)
    assert e.value.code == "model_not_found"


def test_resolve_route_refuses_ambiguous():
    with pytest.raises(ca.ControlActionError) as e:
        ca.resolve_route("claude", _AVAIL)  # matches both sonnet + opus
    assert e.value.code == "model_ambiguous"
    assert len(e.value.extra["candidates"]) == 2


# --- assign_models -------------------------------------------------------
def test_assign_models_by_role_reassigns_and_is_reversible(tmp_errorta_home: Path):
    store = _team_project("ca1")
    change = ca.assign_models_by_role(
        store, {"dev": "sonnet", "reviewer": "opus"}, available=_AVAIL)
    cfg = store.get_run_config()
    devs = [m for m in cfg["members"] if (m["metadata"]["coding_role"]) == "dev"]
    assert all(m["gateway_route_id"] == "anthropic.claude-sonnet-4.6" for m in devs)
    rev = next(m for m in cfg["members"] if m["metadata"]["coding_role"] == "reviewer")
    assert rev["gateway_route_id"] == "anthropic.claude-opus-4.6"
    # the PM member was untouched
    pm = next(m for m in cfg["members"] if m["metadata"]["coding_role"] == "pm")
    assert pm["gateway_route_id"] == "local.qwen2.5-coder:7b"
    # decline reverts the whole team
    pm_changes.decline(store, change.change_id)
    devs2 = [m for m in store.get_run_config()["members"]
             if m["metadata"]["coding_role"] == "dev"]
    assert all(m["gateway_route_id"] == "local.qwen2.5-coder:7b" for m in devs2)


def test_assign_refuses_unavailable_model_without_mutating(tmp_errorta_home: Path):
    store = _team_project("ca2")
    with pytest.raises(ca.ControlActionError):
        ca.assign_models_by_role(store, {"dev": "gpt-9"}, available=_AVAIL)
    # nothing changed
    assert all(m["gateway_route_id"] == "local.qwen2.5-coder:7b"
               for m in store.get_run_config()["members"])


# --- set_autonomy / governance ------------------------------------------
def test_set_autonomy_rejects_unknown_knob(tmp_errorta_home: Path):
    store = _team_project("ca3")
    with pytest.raises(ca.ControlActionError) as e:
        ca.set_autonomy(store, {"human_code_approval": "none"})
    assert e.value.code == "unknown_autonomy_knob"


def test_set_autonomy_off_flags_autonomy_warning(tmp_errorta_home: Path):
    store = _team_project("ca4")
    ch = ca.set_autonomy(store, {"checkpoint_cadence": "off"}, suggested_cap=500)
    assert policy_to_dict(load_policy(store))["checkpoint_cadence"] == "off"
    assert ch.autonomy == {"warning": True, "suggested_cap": 500}


# --- the route ----------------------------------------------------------
def _client() -> TestClient:
    from errorta_app.server import app
    return TestClient(app, headers=TAURI)


def test_pm_control_structured_actions(tmp_errorta_home: Path, monkeypatch):
    _team_project("ca-route")
    monkeypatch.setattr(pm_reference, "list_available_routes", lambda: list(_AVAIL))
    c = _client()
    r = c.post("/coding/projects/ca-route/pm-control", json={"actions": [
        {"type": "assign_models", "role_routes": {"dev": "sonnet"}},
        {"type": "assign_models", "role_routes": {"dev": "no-such-model"}},
    ]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["applied"]) == 1
    assert body["refusals"][0]["code"] == "model_not_found"


def test_pm_control_directive_path(tmp_errorta_home: Path, monkeypatch):
    _team_project("ca-dir")
    monkeypatch.setattr(pm_reference, "list_available_routes", lambda: list(_AVAIL))
    # mock the PM completion so no egress: it "interprets" the directive
    monkeypatch.setattr(
        "errorta_app.routes.coding._pm_complete",
        lambda store: (lambda prompt: '{"actions":[{"type":"set_autonomy",'
                       '"knobs":{"checkpoint_cadence":"off"}}]}'))
    c = _client()
    r = c.post("/coding/projects/ca-dir/pm-control",
               json={"directive": "just build it, don't ask me"})
    assert r.status_code == 200, r.text
    assert len(r.json()["applied"]) == 1
    store = LedgerStore("ca-dir")
    assert policy_to_dict(load_policy(store))["checkpoint_cadence"] == "off"


# --- F145 S5: mid-run steering / PM-initiated changes log, not pop --------
def test_pm_control_log_surface_for_pm_initiated(tmp_errorta_home: Path, monkeypatch):
    # A PM-initiated change during an accepted autonomous run is recorded with
    # surface="log" (Team-Log only), not "pop". The control plane persists to
    # run_config, so a mid-run reassignment takes effect on the next run (the
    # runner snapshots members at start).
    _team_project("ca-log")
    monkeypatch.setattr(pm_reference, "list_available_routes", lambda: list(_AVAIL))
    c = _client()
    r = c.post("/coding/projects/ca-log/pm-control", json={
        "surface": "log",
        "actions": [{"type": "assign_models", "role_routes": {"dev": "sonnet"}}]})
    assert r.status_code == 200
    assert r.json()["applied"][0]["surface"] == "log"
    # it still appears in the pending list for the Team Log
    lst = c.get("/coding/projects/ca-log/pm-changes").json()
    assert any(ch["surface"] == "log" for ch in lst["pending"])


# --- F145: create_task + start_run + robust envelope parsing ----------------- #

def test_create_task_adds_todo_and_is_reversible(tmp_errorta_home: Path):
    store = _team_project("ctask1")
    change = ca.create_task(store, title="Fix the font crash",
                            detail="renderer.py circular import", role="dev")
    tasks = [t for t in store.list_tasks() if t.state != "dropped"]
    assert any(t.title == "Fix the font crash" and t.state == "todo" for t in tasks)
    assert change.restore_target == "task"
    # Decline drops it off the board.
    pm_changes.decline(store, change.change_id)
    live = [t for t in store.list_tasks() if t.state != "dropped"]
    assert not any(t.title == "Fix the font crash" for t in live)


def test_create_task_refuses_empty_title(tmp_errorta_home: Path):
    store = _team_project("ctask2")
    with pytest.raises(ca.ControlActionError) as exc:
        ca.create_task(store, title="   ")
    assert exc.value.code == "task_title_required"


def test_apply_action_create_task_dispatch(tmp_errorta_home: Path):
    store = _team_project("ctask3")
    ch = ca.apply_action(
        store, {"type": "create_task", "title": "Add trainer AI",
                "description": "detail via the description alias"},
        available=_AVAIL)
    assert ch.restore_target == "task"
    assert any(t.title == "Add trainer AI" for t in store.list_tasks())


def test_parse_pm_reply_pure_json_create_task():
    reply, actions = ca.parse_pm_reply(
        '{"reply": "Created it.", "actions": [{"type": "create_task", "title": "X"}]}')
    assert reply == "Created it."
    assert actions == [{"type": "create_task", "title": "X"}]


def test_parse_pm_reply_prose_plus_fenced_envelope_executes():
    # The exact shape the PM produced in the field: a sentence + a ```json``` block.
    text = ('I\'ll create a task to fix it.\n\n```json\n'
            '{"reply": "Created a fix task.", '
            '"actions": [{"type": "create_task", "title": "Fix font crash"}]}\n```')
    reply, actions = ca.parse_pm_reply(text)
    assert reply == "Created a fix task."
    assert actions and actions[0]["type"] == "create_task"


def test_parse_pm_reply_rest_shaped_action_stays_chat():
    # A made-up REST-shaped action (no known `type`) must NOT execute — it stays
    # plain chat, so a hallucinated call can never run something unintended.
    text = ('Sure.\n\n```json\n{"reply": "x", "actions": '
            '[{"method": "POST", "path": "/tasks", "body": {"title": "X"}}]}\n```')
    reply, actions = ca.parse_pm_reply(text)
    assert actions == []
    assert "Sure." in reply  # the whole reply is shown as chat, nothing executed


def test_parse_pm_reply_plain_prose_stays_chat():
    reply, actions = ca.parse_pm_reply("The crash is a circular import in renderer.py.")
    assert actions == []
    assert reply.startswith("The crash")


def test_split_run_actions_separates_start_run():
    config, wants = ca.split_run_actions(
        [{"type": "create_task", "title": "X"}, {"type": "start_run"}])
    assert wants is True
    assert config == [{"type": "create_task", "title": "X"}]


def test_apply_action_start_run_is_route_only():
    store = _team_project("ctask4")
    with pytest.raises(ca.ControlActionError) as exc:
        ca.apply_action(store, {"type": "start_run"}, available=_AVAIL)
    assert exc.value.code == "start_run_route_only"
