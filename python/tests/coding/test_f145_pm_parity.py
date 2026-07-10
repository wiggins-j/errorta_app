"""F145 — the existing "ask PM" and "give directive" get the Wizard's access:
the reference context (knowledge) + control-actions (agency, via PM Changes)."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from errorta_council.coding import pm_reference
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.workspace import CodingWorkspace

TAURI = {"x-errorta-origin": "tauri-ui"}
_AVAIL = [
    {"route_id": "local.qwen", "family": "qwen", "provider_class": "local"},
    {"route_id": "anthropic.claude-sonnet-4.6", "family": "claude", "provider_class": "anthropic"},
]


def _client() -> TestClient:
    from errorta_app.server import app
    return TestClient(app, headers=TAURI)


def _team(pid: str) -> None:
    store = LedgerStore(pid)
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    CodingWorkspace(pid, store).setup(target="new", repo_path=None)
    store.set_run_config(room_id=None, members=[
        {"id": "pm-1", "metadata": {"coding_role": "pm"}, "model_mode": "single",
         "gateway_route_id": "local.qwen"},
        {"id": "dev-1", "metadata": {"coding_role": "dev"}, "model_mode": "single",
         "gateway_route_id": "local.qwen"},
    ])


def test_ask_pm_can_change_a_setting(tmp_errorta_home: Path, monkeypatch):
    _team("askagency")
    monkeypatch.setattr(pm_reference, "list_available_routes", lambda: list(_AVAIL))
    # the PM model replies with a JSON envelope carrying a control-action
    monkeypatch.setattr(
        "errorta_council.coding.runner.gateway_member_caller",
        lambda gw: (lambda m, prompt: '{"reply":"Done — devs on Sonnet.",'
                    '"actions":[{"type":"assign_models","role_routes":{"dev":"sonnet"}}]}'))
    c = _client()
    r = c.post("/coding/projects/askagency/pm-ask",
               json={"message": "put the devs on sonnet"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reply"]["message"] == "Done — devs on Sonnet."  # JSON not shown raw
    assert len(body["applied"]) == 1
    # the change really applied + is reviewable
    store = LedgerStore("askagency")
    dev = next(m for m in store.get_run_config()["members"]
               if m["metadata"]["coding_role"] == "dev")
    assert dev["gateway_route_id"] == "anthropic.claude-sonnet-4.6"


def test_ask_pm_plain_question_stays_chat(tmp_errorta_home: Path, monkeypatch):
    _team("askchat")
    monkeypatch.setattr(pm_reference, "list_available_routes", lambda: list(_AVAIL))
    monkeypatch.setattr(
        "errorta_council.coding.runner.gateway_member_caller",
        lambda gw: (lambda m, prompt: "You have 2 tasks left."))
    c = _client()
    r = c.post("/coding/projects/askchat/pm-ask", json={"message": "how's it going?"})
    assert r.status_code == 200
    body = r.json()
    assert body["reply"]["message"] == "You have 2 tasks left."
    assert body["applied"] == []  # plain chat changes nothing


def test_give_directive_can_change_a_setting(tmp_errorta_home: Path, monkeypatch):
    _team("diragency")
    monkeypatch.setattr(pm_reference, "list_available_routes", lambda: list(_AVAIL))
    monkeypatch.setattr("errorta_app.routes.coding._pm_reply_for_message",
                        lambda store, msg: "Understood.")
    monkeypatch.setattr(
        "errorta_app.routes.coding._pm_complete",
        lambda store: (lambda prompt: '{"actions":[{"type":"set_autonomy",'
                       '"knobs":{"checkpoint_cadence":"off"}}]}'))
    c = _client()
    r = c.post("/coding/projects/diragency/interject",
               json={"message": "go autonomous, don't ask me"})
    assert r.status_code == 200, r.text
    assert len(r.json()["applied"]) == 1
    from errorta_council.coding.autonomy import load_policy, policy_to_dict
    assert policy_to_dict(load_policy(LedgerStore("diragency")))["checkpoint_cadence"] == "off"
    # the authoritative run directive is still recorded (steering preserved)
    assert r.json()["interjection"]


def test_prose_reply_quoting_the_schema_stays_chat(tmp_errorta_home: Path):
    # A prose answer that merely QUOTES the {reply,actions} schema must not be
    # treated as an envelope, must not be hidden, and must apply nothing.
    from errorta_council.coding import control_actions as ca
    prose = ('You can change settings by sending me a JSON object like '
             '{"reply": "ok", "actions": [{"type": "set_autonomy"}]}. '
             'But right now everything looks fine.')
    reply, actions = ca.parse_pm_reply(prose)
    assert reply == prose  # not mangled / hidden
    assert actions == []   # example not executed


def test_whole_json_reply_is_an_envelope(tmp_errorta_home: Path):
    from errorta_council.coding import control_actions as ca
    reply, actions = ca.parse_pm_reply(
        '{"reply": "Done.", "actions": [{"type": "set_governance", "fields": {}}]}')
    assert reply == "Done."
    assert len(actions) == 1


def test_ask_pm_prose_quoting_schema_applies_nothing(tmp_errorta_home: Path, monkeypatch):
    _team("askprose")
    monkeypatch.setattr(pm_reference, "list_available_routes", lambda: list(_AVAIL))
    prose = ('Sure — you\'d send {"reply":"x","actions":[{"type":"assign_models",'
             '"role_routes":{"dev":"sonnet"}}]} to do that. Want me to?')
    monkeypatch.setattr(
        "errorta_council.coding.runner.gateway_member_caller",
        lambda gw: (lambda m, prompt: prose))
    c = _client()
    r = c.post("/coding/projects/askprose/pm-ask", json={"message": "how do I change models?"})
    assert r.status_code == 200
    assert r.json()["reply"]["message"] == prose  # shown verbatim
    assert r.json()["applied"] == []              # nothing applied
    # dev route unchanged
    dev = next(m for m in LedgerStore("askprose").get_run_config()["members"]
               if m["metadata"]["coding_role"] == "dev")
    assert dev["gateway_route_id"] == "local.qwen"


def test_ask_pm_can_create_a_task(tmp_errorta_home: Path, monkeypatch):
    # The reported scenario: user says "fix it", the PM replies with a sentence +
    # a ```json``` create_task envelope. It must ACTUALLY create the task (not show
    # the JSON raw and do nothing).
    _team("askmaketask")
    monkeypatch.setattr(pm_reference, "list_available_routes", lambda: list(_AVAIL))
    monkeypatch.setattr(
        "errorta_council.coding.runner.gateway_member_caller",
        lambda gw: (lambda m, prompt: (
            "I'll create a task to fix the crash.\n\n```json\n"
            '{"reply": "Created a fix task.", "actions": [{"type": "create_task", '
            '"title": "Fix pygame.font crash", "detail": "renderer.py", "role": "dev"}]}'
            "\n```")))
    c = _client()
    r = c.post("/coding/projects/askmaketask/pm-ask", json={"message": "yes fix it"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reply"]["message"] == "Created a fix task."  # not the raw JSON
    assert len(body["applied"]) == 1
    # the task really landed on the board as a todo
    store = LedgerStore("askmaketask")
    assert any(t.title == "Fix pygame.font crash" and t.state == "todo"
               for t in store.list_tasks())
