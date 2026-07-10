"""F143-01 Slice F — per-turn Layer-1 composition is computed, persisted, served.

Drives the REAL ``gateway_member_caller`` seam (over a stub gateway) so the segmented
prompt builder runs, its composition is computed from the sent segments, and the
record path persists it. Asserts:

* a PM turn records a ``composition`` block whose category tokens sum to
  ``sent_total`` (invariant: acceptance criterion 11);
* ``estimated_input`` is UPGRADED to the composition ``sent_total`` (Slice C's whole-
  string estimate is replaced by the per-segment categorized sum);
* the ``.../composition`` endpoint returns the REAL categories + a Layer-2 CLI caveat
  note for a CLI-backed member and no note for a direct-API member.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import HTTPException

from errorta_app.routes.coding import get_turn_composition
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import (
    _usage_sink,
    build_run_turn,
    gateway_member_caller,
    members_by_coding_role,
)
from errorta_council.coding.topology import Plan

_PM_ENV = json.dumps({
    "schema_version": "coding_turn.v1", "role": "pm",
    "intent": {"kind": "plan", "done": False, "tasks": []}})


class _StubResult:
    def __init__(self, *, input_tokens, output_tokens, raw_usage_available,
                 provider_class="local", model="stub"):
        self.content = _PM_ENV
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = None
        self.cache_write_input_tokens = None
        self.raw_usage_available = raw_usage_available
        self.provider_class = provider_class
        self.model = model


def _stub_gateway(result: _StubResult):
    class _G:
        async def call(self, req):
            return result

    return _G()


def _members(route_id: str, provider_kind: str):
    return [{"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"},
             "gateway_route_id": route_id, "provider_kind": provider_kind}]


def _new_store(pid: str) -> LedgerStore:
    store = LedgerStore(pid)
    store.create_project(north_star="build a game", definition_of_done="d",
                         target="new", repo_path=None)
    return store


def _run_pm(pid: str, result: _StubResult, *,
            route_id: str, provider_kind: str) -> tuple[LedgerStore, dict]:
    _usage_sink.last = None
    store = _new_store(pid)
    caller = gateway_member_caller(_stub_gateway(result))
    rt = build_run_turn(store, None, members_by_coding_role(
        _members(route_id, provider_kind)), caller, guardrail_enabled=True)
    rt(Plan(member_id="m-pm"), store)
    turns = store.list_turns()
    assert len(turns) == 1
    return store, turns[0]


def test_pm_turn_records_composition_summing_to_sent_total(
        tmp_errorta_home: Path) -> None:
    _store, turn = _run_pm(
        "compsum",
        _StubResult(input_tokens=None, output_tokens=None, raw_usage_available=False),
        route_id="local.stub", provider_kind="local")
    comp = turn.get("composition")
    assert isinstance(comp, dict)
    cats = comp["categories"]
    assert cats, "PM composition should have at least one category"
    # categories sum to sent_total (acceptance criterion 11)
    assert comp["sent_total"] == sum(c["tokens"] for c in cats) > 0
    # every category carries a taxonomy class + positive int tokens
    for c in cats:
        assert isinstance(c["class"], str) and c["class"]
        assert isinstance(c["tokens"], int) and c["tokens"] > 0
    # estimated_input is the per-turn SUM of every call's estimate (a PM plan turn
    # can make >1 gateway call); the composition's categorized sum is the FIRST
    # (segmented) call's input and is thus a component of — and never exceeds — it.
    # This is the Slice F upgrade: the first call's whole-string estimate is replaced
    # by the per-segment categorized sum.
    assert turn["usage"]["estimated_input"] >= comp["sent_total"]


_COMPOSITION = {
    "sent_total": 4200,
    "categories": [
        {"class": "role_instructions", "tokens": 380},
        {"class": "work_request", "tokens": 1200},
        {"class": "project_context", "tokens": 900},
        {"class": "repo_snapshot", "tokens": 1400},
        {"class": "prior_outputs", "tokens": 250},
        {"class": "pr_diff", "tokens": 70},
    ],
    "estimator_method": "calibrated_heuristic_v1",
}


def test_composition_endpoint_returns_real_categories_direct_api(
        tmp_errorta_home: Path) -> None:
    store = _new_store("compapi")
    task = store.add_task(title="do it", role="dev")
    rec = store.record_turn(
        role="dev", member_id="m-dev", task_id=task.task_id,
        prompt="p", response="r", outcome="applied",
        route_id="anthropic.sonnet",
        input_tokens=4200, output_tokens=200,
        measured=True, measured_input=4200, measured_output=200,
        provenance="measured", composition=_COMPOSITION)
    out = get_turn_composition(store.project_id, task.task_id, rec["turn_id"])
    cats = out["composition"]["categories"]
    assert cats, "endpoint must return real categories"
    assert {c["class"] for c in cats} == {c["class"] for c in _COMPOSITION["categories"]}
    # sent_total recomputed as the sum of retained categories
    assert out["composition"]["sent_total"] == sum(c["tokens"] for c in cats) == 4200
    # direct-API member: no CLI Layer-2 caveat
    assert out["note"] is None
    assert out["cli_overhead_tokens"] is None


def test_composition_endpoint_cli_member_has_layer2_note(
        tmp_errorta_home: Path) -> None:
    store = _new_store("compcli")
    task = store.add_task(title="do it", role="dev")
    rec = store.record_turn(
        role="dev", member_id="m-dev", task_id=task.task_id,
        prompt="p", response="r", outcome="applied",
        route_id="claude_cli.sonnet",
        input_tokens=8000, output_tokens=200,
        measured=True, measured_input=8000, measured_output=200,
        estimated_input=4200, cli_overhead_tokens=3800,
        provenance="measured", composition=_COMPOSITION)
    out = get_turn_composition(store.project_id, task.task_id, rec["turn_id"])
    assert out["composition"]["categories"]
    # CLI member: a Layer-2 caveat naming the route + the vendor-managed overhead.
    assert out["note"] and "claude_cli.sonnet" in out["note"]
    assert out["cli_overhead_tokens"] == 3800
    assert "3800" in out["note"]


def test_composition_endpoint_cli_member_note_without_overhead(
        tmp_errorta_home: Path) -> None:
    # A CLI member with no measured input (no overhead magnitude) STILL gets the
    # Layer-2 caveat — keyed on the route being CLI, not on the overhead number.
    store = _new_store("compclidark")
    task = store.add_task(title="do it", role="dev")
    rec = store.record_turn(
        role="dev", member_id="m-dev", task_id=task.task_id,
        prompt="p", response="r", outcome="applied",
        route_id="cursor_cli.composer-2.5",
        input_tokens=4200, output_tokens=180,
        measured=False, estimated_input=4200, estimated_output=180,
        provenance="estimated", composition=_COMPOSITION)
    out = get_turn_composition(store.project_id, task.task_id, rec["turn_id"])
    assert out["cli_overhead_tokens"] is None
    assert out["note"] and "cursor_cli.composer-2.5" in out["note"]


def test_composition_endpoint_404_unknown_turn(tmp_errorta_home: Path) -> None:
    store = _new_store("comp404")
    task = store.add_task(title="t", role="dev")
    try:
        get_turn_composition(store.project_id, task.task_id, "trn-nope")
    except HTTPException as exc:
        assert exc.status_code == 404
    else:  # pragma: no cover
        raise AssertionError("expected 404")
