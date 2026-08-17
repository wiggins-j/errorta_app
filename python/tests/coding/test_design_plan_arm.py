"""Slice 1 §2/§4 — end-to-end coverage of the runner's DesignPlan dispatch arm.

The arm's own import bug (a local re-import of parse_coding_turn/TurnParseError that
made them locals across the whole _execute body) was invisible to the design unit
tests and only surfaced via a canary — the untested-path lesson. These drive a real
turn through ``build_run_turn`` with a stub caller.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from errorta_council.coding.governance import GovernanceStore
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import build_run_turn, members_by_coding_role
from errorta_council.coding.topology import DesignPlan, Plan
from errorta_council.coding.workspace import CodingWorkspace

MEMBERS = [
    {"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"}},
    {"id": "m-dev", "enabled": True, "metadata": {"coding_role": "dev"}},
    {"id": "m-designer", "enabled": True, "metadata": {"coding_role": "designer"}},
]


def _store(pid: str) -> LedgerStore:
    store = LedgerStore(pid)
    store.create_project(north_star="A todo app", definition_of_done="opens in a browser",
                         target="new", repo_path=None)
    return store


def _ws(pid: str, store: LedgerStore) -> CodingWorkspace:
    ws = CodingWorkspace(pid, store)
    ws.setup(target="new", repo_path=None)
    return ws


def _valid_body() -> dict:
    return {
        "direction_matrix": {"typography": "humanist sans", "color": "warm",
                             "density": "comfortable", "shape": "rounded",
                             "motion": "subtle", "era_mood": "modern"},
        "tokens": {"palette": {"bg": "#fff"}},
        "assets": {"font_family_ids": ["inter"], "icon_set_id": "lucide"},
        "screens": [{"screen": "home", "purpose": "p", "layout": "l",
                     "hierarchy": "h", "key_states": ["default"]}],
        "components": [{"name": "button", "usage": "u"}],
    }


def _design_env(body: dict) -> str:
    return json.dumps({
        "schema_version": "coding_turn.v1", "role": "designer",
        "intent": {"kind": "design_spec", "title": "Design contract",
                   "body_markdown": "warm editorial", "body_json": body},
    })


def _run_turn(store: LedgerStore, ws: CodingWorkspace, caller):
    rt = build_run_turn(store, ws, members_by_coding_role(MEMBERS), caller,
                        guardrail_enabled=True)
    return rt


def test_valid_design_turn_approves_and_materializes(tmp_errorta_home: Path) -> None:
    store = _store("dp-arm-ok")
    ws = _ws("dp-arm-ok", store)

    def caller(member: dict[str, Any], prompt: str) -> str:
        return _design_env(_valid_body())

    _run_turn(store, ws, caller)(DesignPlan(member_id="m-designer"), store)

    gov = GovernanceStore.for_ledger(store)
    art = gov.latest_artifact("design_spec")
    assert art is not None and art.state == "approved"
    materialize = [t for t in store.list_tasks()
                   if t.title.strip().lower() == "materialize design system"]
    assert len(materialize) == 1 and materialize[0].role == "dev"


def test_invalid_body_recovers_in_turn(tmp_errorta_home: Path) -> None:
    """A parseable but semantically-invalid body (missing an axis) is re-prompted
    in-turn; when the retry returns a valid body it is approved (no UI-blocking
    dead-end)."""
    store = _store("dp-arm-recover")
    ws = _ws("dp-arm-recover", store)
    bad = _valid_body()
    del bad["direction_matrix"]["motion"]
    calls = {"n": 0}

    def caller(member: dict[str, Any], prompt: str) -> str:
        calls["n"] += 1
        return _design_env(bad if calls["n"] == 1 else _valid_body())

    _run_turn(store, ws, caller)(DesignPlan(member_id="m-designer"), store)
    assert calls["n"] == 2, "the invalid body must trigger one corrective re-prompt"
    art = GovernanceStore.for_ledger(store).latest_artifact("design_spec")
    assert art is not None and art.state == "approved"


def test_persistently_invalid_body_bounces_to_changes_requested(
        tmp_errorta_home: Path) -> None:
    store = _store("dp-arm-bad")
    ws = _ws("dp-arm-bad", store)
    bad = _valid_body()
    del bad["tokens"]  # missing a required section every time

    def caller(member: dict[str, Any], prompt: str) -> str:
        return _design_env(bad)

    _run_turn(store, ws, caller)(DesignPlan(member_id="m-designer"), store)
    gov = GovernanceStore.for_ledger(store)
    art = gov.latest_artifact("design_spec")
    assert art is not None and art.state == "changes_requested"
    assert not [t for t in store.list_tasks()
                if t.title.strip().lower() == "materialize design system"]


def test_non_design_turn_still_dispatches(tmp_errorta_home: Path) -> None:
    """Regression guard for the local-import scoping bug: a non-DesignPlan action
    (a PM Plan turn) must dispatch without an UnboundLocalError from the shared
    _execute body."""
    store = _store("dp-arm-plan")
    ws = _ws("dp-arm-plan", store)

    def caller(member: dict[str, Any], prompt: str) -> str:
        return json.dumps({"schema_version": "coding_turn.v1", "role": "pm",
                           "intent": {"kind": "plan", "done": False,
                                      "tasks": [{"title": "Add the backend",
                                                 "role": "dev",
                                                 "detail": "the service"}]}})

    outcome = _run_turn(store, ws, caller)(Plan(member_id="m-pm"), store)
    assert outcome is not None  # did not raise
    assert any(t.title == "Add the backend" for t in store.list_tasks())
