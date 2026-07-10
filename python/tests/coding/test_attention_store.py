"""F117-01 — attention signal primitive + store + lifecycle."""
from __future__ import annotations

import pytest

from errorta_council.coding import attention
from errorta_council.coding.ledger import LedgerStore

PID = "attn-proj"


@pytest.fixture
def store(tmp_errorta_home) -> LedgerStore:
    return LedgerStore(PID)


def _problem(store, **over):
    kw = dict(
        kind="problem", source="pm", stage="drafting_spec",
        title="Pick storage", summary="DB vs file?",
        pm_evaluation="The spec is ambiguous on storage.",
        suggestions=[{"id": "s1", "label": "Use a file", "detail": "Store as JSON file"}],
        store=store,
    )
    kw.update(over)
    return attention.raise_signal(PID, **kw)


def test_problem_fail_closed_requires_evaluation_and_suggestion(store):
    with pytest.raises(attention.AttentionError):
        attention.raise_signal(PID, kind="problem", source="pm", stage="x",
                               title="t", summary="s", store=store)
    with pytest.raises(attention.AttentionError):
        attention.raise_signal(PID, kind="problem", source="pm", stage="x",
                               title="t", summary="s", pm_evaluation="e",
                               suggestions=[], store=store)


def test_raise_problem_open_and_blocking(store):
    sig = _problem(store)
    assert sig.state == "open"
    assert sig.blocking is True
    assert attention.blocks_stage(PID, "drafting_spec", store=store) is True
    assert attention.blocks_stage(PID, "build", store=store) is False


def test_raise_alert_is_advisory_and_nonblocking(store):
    sig = attention.raise_signal(PID, kind="alert", source="reviewer",
                                 stage="reviewing_build", title="button vs autosave",
                                 summary="No guidance on save UX", store=store)
    assert sig.kind == "alert" and sig.blocking is False
    assert attention.blocks_stage(PID, "reviewing_build", store=store) is False


def test_invalid_kind_raises(store):
    with pytest.raises(attention.AttentionError):
        attention.raise_signal(PID, kind="weird", source="pm", stage="x",
                               title="t", summary="s", store=store)


def test_accept_problem_suggestion_creates_linked_pm_task(store):
    sig = _problem(store)
    updated, task_id = attention.resolve(PID, sig.id, "accept",
                                         suggestion_id="s1", store=store)
    assert updated.state == "accepted"
    assert task_id is not None
    assert updated.resolution["created_task_id"] == task_id
    # the task is a PM task linked back via _extras
    task = next(t for t in store.list_tasks() if t.task_id == task_id)
    assert task.role == "pm"
    assert task._extras.get("source_signal_id") == sig.id
    # stage no longer blocked
    assert attention.blocks_stage(PID, "drafting_spec", store=store) is False


def test_correct_creates_task_from_correction_text(store):
    sig = _problem(store)
    updated, task_id = attention.resolve(PID, sig.id, "correct",
                                         correction_text="Use SQLite instead", store=store)
    assert updated.state == "corrected"
    task = next(t for t in store.list_tasks() if t.task_id == task_id)
    assert "SQLite" in task.detail


def test_correct_requires_text(store):
    sig = _problem(store)
    with pytest.raises(attention.AttentionError):
        attention.resolve(PID, sig.id, "correct", store=store)


def test_alert_defer_and_dismiss_are_terminal_no_task(store):
    a1 = attention.raise_signal(PID, kind="alert", source="reviewer", stage="s",
                                title="a", summary="b", store=store)
    upd, task = attention.resolve(PID, a1.id, "defer", store=store)
    assert upd.state == "deferred" and task is None
    a2 = attention.raise_signal(PID, kind="alert", source="reviewer", stage="s",
                                title="c", summary="d", store=store)
    upd2, task2 = attention.resolve(PID, a2.id, "dismiss", store=store)
    assert upd2.state == "dismissed" and task2 is None


def test_problem_cannot_be_dismissed_or_deferred(store):
    sig = _problem(store)
    for bad in ("dismiss", "defer"):
        with pytest.raises(attention.AttentionError):
            attention.resolve(PID, sig.id, bad, store=store)


def test_resolving_a_resolved_signal_raises(store):
    sig = _problem(store)
    attention.resolve(PID, sig.id, "accept", suggestion_id="s1", store=store)
    with pytest.raises(attention.AttentionError):
        attention.resolve(PID, sig.id, "accept", suggestion_id="s1", store=store)


def test_unknown_signal_raises(store):
    with pytest.raises(attention.AttentionError):
        attention.resolve(PID, "sig-does-not-exist", "accept", store=store)


def test_auto_resolve_records_and_shows(store):
    sig = _problem(store)
    updated, task_id = attention.auto_resolve(PID, sig.id, store=store)
    assert updated.state == "auto_resolved"
    assert task_id is not None
    # still listed (not silently handled) — just no longer open
    assert sig.id in {s.id for s in attention.list_all(PID, store=store)}
    assert updated.id not in {s.id for s in attention.list_open(PID, store=store)}


def test_survives_reload(store, tmp_errorta_home):
    sig = _problem(store)
    attention.resolve(PID, sig.id, "accept", suggestion_id="s1", store=store)
    fresh = LedgerStore(PID)  # new instance re-reads signals.jsonl
    reloaded = attention.get(PID, sig.id, store=fresh)
    assert reloaded is not None and reloaded.state == "accepted"


def test_transition_records_team_log_decision(store):
    sig = _problem(store)
    attention.resolve(PID, sig.id, "accept", suggestion_id="s1", store=store)
    decisions = store.list_decisions()
    ids = [d.get("attention_signal_id") for d in decisions]
    assert sig.id in ids  # raise + resolve both recorded
    assert sum(1 for d in decisions if d.get("attention_signal_id") == sig.id) >= 2


def test_list_filters_by_state_and_kind(store):
    p = _problem(store)
    a = attention.raise_signal(PID, kind="alert", source="pm", stage="s",
                               title="a", summary="b", store=store)
    assert {s.id for s in attention.list_all(PID, kind="problem", store=store)} == {p.id}
    assert {s.id for s in attention.list_all(PID, kind="alert", store=store)} == {a.id}
    assert {s.id for s in attention.list_open(PID, store=store)} == {p.id, a.id}
