"""``--watch`` re-renders a read command on the poll loop (bounded for tests).

It reuses the exact ``registry.dispatch`` path, so a watched frame is identical to
a one-shot render; a fake sleep + ``iterations`` bound the loop.
"""
from __future__ import annotations

import io

import pytest

from errorta_cli import watch
from errorta_cli.errors import CliError

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


# `run` is EXCLUDED — it streams its own live view to completion, so --watch is
# handled as a single run (see test_run_watch_run_streams_once), not rejected.
@pytest.mark.parametrize("name", ["setup", "cancel", "resume", "continue"])
def test_run_watch_rejects_mutating_commands(make_ctx, name):
    # `cancel --watch` (etc.) would re-fire the mutation every tick and burn
    # budget. It must be rejected BEFORE any dispatch — no request fired (#3).
    client = RouteClient()
    with pytest.raises(CliError) as ei:
        watch.run_watch(name, client, make_ctx(project_id="p"), ["--yes"],
                        iterations=1, sleep=lambda _s: None,
                        out=io.StringIO(), clear=False)
    assert ei.value.code == "watch_on_mutation"
    assert client.calls == []  # never dispatched → nothing mutated / no budget spent


def test_run_watch_run_streams_once(make_ctx, monkeypatch):
    # `run --watch` is NOT rejected: `run` already streams live, so run_watch
    # dispatches it exactly ONCE (no poll loop, no re-fire), not an error.
    calls: list[str] = []
    monkeypatch.setattr(
        watch.registry, "dispatch",
        lambda name, *a, **k: (calls.append(name), ("payload", "text"))[1],
    )
    watch.run_watch("run", RouteClient(), make_ctx(project_id="p"),
                    ["--yes"], iterations=5, sleep=lambda _s: None,
                    out=io.StringIO(), clear=False)
    assert calls == ["run"]  # dispatched once despite iterations=5 (no loop)


def test_run_watch_allows_read_command(make_ctx):
    # Regression guard: the mutation gate must NOT block a read from watching.
    client = RouteClient(default={"tasks": []})
    watch.run_watch("tasks", client, make_ctx(project_id="p"), [], iterations=1,
                    sleep=lambda _s: None, out=io.StringIO(), clear=False)
    assert any(method == "GET" for method, _ in client.calls)
