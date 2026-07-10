"""F087-14 WS-1 — cancellation is honored between test commands."""
from __future__ import annotations

import sys
from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.testing import run_test_commands
from errorta_council.coding.workspace import CodingWorkspace


def _ws(tmp_errorta_home, pid: str):
    s = LedgerStore(pid)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    w = CodingWorkspace(pid, s)
    w.setup(target="new", repo_path=None)
    return s, w


_TRUE = {"argv": [sys.executable, "-c", "pass"], "cwd": ".", "timeout_seconds": 30}


def test_cancel_stops_before_next_command(tmp_errorta_home) -> None:
    _s, w = _ws(tmp_errorta_home, "cancel1")
    registry = {"a": _TRUE, "b": _TRUE, "c": _TRUE}
    calls = {"n": 0}

    def should_cancel() -> bool:
        # allow the first command, cancel before the second
        calls["n"] += 1
        return calls["n"] > 1

    session = run_test_commands(w.root(), registry, ["a", "b", "c"],
                                should_cancel=should_cancel)
    # ran exactly one real command, then hit the cancel before "b"
    assert session.passed is False
    assert len(session.results) == 2
    assert session.results[-1].status == "blocked"
    assert session.results[-1].reason == "cancelled before launch"


def test_no_cancel_runs_all(tmp_errorta_home) -> None:
    _s, w = _ws(tmp_errorta_home, "cancel2")
    registry = {"a": _TRUE, "b": _TRUE}
    session = run_test_commands(w.root(), registry, ["a", "b"], should_cancel=lambda: False)
    assert session.passed is True
    assert len(session.results) == 2


def test_cancel_before_first_command_runs_nothing(tmp_errorta_home) -> None:
    _s, w = _ws(tmp_errorta_home, "cancel3")
    registry = {"a": _TRUE}
    session = run_test_commands(w.root(), registry, ["a"], should_cancel=lambda: True)
    assert session.passed is False
    assert session.results[0].status == "blocked"
