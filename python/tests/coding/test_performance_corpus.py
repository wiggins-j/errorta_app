from pathlib import Path

from errorta_council.coding.performance_corpus import append, digest, make_attempt, read_records


def _attempt(route: str, outcome: str, *, escalation: bool = False):
    return make_attempt(
        assignment_id=f"a-{route}", project_id="p", run_id="r", task_id="t",
        member_id="m", route_id=route, task_type="implementation",
        difficulty_tier="mid", capability_tier="mid", cost_tier=1,
        latency_ms=10, outcome=outcome, triggered_escalation=escalation,
    )


def test_attempts_are_attributed_per_route(tmp_path: Path) -> None:
    path = tmp_path / "attempts.jsonl"
    append(_attempt("local.ollama.qwen", "rejected", escalation=True), path)
    append(_attempt("claude_cli.opus", "accepted"), path)
    rows = read_records(path)
    assert [(row.route_id, row.outcome) for row in rows] == [
        ("local.ollama.qwen", "rejected"), ("claude_cli.opus", "accepted")]
    stats = digest(path)
    assert stats["local.ollama.qwen"]["implementation:mid"]["accepted_rate"] == 0
    assert stats["claude_cli.opus"]["implementation:mid"]["accepted_rate"] == 1


def test_corrupt_line_does_not_hide_valid_rows(tmp_path: Path) -> None:
    path = tmp_path / "attempts.jsonl"
    append(_attempt("openai.gpt-5", "accepted"), path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{truncated\n")
    assert len(read_records(path)) == 1


# F129 Contract #7: productive attempts are pending until task-boundary review.


def _pending_payload(route: str) -> dict:
    return dict(
        assignment_id=f"a-{route}", project_id="p", run_id="r", task_id="t",
        member_id="m", route_id=route, task_type="implementation",
        difficulty_tier="mid", capability_tier="mid", cost_tier=1,
        latency_ms=10, triggered_escalation=False,
    )


def test_buffer_pending_attempt_appends_to_extras() -> None:
    from errorta_council.coding.performance_corpus import buffer_pending_attempt

    extras = buffer_pending_attempt({}, _pending_payload("local.ollama.qwen"))
    extras = buffer_pending_attempt(extras, _pending_payload("claude_cli.opus"))
    pending = extras["_f129_pending"]
    assert [p["route_id"] for p in pending] == [
        "local.ollama.qwen", "claude_cli.opus"]


def test_flush_pending_accepts_writes_records_and_clears_extras(tmp_path: Path) -> None:
    from errorta_council.coding.performance_corpus import (
        buffer_pending_attempt, flush_pending_attempts,
    )

    path = tmp_path / "attempts.jsonl"
    extras = buffer_pending_attempt({}, _pending_payload("claude_cli.opus"))
    written, cleaned = flush_pending_attempts(extras, "accepted", path=path)
    assert written == 1
    assert "_f129_pending" not in cleaned
    rows = read_records(path)
    assert len(rows) == 1
    assert rows[0].outcome == "accepted"
    assert rows[0].route_id == "claude_cli.opus"


def test_flush_pending_rejected_on_escalation(tmp_path: Path) -> None:
    """The marquee: a light route's productive turn should be rejected when the
    task later escalates to a stronger route (learn inverted performance data)."""
    from errorta_council.coding.performance_corpus import (
        buffer_pending_attempt, flush_pending_attempts,
    )

    path = tmp_path / "attempts.jsonl"
    # Light route was productive; buffered pending.
    extras = buffer_pending_attempt({}, _pending_payload("local.ollama.qwen"))
    # Task escalated to mid — pending flushed as rejected.
    written, cleaned = flush_pending_attempts(extras, "rejected", path=path)
    assert written == 1
    # Now a strong-route turn is productive and later accepted at task-done.
    extras2 = buffer_pending_attempt(cleaned, _pending_payload("claude_cli.opus"))
    flush_pending_attempts(extras2, "accepted", path=path)
    rows = read_records(path)
    # Two rows: light rejected, strong accepted. NO row credits light as accepted.
    assert [(r.route_id, r.outcome) for r in rows] == [
        ("local.ollama.qwen", "rejected"),
        ("claude_cli.opus", "accepted"),
    ]


def test_flush_empty_pending_is_noop(tmp_path: Path) -> None:
    from errorta_council.coding.performance_corpus import flush_pending_attempts

    path = tmp_path / "attempts.jsonl"
    written, cleaned = flush_pending_attempts({}, "accepted", path=path)
    assert written == 0
    assert cleaned == {}
    assert not path.exists()


def test_flush_unknown_outcome_raises() -> None:
    from errorta_council.coding.performance_corpus import flush_pending_attempts
    import pytest

    with pytest.raises(ValueError):
        flush_pending_attempts({"_f129_pending": [_pending_payload("r")]}, "bogus")
