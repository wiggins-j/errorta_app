from __future__ import annotations

from pathlib import Path

import pytest

from errorta_council.coding import next_goal
from errorta_council.coding.ledger import LedgerStore


@pytest.fixture(autouse=True)
def _isolated_errorta_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


def _project(project_id: str) -> LedgerStore:
    ledger = LedgerStore(project_id)
    ledger.create_project(
        north_star="A stale north star from ten days ago.",
        definition_of_done="Whatever done meant back then.",
        target="existing", repo_path=None, delivery_root=None,
    )
    return ledger


def test_start_gate_refuses_when_there_is_no_goal() -> None:
    """The abovo case (spec §1): zero active focuses, empty work_request, and
    a ten-day-stale north star. Starting spends real model budget
    re-litigating finished work, so refuse and name the remedy."""
    ledger = _project("gate-none")

    reason = next_goal.start_gate(ledger)

    assert reason is not None
    assert "goal" in reason.lower()


def test_start_gate_allows_an_active_focus() -> None:
    ledger = _project("gate-focus")
    ledger.add_focus(title="Build the tick engine", origin="slack_pm")

    assert next_goal.start_gate(ledger) is None


def test_start_gate_allows_a_legacy_work_request() -> None:
    """F137 migrated the single work_request string into the focus ledger, and
    runner._pm_prompt still falls back to it (runner.py:3170-3178). A project
    steered only by the legacy field must not be blocked from starting."""
    ledger = _project("gate-legacy")
    ledger.set_work_request("Finish the transcript golden")

    assert next_goal.start_gate(ledger) is None


def test_start_gate_never_raises_on_an_unreadable_store() -> None:
    """The gate runs on every start path including autopilot's. A broken
    ledger must not turn into an uncaught raise mid-turn — fail OPEN (allow
    the start) rather than wedging every project behind a read error."""
    class Broken:
        def active_focuses(self):
            raise RuntimeError("focus ledger unreadable")

        def get_project(self):
            raise RuntimeError("project unreadable")

    assert next_goal.start_gate(Broken()) is None
