"""F143-01 Slice C — a measured turn feeds the shared calibration store; a dark
turn does not.

The coding runner reuses Council's ``TokenCalibrationStore`` (shared path under
``${ERRORTA_HOME}``) so ``(provider,model)`` factors accumulate across runs. When a
provider reports a real input, the caller nudges the factor via the store's EMA;
when it reports nothing, no ratio exists so the factor is left untouched.

We pin ``ERRORTA_HOME`` to a tmp dir so the store writes there, then read the factor
straight back through ``TokenCalibrationStore`` to prove the update landed (or
didn't).
"""
import json
from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import (
    _usage_sink,
    build_run_turn,
    gateway_member_caller,
    members_by_coding_role,
)
from errorta_council.coding.topology import Plan
from errorta_council.context.tokens import CalibrationSample, TokenCalibrationStore
from errorta_council.paths import token_calibration_path

MEMBERS = [
    {"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"},
     "gateway_route_id": "claude_cli.sonnet", "provider_kind": "claude_cli"},
]

_PM_ENV = json.dumps({
    "schema_version": "coding_turn.v1", "role": "pm",
    "intent": {"kind": "plan", "done": False, "tasks": []}})


class _StubResult:
    def __init__(self, *, input_tokens, raw_usage_available,
                 provider_class="claude_cli", model="sonnet"):
        self.content = _PM_ENV
        self.input_tokens = input_tokens
        self.output_tokens = 10 if raw_usage_available else None
        self.cache_read_input_tokens = None
        self.cache_write_input_tokens = None
        self.raw_usage_available = raw_usage_available
        self.provider_class = provider_class
        self.model = model


def _stub_gateway(result):
    class _G:
        async def call(self, req):
            return result

    return _G()


def _run(tmp_path: Path, pid: str, result: _StubResult) -> dict:
    _usage_sink.last = None
    store = LedgerStore(pid, root=tmp_path)
    store.create_project(north_star="x", definition_of_done="d",
                         target="new", repo_path=None)
    caller = gateway_member_caller(_stub_gateway(result))
    rt = build_run_turn(store, None, members_by_coding_role(MEMBERS), caller,
                        guardrail_enabled=True)
    rt(Plan(member_id="m-pm"), store)
    turns = store.list_turns()
    assert len(turns) == 1
    return turns[0]


def test_measured_turn_nudges_calibration_factor(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    cal = TokenCalibrationStore(token_calibration_path())
    assert cal.read_factor("claude_cli", "sonnet") == 1.0  # default before any turn

    # A large measured input vs. a small prompt estimate produces a ratio well above
    # 1.0 (clamped into [0.7, 3.0]), so the stored factor moves off the default.
    _run(tmp_path, "calmeas",
         _StubResult(input_tokens=50_000, raw_usage_available=True))

    updated = cal.read_factor("claude_cli", "sonnet")
    assert updated != 1.0
    assert updated > 1.0


def test_dark_turn_does_not_touch_calibration(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    cal = TokenCalibrationStore(token_calibration_path())

    _run(tmp_path, "caldark",
         _StubResult(input_tokens=None, raw_usage_available=False))

    # No provider input reported -> no ratio -> factor stays at the default.
    assert cal.read_factor("claude_cli", "sonnet") == 1.0
    assert cal.read_all() == {}


def test_stored_factor_actually_changes_a_later_estimate(tmp_path, monkeypatch) -> None:
    """The point of calibration: a stored (provider,model) factor is READ BACK and
    scales a subsequent turn's estimate. This is the regression that guards the
    'calibration loop is inert / calibration_factor is always 1.0' defect — an
    estimate must move by ~the factor, and the persisted field must report it."""
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))

    # Baseline: no stored factor -> factor 1.0 -> raw estimate. Use a DARK turn so the
    # persisted estimated_input is purely our own estimate (no measured value replaces
    # it) and no calibration write happens to perturb the second run.
    dark = _StubResult(input_tokens=None, raw_usage_available=False)
    raw_turn = _run(tmp_path, "calbase", dark)
    raw_est = raw_turn["usage"]["estimated_input"]
    assert raw_est > 0
    assert raw_turn["usage"]["calibration_factor"] == 1.0  # honest default, not absent

    # Seed a 2.0 factor for exactly this (provider, model), then run the SAME dark turn.
    cal = TokenCalibrationStore(token_calibration_path())
    cal.record(CalibrationSample(provider="claude_cli", model="sonnet", ratio=2.0))
    assert cal.read_factor("claude_cli", "sonnet") == 2.0

    cal_turn = _run(tmp_path, "calapplied", dark)
    cal_est = cal_turn["usage"]["estimated_input"]

    # The estimate moved by ~2x (per-call ceil rounding keeps it in a tight band), and
    # the persisted factor reports the live 2.0 rather than a hardcoded 1.0.
    assert cal_est > raw_est
    assert 1.9 * raw_est <= cal_est <= 2.1 * raw_est
    assert cal_turn["usage"]["calibration_factor"] == 2.0
    # A wrong (mismatched) provider/model key must NOT pick up the factor.
    assert cal.read_factor("claude_cli", "opus") == 1.0
