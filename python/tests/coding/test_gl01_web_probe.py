"""GL01 (Item 1 + Item 3) — the unconditional default web probe (black-canvas oracle).

The committed batch grounds the loop in *does-it-run*; nothing grounds it in
*did-it-render*. A buildless web project that authored no test has an EMPTY command
registry, so ``_run_gate`` returns ``None`` and executes nothing — a 0x0 black
canvas that serves HTTP 200 with a clean console ships ``done``. These tests lock
the fix and its load-bearing property: the probe runs REGARDLESS of the registry.

The browser invocation is behind the injectable ``node_runner`` seam, so these run
with NO Playwright browser installed — the seam is scripted to return a verdict.
The launch machinery (``python -m http.server`` under the F039 sandbox) is REAL —
a short-lived child, exactly as the F146 Slice C launch suite spawns real children.
"""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding import gate_state, web_probe
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runtime import RuntimeProfileStore, detect
from errorta_council.coding.workspace import CodingWorkspace

_INDEX = ("<html><body><canvas id=c></canvas><script>"
          "const c=document.getElementById('c');c.width=300;c.height=150;"
          "const x=c.getContext('2d');x.fillStyle='#3af';x.fillRect(0,0,300,150);"
          "</script></body></html>")


def _store(pid: str, tmp_path: Path) -> LedgerStore:
    s = LedgerStore(pid, root=tmp_path / f"ledger-{pid}")
    s.create_project(north_star="n", definition_of_done="d", target="new",
                     repo_path=None)
    return s


def _ws(pid: str, store: LedgerStore) -> CodingWorkspace:
    ws = CodingWorkspace(pid, store)
    ws.setup(target="new", repo_path=None)
    return ws


def _web_project(pid: str, tmp_path: Path) -> tuple[LedgerStore, CodingWorkspace, str]:
    """A buildless web project: index.html on master + its detected static
    ``managed_local`` profile registered. Returns (store, ws, head)."""
    s = _store(pid, tmp_path)
    ws = _ws(pid, s)
    branch = ws.start_task_branch("t1")
    ws.write_file("index.html", _INDEX, task_id="t1")
    res = ws.merge_pr(branch)
    assert res.get("merged")
    rstore = RuntimeProfileStore.for_ledger(s)
    for p in detect(ws.root(), project_id=s.project_id):
        rstore.upsert_profile(p)
    return s, ws, str(res.get("head") or ws.head() or "")


def _runner(verdict: dict):
    def _fn(url, frames, *, screenshot_path="", timeout_ms=15000):
        assert url.startswith("http://127.0.0.1:")
        return dict(verdict)
    return _fn


# --------------------------------------------------------------------------- #
# Item 1 — the verdict: black frame RED, console error RED, live+clean GREEN.
# --------------------------------------------------------------------------- #
def test_black_frame_is_red(tmp_errorta_home: Path, tmp_path: Path) -> None:
    s, ws, head = _web_project("blk", tmp_path)
    run = web_probe.run_and_record(
        s, ws, head=head, frames=5,
        node_runner=_runner({"ok": False, "non_black": False,
                             "console_errors": [], "reason": "uniformly black",
                             "screenshot": ""}))
    assert run is not None and run["passed"] is False
    assert run["command_ids"] == [web_probe.PROBE_COMMAND_ID]
    # the verbatim reason rides the recorded run (surfaced to the reviewer).
    assert "black" in gate_state.latest_gate_text(s).lower()


def test_console_error_is_red(tmp_errorta_home: Path, tmp_path: Path) -> None:
    s, ws, head = _web_project("con", tmp_path)
    run = web_probe.run_and_record(
        s, ws, head=head, frames=5,
        node_runner=_runner({"ok": False, "non_black": True,
                             "console_errors": ["boom: TypeError x"],
                             "reason": "rendered content", "screenshot": ""}))
    assert run is not None and run["passed"] is False
    assert "boom: TypeError x" in gate_state.latest_gate_text(s)


def test_live_clean_is_green(tmp_errorta_home: Path, tmp_path: Path) -> None:
    s, ws, head = _web_project("grn", tmp_path)
    run = web_probe.run_and_record(
        s, ws, head=head, frames=5,
        node_runner=_runner({"ok": True, "non_black": True,
                             "console_errors": [], "reason": "rendered content",
                             "screenshot": "/x.png"}))
    assert run is not None and run["passed"] is True
    assert run["head"] == head


# --------------------------------------------------------------------------- #
# THE load-bearing property: the probe runs on an EMPTY command registry — the
# exact case `_run_gate` skips (returns None). This is the regression GL01 closes.
# --------------------------------------------------------------------------- #
def test_probe_runs_on_empty_registry(tmp_errorta_home: Path, tmp_path: Path) -> None:
    s, ws, head = _web_project("empty", tmp_path)
    assert s.get_test_commands() == {}  # no registered command — `_run_gate` -> None
    run = web_probe.run_and_record(
        s, ws, head=head, frames=3,
        node_runner=_runner({"ok": True, "non_black": True,
                             "console_errors": [], "reason": "live",
                             "screenshot": ""}))
    assert run is not None  # recorded a verdict despite the empty registry
    assert s.list_test_runs()[-1]["command_ids"] == [web_probe.PROBE_COMMAND_ID]


# --------------------------------------------------------------------------- #
# Skips + fail-open.
# --------------------------------------------------------------------------- #
def test_non_web_project_is_skipped(tmp_errorta_home: Path, tmp_path: Path) -> None:
    # No index.html, no registered profile -> no web profile -> None, no record.
    s = _store("cli", tmp_path)
    ws = _ws("cli", s)
    called = {"n": 0}

    def _never(url, frames, **kw):
        called["n"] += 1
        return {"ok": True}

    run = web_probe.run_and_record(s, ws, head="h", frames=3, node_runner=_never)
    assert run is None
    assert called["n"] == 0  # never even started a runtime / ran the probe
    assert s.list_test_runs() == []


