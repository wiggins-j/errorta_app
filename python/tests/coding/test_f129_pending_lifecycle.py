"""F129 Contract #7 lock-tests: productive attempts on a task are pending until
task-boundary review closes/escalates them. The ledger's ``update_task``
interceptor is the single choke-point for flushing pending payloads.

Marquee case: a light route's productive turn must NOT be credited as
``accepted`` if the task later escalates to a stronger route. Otherwise the PM's
performance corpus learns inverted data ("cheap model succeeded — use it again"
when in reality the escalation was what carried the task).
"""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.performance_corpus import (
    buffer_pending_attempt, corpus_path, read_records,
)


def _store(pid: str) -> LedgerStore:
    s = LedgerStore(pid)
    s.create_project(north_star="n", definition_of_done="d", target="new",
                     repo_path=None)
    return s


def _payload(route: str, *, task_id: str, assignment_id: str) -> dict:
    return dict(
        assignment_id=assignment_id, project_id="p", run_id="r",
        task_id=task_id, member_id="m", route_id=route,
        task_type="implementation", difficulty_tier="mid",
        capability_tier="mid", cost_tier=1, latency_ms=10,
        triggered_escalation=False,
    )


def test_task_done_flushes_pending_as_accepted(tmp_errorta_home: Path) -> None:
    s = _store("done-flush")
    t = s.add_task(title="do work", role="dev")
    pending = [_payload("claude_cli.opus", task_id=t.task_id,
                        assignment_id="asg-1")]
    s.update_task(t.task_id, _f129_pending=pending)

    s.update_task(t.task_id, state="done")

    rows = read_records(corpus_path())
    assert [(r.route_id, r.outcome) for r in rows] == [
        ("claude_cli.opus", "accepted")]
    # The persisted task no longer carries the buffer.
    reloaded = next(x for x in s.list_tasks() if x.task_id == t.task_id)
    assert "_f129_pending" not in (reloaded._extras or {})


def test_task_dropped_flushes_pending_as_rejected(tmp_errorta_home: Path) -> None:
    s = _store("drop-flush")
    t = s.add_task(title="do work", role="dev")
    pending = [_payload("claude_cli.opus", task_id=t.task_id,
                        assignment_id="asg-1")]
    s.update_task(t.task_id, _f129_pending=pending)

    s.update_task(t.task_id, state="dropped")

    rows = read_records(corpus_path())
    assert [(r.route_id, r.outcome) for r in rows] == [
        ("claude_cli.opus", "rejected")]


def test_in_pool_model_escalation_flushes_pending_as_rejected(
    tmp_errorta_home: Path,
) -> None:
    """The marquee: light was productive but the task escalated to strong.
    Light must NOT be credited as accepted."""
    s = _store("escalation-flush")
    t = s.add_task(title="do work", role="dev",
                   model_assignment={"assignment_id": "asg-light",
                                     "route_id": "local.ollama.qwen"})
    # Light's productive turn buffered pending.
    pending = [_payload("local.ollama.qwen", task_id=t.task_id,
                        assignment_id="asg-light")]
    s.update_task(t.task_id, _f129_pending=pending)

    # In-pool escalation swaps the assignment_id.
    s.update_task(t.task_id, state="todo",
                  model_assignment={"assignment_id": "asg-strong",
                                    "route_id": "claude_cli.opus"})

    # Strong's productive turn buffered pending, then task closes.
    pending_strong = [_payload("claude_cli.opus", task_id=t.task_id,
                               assignment_id="asg-strong")]
    s.update_task(t.task_id, _f129_pending=pending_strong)
    s.update_task(t.task_id, state="done")

    rows = read_records(corpus_path())
    # Two rows: light rejected (didn't carry the task), strong accepted.
    # NO row credits light as accepted.
    assert [(r.route_id, r.outcome) for r in rows] == [
        ("local.ollama.qwen", "rejected"),
        ("claude_cli.opus", "accepted"),
    ]


def test_cross_member_reassignment_flushes_pending_as_rejected(
    tmp_errorta_home: Path,
) -> None:
    """F127 cross-member reassignment (assignee cleared) is also a boundary."""
    s = _store("reassign-flush")
    t = s.add_task(title="do work", role="dev",
                   assignee_member_id="m-junior")
    pending = [_payload("local.ollama.qwen", task_id=t.task_id,
                        assignment_id="asg-1")]
    s.update_task(t.task_id, _f129_pending=pending)

    # Reassignment: assignee cleared, state stays todo.
    s.update_task(t.task_id, state="todo", assignee_member_id=None)

    rows = read_records(corpus_path())
    assert [(r.route_id, r.outcome) for r in rows] == [
        ("local.ollama.qwen", "rejected")]


def test_state_change_without_pending_writes_nothing(
    tmp_errorta_home: Path,
) -> None:
    """No buffered attempts = no corpus rows written on closure. Empty/corrupt
    telemetry never blocks or fabricates records."""
    s = _store("empty-flush")
    t = s.add_task(title="do work", role="dev")
    s.update_task(t.task_id, state="done")
    assert read_records(corpus_path()) == []


def test_no_flush_on_unrelated_patch(tmp_errorta_home: Path) -> None:
    """Patches that don't cross a boundary must leave pending untouched."""
    s = _store("noop-flush")
    t = s.add_task(title="do work", role="dev")
    pending = [_payload("claude_cli.opus", task_id=t.task_id,
                        assignment_id="asg-1")]
    s.update_task(t.task_id, _f129_pending=pending)

    # Unrelated patch — title change only, state and assignee unchanged.
    s.update_task(t.task_id, title="new title")

    reloaded = next(x for x in s.list_tasks() if x.task_id == t.task_id)
    assert reloaded._extras.get("_f129_pending") == pending
    assert read_records(corpus_path()) == []


def test_buffer_helper_used_by_runner_matches_extras_shape() -> None:
    """The runner uses ``buffer_pending_attempt`` to build the same extras key
    the ledger interceptor flushes. Lock the shape."""
    extras = buffer_pending_attempt({},
        _payload("claude_cli.opus", task_id="t", assignment_id="a"))
    assert "_f129_pending" in extras
    assert isinstance(extras["_f129_pending"], list)
    assert extras["_f129_pending"][0]["route_id"] == "claude_cli.opus"
