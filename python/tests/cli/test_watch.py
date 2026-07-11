"""``--watch`` re-renders a read command on the poll loop (bounded for tests).

It reuses the exact ``registry.dispatch`` path, so a watched frame is identical to
a one-shot render; a fake sleep + ``iterations`` bound the loop.
"""
from __future__ import annotations

import io

from errorta_cli import watch

from .conftest import RouteClient


def test_run_watch_redraws_bounded_by_iterations(make_ctx):
    client = RouteClient(default={"tasks": [
        {"task_id": "t1", "title": "do it", "role": "dev", "state": "doing"}]})
    ctx = make_ctx(project_id="p")
    out = io.StringIO()
    slept: list[float] = []
    watch.run_watch(
        "tasks", client, ctx, [], interval=2.0, iterations=3,
        sleep=slept.append, out=out, clear=False,
    )
    # Three frames drawn; sleeps happen between frames (bounded, not after last).
    assert out.getvalue().count("do it") == 3
    assert slept == [2.0, 2.0]  # iterations-1 sleeps


def test_run_watch_uses_ctx_poll_interval_when_unset(make_ctx):
    client = RouteClient(default={"tasks": []})
    ctx = make_ctx(project_id="p")
    ctx.poll_interval = 0.5
    slept: list[float] = []
    watch.run_watch("tasks", client, ctx, [], iterations=2, sleep=slept.append,
                    out=io.StringIO(), clear=False)
    assert slept == [0.5]


def test_run_watch_reports_unknown_command(make_ctx, capsys):
    ctx = make_ctx(project_id="p")
    watch.run_watch("nope", RouteClient(), ctx, [], iterations=1,
                    sleep=lambda _s: None, out=io.StringIO(), clear=False)
    assert "unknown command" in capsys.readouterr().err
