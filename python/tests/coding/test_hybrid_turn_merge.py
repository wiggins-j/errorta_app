"""F143-01 Slice D (Part 1) — hybrid multi-call turn merge.

A single coding-team turn can make several gateway calls (a parse-retry). When those
calls MIX a measured provider call with a dark call, the merge must:

* keep BOTH calls' spend in the effective headline (the dark call's estimate is not
  dropped), and
* report ``provenance="measured_partial"`` — NEVER over-claim ``measured`` — because
  not every call in the turn was measured, and ``measured_input`` < effective_input.

Two layers of coverage:
1. a direct unit test of the ``_merge_call_usage`` accumulator (fully deterministic);
2. the REAL ``gateway_member_caller`` seam driven so a turn makes a measured call
   then a dark call (first response is malformed → forces one corrective retry).
"""
import json
from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import (
    _derive_provenance,
    _merge_call_usage,
    _usage_sink,
    build_run_turn,
    gateway_member_caller,
    members_by_coding_role,
)
from errorta_council.coding.topology import Plan

_PM_ENV = json.dumps({
    "schema_version": "coding_turn.v1", "role": "pm",
    "intent": {"kind": "plan", "done": False, "tasks": []}})


# --- Layer 1: the accumulator directly ----------------------------------------

def test_merge_hybrid_measured_plus_dark_call() -> None:
    measured_call = {
        "measured": True, "input_tokens": 1000, "output_tokens": 200,
        "estimated_input": 120, "estimated_output": 30,
        "provider_class": "claude_cli", "model": "sonnet",
    }
    dark_call = {
        "measured": False, "input_tokens": None, "output_tokens": None,
        "estimated_input": 400, "estimated_output": 90,
        "provider_class": "claude_cli", "model": "sonnet",
    }
    acc = _merge_call_usage(None, measured_call)
    acc = _merge_call_usage(acc, dark_call)

    assert acc["total_calls"] == 2 and acc["measured_calls"] == 1
    # Measured-only sums cover ONLY the measured call.
    assert acc["measured_input"] == 1000 and acc["measured_output"] == 200
    # Estimated sums span ALL calls.
    assert acc["estimated_input"] == 120 + 400 and acc["estimated_output"] == 30 + 90
    # Effective = measured value where measured, else the estimate — per call:
    #   input:  1000 (measured) + 400 (dark's estimate) = 1400
    #   output:  200 (measured) +  90 (dark's estimate) =  290
    assert acc["effective_input"] == 1400 and acc["effective_output"] == 290

    # Provenance from these counts: partial, NOT measured (a dark call is present).
    prov = _derive_provenance(
        measured_input=acc["measured_input"], measured_output=acc["measured_output"],
        estimated_input=acc["estimated_input"], estimated_output=acc["estimated_output"],
        raw_usage_available=True,
        measured_calls=acc["measured_calls"], total_calls=acc["total_calls"])
    assert prov == "measured_partial"
    # measured_input strictly less than the effective input (dark spend included).
    assert acc["measured_input"] < acc["effective_input"]


def test_merge_all_measured_calls_is_measured() -> None:
    call = {"measured": True, "input_tokens": 50, "output_tokens": 10,
            "estimated_input": 40, "estimated_output": 8,
            "provider_class": "anthropic", "model": "m"}
    acc = _merge_call_usage(None, call)
    acc = _merge_call_usage(acc, dict(call))
    assert acc["measured_calls"] == acc["total_calls"] == 2
    assert acc["effective_input"] == 100 and acc["measured_input"] == 100
    prov = _derive_provenance(
        measured_input=acc["measured_input"], measured_output=acc["measured_output"],
        estimated_input=acc["estimated_input"], estimated_output=acc["estimated_output"],
        raw_usage_available=True,
        measured_calls=acc["measured_calls"], total_calls=acc["total_calls"])
    assert prov == "measured"


# --- Layer 2: the real seam ----------------------------------------------------

class _Result:
    def __init__(self, *, content, input_tokens, output_tokens, raw):
        self.content = content
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = None
        self.cache_write_input_tokens = None
        self.raw_usage_available = raw
        self.provider_class = "claude_cli"
        self.model = "sonnet"


class _SequencedGateway:
    """Returns a scripted sequence of results, one per ``call`` — the first a
    malformed (measured) response that forces a corrective retry, the second a valid
    (dark) PM env."""

    def __init__(self, results):
        self._results = list(results)
        self._i = 0

    async def call(self, req):
        r = self._results[min(self._i, len(self._results) - 1)]
        self._i += 1
        return r


MEMBERS = [
    {"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"},
     "gateway_route_id": "claude_cli.sonnet", "provider_kind": "claude_cli"},
]


def test_real_seam_hybrid_turn_is_partial_not_measured(tmp_path: Path) -> None:
    _usage_sink.last = None
    store = LedgerStore("hybridseam", root=tmp_path)
    store.create_project(north_star="x", definition_of_done="d",
                         target="new", repo_path=None)
    gateway = _SequencedGateway([
        # Call 1: malformed content (not a coding-turn env) -> triggers a retry,
        # WITH measured usage.
        _Result(content="not json at all", input_tokens=5000, output_tokens=800,
                raw=True),
        # Call 2: valid PM env, but DARK (provider reported nothing).
        _Result(content=_PM_ENV, input_tokens=None, output_tokens=None, raw=False),
    ])
    caller = gateway_member_caller(gateway)
    rt = build_run_turn(store, None, members_by_coding_role(MEMBERS), caller,
                        guardrail_enabled=True)
    rt(Plan(member_id="m-pm"), store)

    turns = store.list_turns()
    assert len(turns) == 1
    usage = turns[0]["usage"]
    # A dark call is present, so the turn is partial — never over-claimed measured.
    assert usage["provenance"] == "measured_partial"
    # The measured call's spend is present AND strictly below the effective total
    # (the dark call's estimated spend was NOT dropped).
    assert usage["measured_input"] == 5000
    assert usage["input_tokens"] > usage["measured_input"]
    assert usage["output_tokens"] > usage["measured_output"]
