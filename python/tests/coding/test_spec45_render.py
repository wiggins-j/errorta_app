from errorta_cli.render.decisions import render_decisions
from errorta_cli.render.board import render_board


def test_decisions_render_reason_code():
    payload = {"decisions": [{
        "at": "2026-08-07T00:00:00Z", "choice": "task_requires_absent_capability",
        "title": "task blocked (no executor): run suite",
        "reason_code": "missing_capability", "capability": "execution_gate"}]}
    out = render_decisions(payload, None)
    assert "missing_capability" in out


def test_board_renders_blocked_reason():
    payload = {"tasks": [{
        "task_id": "t1", "title": "run suite", "role": "dev", "state": "blocked",
        "blocked_reason": "missing_capability:execution_gate"}]}
    out = render_board(payload, None)
    assert "run suite" in out
    assert "capability" in out.lower()
