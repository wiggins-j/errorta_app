"""F143-01 Slice C — cli_overhead_tokens reconciles measured input vs. our estimate.

For a CLI-backed member (provider_class endswith ``_cli``) that reported a real
total input, the vendor-managed inner context we can't see directly is inferred as
``clamp>=0(measured_input - estimated_input)`` (spec D6 / invariant 6, Layer-1
composition). A non-CLI provider never gets the field; an over-counting estimate
clamps the overhead to 0.

Uses the real ``gateway_member_caller`` seam so the estimate is computed from the
actual sent prompt. The prompt length is controlled so the estimate is small
relative to the (fabricated) measured input, making the > / < cases deterministic.
"""
import json
from pathlib import Path

import pytest

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import (
    _usage_sink,
    build_run_turn,
    gateway_member_caller,
    members_by_coding_role,
)
from errorta_council.coding.topology import Plan


@pytest.fixture(autouse=True)
def _isolate_calibration_store(tmp_errorta_home: Path) -> Path:
    """Pin the SHARED token-calibration store (under HOME/.errorta) to a tmp dir so
    these tests are hermetic — factor 1.0 by default, and their measured turns don't
    write into the dev machine's real calibration file. Without this, a real
    (claude_cli, m) factor makes the calibrated estimate diverge from the raw one and
    the overhead assertions become machine-dependent."""
    return tmp_errorta_home

MEMBERS = [
    {"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"},
     "gateway_route_id": "claude_cli.sonnet", "provider_kind": "claude_cli"},
]

_PM_ENV = json.dumps({
    "schema_version": "coding_turn.v1", "role": "pm",
    "intent": {"kind": "plan", "done": False, "tasks": []}})


class _StubResult:
    def __init__(self, *, input_tokens, provider_class):
        self.content = _PM_ENV
        self.input_tokens = input_tokens
        self.output_tokens = 20
        self.cache_read_input_tokens = None
        self.cache_write_input_tokens = None
        self.raw_usage_available = True
        self.provider_class = provider_class
        self.model = "m"


def _stub_gateway(result):
    class _G:
        async def call(self, req):
            return result

    return _G()


def _new_store(tmp_path: Path, pid: str) -> LedgerStore:
    store = LedgerStore(pid, root=tmp_path)
    store.create_project(north_star="x", definition_of_done="d",
                         target="new", repo_path=None)
    return store


def _run(tmp_path: Path, pid: str, *, input_tokens, provider_class) -> dict:
    _usage_sink.last = None
    store = _new_store(tmp_path, pid)
    caller = gateway_member_caller(_stub_gateway(
        _StubResult(input_tokens=input_tokens, provider_class=provider_class)))
    rt = build_run_turn(store, None, members_by_coding_role(MEMBERS), caller,
                        guardrail_enabled=True)
    rt(Plan(member_id="m-pm"), store)
    return store.list_turns()[0]["usage"]


def test_cli_overhead_positive_when_measured_exceeds_estimate(tmp_path: Path) -> None:
    # A large per-call measured input (summed across the turn's calls) dwarfs the
    # prompt estimate, so overhead is positive and equals the clamped difference of
    # measured minus the RAW (uncalibrated) Layer-1 estimate — persisted as
    # ``estimated_input_raw`` when calibration moved the top-line, else == estimated_input.
    usage = _run(tmp_path, "cliovpos", input_tokens=50_000,
                 provider_class="claude_cli")
    raw_basis = usage.get("estimated_input_raw", usage["estimated_input"])
    expected = max(0, usage["measured_input"] - raw_basis)
    assert usage["cli_overhead_tokens"] == expected
    assert usage["cli_overhead_tokens"] > 0


def test_cli_overhead_clamps_to_zero_when_estimate_exceeds_measured(tmp_path: Path) -> None:
    # A tiny measured input (1) below any estimate must clamp the overhead to 0,
    # never go negative.
    usage = _run(tmp_path, "cliovclamp", input_tokens=1,
                 provider_class="claude_cli")
    assert usage["estimated_input"] >= 1
    assert usage["cli_overhead_tokens"] == 0


def test_non_cli_provider_has_no_cli_overhead(tmp_path: Path) -> None:
    usage = _run(tmp_path, "cliovnone", input_tokens=50_000,
                 provider_class="anthropic")
    assert "cli_overhead_tokens" not in usage


def test_cli_overhead_measured_against_raw_not_calibrated_estimate(tmp_path: Path) -> None:
    """A converged CLI factor absorbs the vendor's hidden inner context. If overhead
    were measured against the CALIBRATED estimate it would collapse toward 0 — hiding
    the very Layer-2 band it exists to surface. It must be measured against the RAW
    (uncalibrated) Layer-1 estimate, so a large factor does NOT shrink the overhead."""
    # First measured turn on a fresh isolated store; it also WRITES a large factor
    # (measured 50k vs a small prompt estimate → clamped near the 3.0 ceiling).
    base = _run(tmp_path, "cliovraw_base", input_tokens=50_000,
                provider_class="claude_cli")
    raw_base = base.get("estimated_input_raw", base["estimated_input"])

    # Second identical turn now READS that large factor, so its calibrated top-line
    # inflates well above the raw estimate.
    cal = _run(tmp_path, "cliovraw_cal", input_tokens=50_000,
               provider_class="claude_cli")
    raw_cal = cal.get("estimated_input_raw", cal["estimated_input"])

    # Near-identical prompts (only the project id differs) → near-identical RAW
    # Layer-1 estimate; the factor grew the CALIBRATED top-line well above it (proves
    # the factor is actually applied).
    assert abs(raw_cal - raw_base) <= 10
    assert cal["estimated_input"] > raw_cal
    assert cal["calibration_factor"] > 1.0
    # The overhead did NOT collapse: measured against the RAW estimate (exact,
    # self-consistent) and essentially unchanged from the baseline — not measured minus
    # the ~3x-inflated calibrated estimate.
    assert cal["cli_overhead_tokens"] == max(0, cal["measured_input"] - raw_cal)
    assert abs(cal["cli_overhead_tokens"] - base["cli_overhead_tokens"]) <= 10
    # And it is strictly LARGER than the (wrong) calibrated-basis overhead would be —
    # i.e. calibration did not silently shrink the surfaced vendor overhead.
    assert cal["cli_overhead_tokens"] > max(0, cal["measured_input"] - cal["estimated_input"])
