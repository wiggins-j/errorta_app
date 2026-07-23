"""Spec 10 §4 — `errorta status` surfaces `todo: N (dispatchable: M)`.

A large N with M == 0 is the wedged-graph signature — the single line that turns
a manual backlog dig into an at-a-glance diagnosis. Additive/backward compatible:
an older server omits `backlog`, so nothing is printed.
"""
from __future__ import annotations

from errorta_cli.render.status import render_status
from errorta_cli.verbosity import Verbosity


def _payload(backlog: dict | None) -> dict:
    run: dict = {"running": False, "state": {"status": "running"}}
    if backlog is not None:
        run["backlog"] = backlog
    return {
        "project_id": "wedge-proj",
        "health": {"service": "errorta", "version": "1", "python": "3.14"},
        "run": run,
    }


def test_render_status_shows_todo_and_dispatchable_counts() -> None:
    out = " ".join(
        render_status(_payload({"todo": 130, "dispatchable": 0}), Verbosity()).split())
    assert "todo:" in out
    assert "130 (dispatchable: 0)" in out


def test_render_status_shows_a_healthy_dispatchable_count() -> None:
    out = " ".join(
        render_status(_payload({"todo": 12, "dispatchable": 4}), Verbosity()).split())
    assert "12 (dispatchable: 4)" in out


def test_render_status_omits_backlog_line_for_older_server() -> None:
    out = render_status(_payload(None), Verbosity())
    assert "dispatchable" not in out
