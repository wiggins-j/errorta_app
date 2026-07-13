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


def test_run_watch_reraises_internally_gated_mutation(make_ctx):
    # `pm`/`runtime` register mutating=False (they have read AND write sub-verbs)
    # and reject a watched mutation INSIDE the command. run_watch must surface
    # that rejection ONCE and stop — not render "error: …" and re-loop forever.
    client = RouteClient()
    with pytest.raises(CliError) as ei:
        watch.run_watch("runtime", client, make_ctx(project_id="p"),
                        ["setup", "--watch"], iterations=5, sleep=lambda _s: None,
                        out=io.StringIO(), clear=False)
    assert ei.value.code == "watch_on_mutation"
    assert client.calls == []  # guard fires before any HTTP


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


class _FakeTty(io.StringIO):
    """A StringIO that reports a configurable ``isatty()`` (so ``_draw`` can be
    exercised on both the real-terminal and piped paths)."""

    def __init__(self, is_tty: bool = True) -> None:
        super().__init__()
        self._is_tty = is_tty

    def isatty(self) -> bool:  # noqa: D401
        return self._is_tty


def test_draw_emits_no_ansi_when_not_a_tty():
    # Piped output (`errorta log --watch | tee`) MUST stay plain text — no clear
    # escape codes leak into the captured stream even with clear=True.
    out = _FakeTty(is_tty=False)
    watch._draw(out, "frame body", clear=True)
    value = out.getvalue()
    assert "\x1b" not in value
    assert value == "frame body\n"


def test_draw_homes_before_erasing_on_tty():
    # The redraw clear must HOME the cursor (\x1b[H) BEFORE erasing (\x1b[2J) —
    # erasing first is the macOS "scroll-into-scrollback" accumulation bug. It must
    # NOT wipe scrollback (\x1b[3J) — that would nuke the user's history every tick.
    out = _FakeTty(is_tty=True)
    watch._draw(out, "frame body", clear=True)
    value = out.getvalue()
    assert value.startswith("\x1b[H\x1b[2J")  # home THEN erase, not the reverse
    assert "\x1b[2J\x1b[H" not in value       # the old (buggy) ordering is gone
    assert "\x1b[3J" not in value             # scrollback preserved (not wiped per tick)
    assert value.endswith("frame body\n")


def test_run_watch_clears_between_every_frame_on_tty(make_ctx):
    # The redraw path is exercised on a TTY: every tick clears the previous frame,
    # so the rendered view is REDRAWN in place, not appended forever. One clear
    # per frame + one content render per frame ⇒ bounded, no accumulation.
    client = RouteClient(default={"tasks": [
        {"task_id": "t1", "title": "do it", "role": "dev", "state": "doing"}]})
    out = _FakeTty(is_tty=True)
    watch.run_watch(
        "tasks", client, make_ctx(project_id="p"), [],
        interval=0.0, iterations=3, sleep=lambda _s: None, out=out, clear=True,
    )
    value = out.getvalue()
    assert value.count("\x1b[H\x1b[2J") == 3  # exactly one clear per redraw
    assert value.count("do it") == 3          # content redrawn, not duplicated-and-kept
