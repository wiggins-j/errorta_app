"""Spec 18 Phase 0 — `_TERMINAL_BAD` backfill.

Specs 04/07/10 added the `gate_not_improving`, `planning_churn` and
`dispatch_wedged` stop reasons without adding them to `_TERMINAL_BAD`, whose sole
consumer is the bound-status stop-reason styling. Each must now render as a
genuine failure (`cli.bad` → red) rather than a clean/muted finish; the existing
entries and clean finishes are unchanged.
"""
from __future__ import annotations

import errorta_cli.render as _render
from errorta_cli.render.status import _TERMINAL_BAD, render_status
from errorta_cli.verbosity import Verbosity

_RED = "\x1b[31m"  # cli.bad
_DIM = "\x1b[2m"   # cli.muted


def _payload(stop_reason: str) -> dict:
    return {
        "project_id": "p",
        "health": {"service": "errorta", "version": "1", "python": "3.14"},
        "run": {"running": False, "state": {"status": "failed", "stop_reason": stop_reason}},
    }


def _stop_line(monkeypatch, stop_reason: str) -> str:
    monkeypatch.setattr(_render, "_color_enabled", lambda: True)
    out = render_status(_payload(stop_reason), Verbosity())
    return next(ln for ln in out.splitlines() if "stop:" in ln)


def test_backfilled_reasons_are_in_the_set() -> None:
    assert {"gate_not_improving", "planning_churn", "dispatch_wedged"} <= _TERMINAL_BAD


def test_backfilled_reasons_render_in_failure_style(monkeypatch) -> None:
    for reason in ("gate_not_improving", "planning_churn", "dispatch_wedged"):
        line = _stop_line(monkeypatch, reason)
        assert _RED in line, reason
        assert _DIM not in line, reason


def test_clean_finish_stays_muted(monkeypatch) -> None:
    # A clean checkpoint reason not in _TERMINAL_BAD renders muted, not as failure.
    line = _stop_line(monkeypatch, "definition_of_done")
    assert _DIM in line
    assert _RED not in line


def test_prior_terminal_bad_entries_unchanged(monkeypatch) -> None:
    # A pre-existing failure reason keeps its failure style (regression guard).
    line = _stop_line(monkeypatch, "budget_exhausted")
    assert _RED in line
