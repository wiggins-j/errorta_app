"""F143-01 Slice C — every non-legacy turn records honest provenance.

Drives the REAL ``gateway_member_caller`` seam (over a stub gateway) so estimation
actually runs from the sent prompt + result content. Asserts:

* a measured turn (provider reports input+output)      -> ``provenance="measured"``,
  effective == measured;
* a dark turn (provider reports nothing)               -> ``provenance="estimated"``
  with non-null estimated ints, ``measured`` false, and — crucially — the persisted
  ``usage`` block is PRESENT (not dropped as it was under old F143);
* a measured-input-only turn                           -> ``provenance="measured_partial"``
  with output filled from the estimate.

The pure ``_derive_provenance`` helper is unit-tested at the bottom for the edge
grid, since the real seam only exercises the three product-realistic shapes.
"""
import json
from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import (
    _derive_provenance,
    _usage_sink,
    build_run_turn,
    gateway_member_caller,
    members_by_coding_role,
)
from errorta_council.coding.topology import Plan

MEMBERS = [
    {"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"},
     "gateway_route_id": "local.stub", "provider_kind": "local"},
]

_PM_ENV = json.dumps({
    "schema_version": "coding_turn.v1", "role": "pm",
    "intent": {"kind": "plan", "done": False, "tasks": []}})


class _StubResult:
    """Mimics ``LocalCouncilModelResult`` for the attributes the caller reads."""

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


def _new_store(tmp_path: Path, pid: str) -> LedgerStore:
    store = LedgerStore(pid, root=tmp_path)
    store.create_project(north_star="x", definition_of_done="d",
                         target="new", repo_path=None)
    return store


def _run_one(tmp_path: Path, pid: str, result: _StubResult) -> dict:
    _usage_sink.last = None
    store = _new_store(tmp_path, pid)
    caller = gateway_member_caller(_stub_gateway(result))
    rt = build_run_turn(store, None, members_by_coding_role(MEMBERS), caller,
                        guardrail_enabled=True)
    rt(Plan(member_id="m-pm"), store)
    turns = store.list_turns()
    assert len(turns) == 1
    return turns[0]


def test_measured_turn_is_provenance_measured(tmp_path: Path) -> None:
    # NOTE: a PM plan "turn" internally makes >1 gateway call, and measured tokens
    # SUM across a turn's calls (real F143 behavior) — so we assert internal
    # consistency (effective == measured) rather than a single-call literal.
    turn = _run_one(tmp_path, "provmeas", _StubResult(
        input_tokens=200, output_tokens=40, raw_usage_available=True))
    usage = turn["usage"]
    assert usage["provenance"] == "measured"
    assert usage["measured"] is True
    # effective == measured (both are the summed value)
    assert usage["input_tokens"] == usage["measured_input"] > 0
    assert usage["output_tokens"] == usage["measured_output"] > 0
    # the estimate rode along for reconciliation/calibration
    assert usage["estimated_input"] >= 1 and usage["estimated_output"] >= 1


def test_dark_turn_is_estimated_not_unreported(tmp_path: Path) -> None:
    # A provider that reports nothing (raw_usage_available=False) must still produce
    # a usage block with estimated ints and provenance="estimated" — NOT unreported,
    # NOT a dropped block. This is the motivating fix.
    turn = _run_one(tmp_path, "provdark", _StubResult(
        input_tokens=None, output_tokens=None, raw_usage_available=False))
    usage = turn["usage"]
    assert usage is not None
    assert usage["provenance"] == "estimated"
    assert usage["measured"] is False
    assert usage["estimated_input"] >= 1 and usage["estimated_output"] >= 1
    # effective ints fall back to the estimate
    assert usage["input_tokens"] == usage["estimated_input"]
    assert usage["output_tokens"] == usage["estimated_output"]
    # no measured fields present
    assert "measured_input" not in usage and "measured_output" not in usage


def test_measured_input_only_is_partial_with_estimated_output(tmp_path: Path) -> None:
    # Provider reported input but not output (raw_usage_available=True, output None):
    # provenance="measured_partial"; effective output filled from the estimate.
    turn = _run_one(tmp_path, "provpart", _StubResult(
        input_tokens=180, output_tokens=None, raw_usage_available=True))
    usage = turn["usage"]
    assert usage["provenance"] == "measured_partial"
    assert usage["measured_input"] > 0
    assert "measured_output" not in usage
    # effective input is the measured one; effective output is the estimate
    assert usage["input_tokens"] == usage["measured_input"]
    assert usage["output_tokens"] == usage["estimated_output"]
    assert usage["estimated_output"] >= 1


# --- pure helper grid (edge cases the real seam doesn't naturally produce) -----

def test_derive_provenance_grid() -> None:
    # measured: raw + both measured present
    assert _derive_provenance(measured_input=10, measured_output=5,
                              estimated_input=9, estimated_output=6,
                              raw_usage_available=True) == "measured"
    # measured_partial: exactly one measured int present
    assert _derive_provenance(measured_input=10, measured_output=None,
                              estimated_input=9, estimated_output=6,
                              raw_usage_available=True) == "measured_partial"
    assert _derive_provenance(measured_input=None, measured_output=5,
                              estimated_input=9, estimated_output=6,
                              raw_usage_available=True) == "measured_partial"
    # estimated: no measured ints but estimates present (even if raw flag somehow set)
    assert _derive_provenance(measured_input=None, measured_output=None,
                              estimated_input=9, estimated_output=6,
                              raw_usage_available=False) == "estimated"
    # unreported: nothing at all
    assert _derive_provenance(measured_input=None, measured_output=None,
                              estimated_input=None, estimated_output=None,
                              raw_usage_available=False) == "unreported"
