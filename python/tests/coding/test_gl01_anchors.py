"""GL01 (Item 2) — test anchors: the mechanical anti-oscillation lock.

Once a probe/command goes green on the integrated head it becomes an anchor; a
later head that flips it red is a regression → an ``anchor_regressed`` decision
(the GL04 signal) + ONE deduped non-blocking alert. Deliberately satisfiable
(re-green clears it) and master-scoped: it NEVER touches ``_set_mergeable_if_ready``,
so a partial-branch merge is never wedged.
"""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding import anchors, attention
from errorta_council.coding.ledger import LedgerStore

_KEY = "web:probe"


def _store(pid: str, tmp_path: Path) -> LedgerStore:
    s = LedgerStore(pid, root=tmp_path / f"ledger-{pid}")
    s.create_project(north_star="n", definition_of_done="d", target="new",
                     repo_path=None)
    return s


def _run(head: str, *, passed: bool, key: str = _KEY) -> dict:
    return {"head": head, "results": [{"command_id": key, "passed": passed}]}


def _alerts(pid: str, store: LedgerStore) -> list:
    return [s for s in attention.list_open(pid, store=store)
            if s.source == "anchor_regressed"]


def _decisions(store: LedgerStore) -> list:
    return [d for d in store.list_decisions() if d.get("choice") == "anchor_regressed"]


# --------------------------------------------------------------------------- #
# green-then-red -> anchor_regressed + exactly one alert.
# --------------------------------------------------------------------------- #
def test_green_then_red_regresses(tmp_errorta_home: Path, tmp_path: Path) -> None:
    s = _store("a1", tmp_path)
    anchors.reconcile(s, _run("H1", passed=True), project_id="a1")   # promote
    breaks = anchors.reconcile(s, _run("H2", passed=False), project_id="a1")  # break
    assert [b["key"] for b in breaks] == [_KEY]
    assert breaks[0]["anchor_head"] == "H1" and breaks[0]["broken_head"] == "H2"
    assert len(_decisions(s)) == 1
    assert len(_alerts("a1", s)) == 1


def test_never_green_never_breaks(tmp_errorta_home: Path, tmp_path: Path) -> None:
    s = _store("a2", tmp_path)
    # A red result with NO prior anchor: nothing was ever green, so nothing regresses.
    breaks = anchors.reconcile(s, _run("H1", passed=False), project_id="a2")
    assert breaks == []
    assert _decisions(s) == [] and _alerts("a2", s) == []
    assert anchors.broken_anchors(s, _run("H2", passed=False)) == []


def test_still_green_reaffirms_quietly(tmp_errorta_home: Path, tmp_path: Path) -> None:
    s = _store("a3", tmp_path)
    anchors.reconcile(s, _run("H1", passed=True), project_id="a3")
    anchors.reconcile(s, _run("H2", passed=True), project_id="a3")  # still green
    assert _decisions(s) == [] and _alerts("a3", s) == []
    # the anchor head advanced to the newest green head.
    assert s.get_run_state()["test_anchors"][_KEY]["head"] == "H2"


def test_red_at_same_head_is_not_a_break(tmp_errorta_home: Path,
                                        tmp_path: Path) -> None:
    s = _store("a3b", tmp_path)
    anchors.reconcile(s, _run("H1", passed=True), project_id="a3b")
    # A red on the SAME head is a flap on one tree, not a cross-head oscillation.
    breaks = anchors.reconcile(s, _run("H1", passed=False), project_id="a3b")
    assert breaks == []
    assert _alerts("a3b", s) == []


# --------------------------------------------------------------------------- #
# Dedup: repeated red reconciles on one lineage raise exactly ONE alert.
# --------------------------------------------------------------------------- #
def test_alert_dedup_on_repeat(tmp_errorta_home: Path, tmp_path: Path) -> None:
    s = _store("a4", tmp_path)
    anchors.reconcile(s, _run("H1", passed=True), project_id="a4")
    for h in ("H2", "H3", "H4"):
        anchors.reconcile(s, _run(h, passed=False), project_id="a4")
    assert len(_alerts("a4", s)) == 1  # deduped by (source, stage, title=key)


def test_regreen_reaffirms_after_break(tmp_errorta_home: Path,
                                       tmp_path: Path) -> None:
    s = _store("a5", tmp_path)
    anchors.reconcile(s, _run("H1", passed=True), project_id="a5")
    anchors.reconcile(s, _run("H2", passed=False), project_id="a5")  # break
    # Re-green clears the signal (the anchor is satisfiable) and re-affirms.
    anchors.reconcile(s, _run("H3", passed=True), project_id="a5")
    assert s.get_run_state()["test_anchors"][_KEY]["head"] == "H3"


# --------------------------------------------------------------------------- #
# The lock is master-scoped: reconcile never touches PR merge state.
# --------------------------------------------------------------------------- #
def test_lock_does_not_touch_pr_mergeable(tmp_errorta_home: Path,
                                          tmp_path: Path) -> None:
    s = _store("a6", tmp_path)
    pr = s.record_pr(task_id="t1", branch="b1", head="H2", dev_member="m")
    s.update_pr(pr["pr_id"], status="mergeable")
    anchors.reconcile(s, _run("H1", passed=True), project_id="a6")
    anchors.reconcile(s, _run("H2", passed=False), project_id="a6")  # break at H2
    # The PR's status is UNCHANGED — the anchor lock is not a per-branch merge veto.
    assert s.get_pr(pr["pr_id"])["status"] == "mergeable"


# --------------------------------------------------------------------------- #
# promote_anchor / broken_anchors primitives.
# --------------------------------------------------------------------------- #
def test_promote_and_broken_primitives(tmp_errorta_home: Path,
                                       tmp_path: Path) -> None:
    s = _store("a7", tmp_path)
    anchors.promote_anchor(s, _KEY, head="H1")
    assert s.get_run_state()["test_anchors"][_KEY]["head"] == "H1"
    # broken at a new head
    assert anchors.broken_anchors(s, _run("H2", passed=False))[0]["key"] == _KEY
    # not broken while green
    assert anchors.broken_anchors(s, _run("H2", passed=True)) == []
    # empty key is a no-op
    anchors.promote_anchor(s, "", head="H9")
    assert "" not in s.get_run_state()["test_anchors"]
