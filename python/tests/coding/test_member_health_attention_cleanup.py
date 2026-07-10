"""resolve_stale_member_health — clear blocking member-health Problems the
current roster has already fixed (route/provider changed, or member removed)."""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding import attention
from errorta_council.coding.ledger import LedgerStore


def _store(tmp_path: Path) -> LedgerStore:
    return LedgerStore("mh", root=tmp_path)


def _raise(s: LedgerStore, member_id: str, route: str, reason: str) -> None:
    attention.raise_member_health_problem(
        "mh", member_id=member_id, role="dev", route=route, reason=reason,
        detail="x", remediation="y", attempts=3, stage="development", store=s)


def _open_titles(s: LedgerStore) -> set[str]:
    return {
        sig.title
        for sig in attention.list_open("mh", store=s)
        if sig.source == "member_health"
    }


def test_dismisses_only_problems_whose_member_route_changed(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _raise(s, "m-dev-1", "cursor_cli.gpt-5-codex", "errored")   # route since changed
    _raise(s, "m-dev-1", "cursor_cli.composer-2.5", "rate_limited")  # route since changed
    _raise(s, "m-review-2", "cursor_cli.gpt-5", "errored")      # member still on it
    assert len(_open_titles(s)) == 3

    # Current roster: m-dev-1 moved to Claude; m-review-2 still on the bad Cursor route.
    members = [
        {"id": "m-dev-1", "provider_kind": "claude_cli", "gateway_route_id": "claude_cli.opus"},
        {"id": "m-review-2", "provider_kind": "cursor_cli", "gateway_route_id": "cursor_cli.gpt-5"},
    ]
    dismissed = attention.resolve_stale_member_health("mh", members, store=s)

    # Both m-dev-1 problems clear (its route changed); m-review-2's stays (unchanged).
    assert any("m-dev-1" in t for t in dismissed)
    assert all("m-review-2" not in t for t in dismissed)
    remaining = _open_titles(s)
    assert remaining == {"Member unhealthy: m-review-2 (errored)"}


def test_dismisses_problem_for_a_member_removed_from_the_roster(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _raise(s, "m-gone", "cursor_cli.gpt-5", "errored")
    members = [
        {"id": "m-pm", "provider_kind": "claude_cli", "gateway_route_id": "claude_cli.opus"},
    ]
    dismissed = attention.resolve_stale_member_health("mh", members, store=s)
    assert dismissed == ["Member unhealthy: m-gone (errored)"]
    assert _open_titles(s) == set()


def test_resolving_member_health_problem_creates_no_backlog_task(tmp_path: Path) -> None:
    # A member-health Problem is an infra issue (fix the provider/model), not dev
    # work — resolving it must NOT spawn a "Resolve attention problem: Member
    # unhealthy …" backlog task (meta-work that clutters the board + blocks
    # definition-of-done). Both the explicit accept and the PM auto-resolve path.
    s = _store(tmp_path)
    _raise(s, "m-dev-1", "cursor_cli.gpt-5", "errored")
    sig = next(
        x for x in attention.list_open("mh", store=s) if x.source == "member_health"
    )
    _, task_id = attention.resolve("mh", sig.id, "accept", by="pm", store=s)
    assert task_id is None
    assert not any(
        "Resolve attention" in (t.title or "") for t in s.list_tasks()
    )

    # auto-resolve path (block_on_problems off) likewise creates no task.
    _raise(s, "m-dev-2", "cursor_cli.gpt-5", "errored")
    sig2 = next(
        x for x in attention.list_open("mh", store=s) if x.source == "member_health"
    )
    _, task_id2 = attention.auto_resolve("mh", sig2.id, store=s)
    assert task_id2 is None
    assert not any("Resolve attention" in (t.title or "") for t in s.list_tasks())


def test_non_member_health_problem_still_creates_a_task_on_accept(tmp_path: Path) -> None:
    # Guard the scope: a normal PM problem still spawns its implementing task.
    s = _store(tmp_path)
    sig = attention.raise_signal(
        "mh", kind="problem", source="pm", stage="drafting_spec",
        title="Pick storage", summary="DB vs file?",
        pm_evaluation="The spec is ambiguous on storage.",
        suggestions=[{"id": "s1", "label": "Use SQLite", "detail": "local"}],
        store=s)
    _, task_id = attention.resolve("mh", sig.id, "accept", suggestion_id="s1", by="pm", store=s)
    assert task_id is not None


def test_keeps_problem_when_member_and_route_unchanged(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _raise(s, "m-dev-1", "cursor_cli.gpt-5", "errored")
    members = [
        {"id": "m-dev-1", "provider_kind": "cursor_cli", "gateway_route_id": "cursor_cli.gpt-5"},
    ]
    assert attention.resolve_stale_member_health("mh", members, store=s) == []
    assert _open_titles(s) == {"Member unhealthy: m-dev-1 (errored)"}
