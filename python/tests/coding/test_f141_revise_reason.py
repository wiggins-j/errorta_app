"""F141 WS-D — rework tasks carry a legible reason, and the machine title stays
compatible with _supersede_ancestors' stale-task cleanup (the review blocker)."""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding import runner
from errorta_council.coding.ledger import LedgerStore

_FINDINGS = [
    {"severity": "blocking", "title": "Missing null check", "path": "src/api.ts",
     "body": "…", "blocking": True},
    {"severity": "advisory", "title": "Rename var", "path": "src/x.ts",
     "body": "…", "blocking": False},
]


def test_reason_from_findings_prefers_blocking() -> None:
    reason = runner._reason_from_findings(_FINDINGS)
    assert reason == "1 blocking finding: 'Missing null check' (src/api.ts)"


def test_reason_from_findings_counts_multiple_blocking() -> None:
    findings = [
        {"title": "A", "path": "a.ts", "blocking": True},
        {"title": "B", "path": "b.ts", "blocking": True},
    ]
    assert runner._reason_from_findings(findings) == (
        "2 blocking findings — 'A' (a.ts) +1 more")


def test_reason_from_findings_empty_is_blank() -> None:
    assert runner._reason_from_findings([]) == ""


def test_detail_from_findings_lists_capped() -> None:
    detail = runner._detail_from_findings(_FINDINGS)
    assert detail == "Missing null check (src/api.ts); Rename var (src/x.ts)"


def test_revise_task_carries_reason_and_persists(tmp_errorta_home: Path) -> None:
    store = LedgerStore("wsd")
    store.create_project(north_star="n", definition_of_done="d", target="new",
                         repo_path=None)
    branch = "feat/thing"
    t = store.add_task(
        title=f"revise: {branch}", role="dev",
        pr_id="pr-1", reason_summary=runner._reason_from_findings(_FINDINGS),
        detail=runner._detail_from_findings(_FINDINGS))
    # persisted + reloaded
    reloaded = {x.task_id: x for x in store.list_tasks()}[t.task_id]
    assert reloaded.reason_summary == "1 blocking finding: 'Missing null check' (src/api.ts)"
    assert "Missing null check" in reloaded.detail
    assert reloaded.to_dict()["reason_summary"] == reloaded.reason_summary


def test_revise_title_still_matches_supersede_matcher(tmp_errorta_home: Path) -> None:
    """BLOCKER regression: _supersede_ancestors prunes stale corrective tasks by
    matching `branch in title AND title.startswith(_CORRECTIVE_PREFIXES)`. The
    WS-D change keeps the reason OFF the title precisely so this still holds."""
    branch = "feat/thing"
    store = LedgerStore("wsd2")
    store.create_project(north_star="n", definition_of_done="d", target="new",
                         repo_path=None)
    t = store.add_task(title=f"revise: {branch}", role="dev", pr_id="pr-1",
                       reason_summary="1 blocking finding: 'X' (a.ts)")
    # Exactly the predicate _supersede_ancestors uses (runner.py ~259/325).
    assert branch in t.title
    assert t.title.lower().startswith(runner._CORRECTIVE_PREFIXES)
    # And the matcher would drop it when it's a todo (state after add_task).
    assert t.state == "todo"
    todo = [x for x in store.list_tasks(state="todo")
            if branch in x.title
            and x.title.lower().startswith(runner._CORRECTIVE_PREFIXES)]
    assert t.task_id in {x.task_id for x in todo}