def test_probe_raises_records_no_evidence(tmp_errorta_home: Path,
                                          tmp_path: Path) -> None:
    """The fail-open lock: a probe that raises (headless-browser inability) records
    NO evidence and does not fail the turn."""
    s, ws, head = _web_project("raise", tmp_path)

    def _boom(url, frames, **kw):
        raise RuntimeError("chromium unavailable")

    run = web_probe.run_and_record(s, ws, head=head, frames=3, node_runner=_boom)
    assert run is None
    assert s.list_test_runs() == []  # no red gate, no evidence


def test_probe_unavailable_returns_none(tmp_errorta_home: Path,
                                        tmp_path: Path) -> None:
    s, ws, head = _web_project("unavail", tmp_path)
    # node_runner returns None (playwright/chromium not installed) -> no evidence.
    run = web_probe.run_and_record(
        s, ws, head=head, frames=3, node_runner=lambda *a, **k: None)
    assert run is None
    assert s.list_test_runs() == []


# --------------------------------------------------------------------------- #
# Item 3 — the verdict rides the PR record + the reviewer prompt (gate_output).
# --------------------------------------------------------------------------- #
def test_verdict_on_pr_record_and_prompt(tmp_errorta_home: Path,
                                         tmp_path: Path) -> None:
    s, ws, head = _web_project("ev", tmp_path)
    # A PR whose head is the probed head.
    pr = s.record_pr(task_id="t1", branch="b1", head=head, dev_member="m-dev")
    # Before any probe: the reviewer's gate_output segment is ABSENT (empty), and
    # the PR record's probe fields are falsy — byte-identical to today.
    assert gate_state.latest_gate_text(s) == ""
    assert s.get_pr(pr["pr_id"])["probe_passed"] is None

    run = web_probe.run_and_record(
        s, ws, head=head, frames=3,
        node_runner=_runner({"ok": False, "non_black": False,
                             "console_errors": ["e1"], "reason": "uniformly black",
                             "screenshot": "/s.png"}))
    assert run is not None
    got = s.get_pr(pr["pr_id"])
    assert got["probe_passed"] is False
    assert got["probe_non_black"] is False
    assert got["probe_console_errors"] == 1
    assert got["probe_screenshot"] == "/s.png"
    assert got["probe_head"] == head
    # And the verdict is now present in the reviewer's gate_output block.
    assert "black" in gate_state.latest_gate_text(s).lower()


def test_verdict_not_attached_to_other_head(tmp_errorta_home: Path,
                                            tmp_path: Path) -> None:
    s, ws, head = _web_project("oh", tmp_path)
    pr = s.record_pr(task_id="t1", branch="b1", head="different-head",
                     dev_member="m-dev")
    web_probe.run_and_record(
        s, ws, head=head, frames=3,
        node_runner=_runner({"ok": True, "non_black": True,
                             "console_errors": [], "reason": "live",
                             "screenshot": ""}))
    # A PR at a DIFFERENT head is untouched (verdict bound to its own head).
    assert s.get_pr(pr["pr_id"])["probe_passed"] is None


# --------------------------------------------------------------------------- #
# has_web_profile helper.
# --------------------------------------------------------------------------- #
def test_has_web_profile(tmp_errorta_home: Path, tmp_path: Path) -> None:
    s, ws, _head = _web_project("hp", tmp_path)
    assert web_probe.has_web_profile(s) is True
    s2 = _store("hp2", tmp_path)
    _ws("hp2", s2)
    assert web_probe.has_web_profile(s2) is False


# --------------------------------------------------------------------------- #
# Integration: the runner-side arm runs the probe REGARDLESS of the registry (the
# case `_run_gate` skips), through the default node-runner seam (monkeypatched).
# --------------------------------------------------------------------------- #
def test_web_probe_arm_records_on_empty_registry(tmp_errorta_home: Path,
                                                 tmp_path: Path,
                                                 monkeypatch) -> None:
    from errorta_council.coding import runner
    s, ws, head = _web_project("arm", tmp_path)
    assert s.get_test_commands() == {}  # `_run_gate` would return None here

    def _fake_default(url, frames, *, screenshot_path="", timeout_ms=15000):
        return {"ok": False, "non_black": False, "console_errors": [],
                "reason": "uniformly black", "screenshot": ""}

    monkeypatch.setattr(web_probe, "_default_node_runner", _fake_default)
    runner._web_probe_arm(s, ws, head=head)  # the GateRun sibling arm

    last = s.list_test_runs()[-1]
    assert last["command_ids"] == [web_probe.PROBE_COMMAND_ID]
    assert last["passed"] is False
    # a green-then-this-red would trip an anchor; a first red just records evidence.


def test_web_probe_arm_disabled_by_policy(tmp_errorta_home: Path,
                                          tmp_path: Path, monkeypatch) -> None:
    from errorta_council.coding import autonomy, runner
    s, ws, head = _web_project("armoff", tmp_path)
    autonomy.save_policy(s, autonomy.CodingAutonomyPolicy(web_probe=False))
    called = {"n": 0}

    def _never(*a, **k):
        called["n"] += 1
        return {"ok": True}

    monkeypatch.setattr(web_probe, "_default_node_runner", _never)
    runner._web_probe_arm(s, ws, head=head)
    assert called["n"] == 0 and s.list_test_runs() == []
