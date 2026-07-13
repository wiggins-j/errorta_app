"""``--watch`` on read commands — re-render on the poll loop (F147 §5.3).

The app polls + re-renders; ``--watch`` does the same for a read command: dispatch
through the shared registry, redraw, sleep the poll interval, repeat until Ctrl-C.
It reuses the exact same ``registry.dispatch`` path as a one-shot invocation, so a
watched view is byte-identical to its single-shot render — no separate code path.

``iterations`` bounds the loop for tests; production leaves it ``None`` and stops on
``KeyboardInterrupt``.
"""
from __future__ import annotations

import sys
import time
from typing import Any, Callable, TextIO

from . import registry
from .errors import CliError
from .session import Context

DEFAULT_INTERVAL = 2.5

# Mutations that STREAM their own live view to completion (`run`). For these,
# `--watch` is redundant, not a re-firing hazard, so it's treated as a single
# run rather than rejected. app.py / repl route these to the normal dispatch
# path (for exit-code handling + a note); this set is the shared source of truth
# and run_watch also honors it as a safety net for direct callers.
SELF_STREAMING = frozenset({"run"})


def run_watch(
    name: str,
    client: Any,
    ctx: Context,
    raw_args: list[str],
    *,
    interval: float | None = None,
    iterations: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    out: TextIO | None = None,
    clear: bool = True,
) -> None:
    """Loop-render ``name`` until Ctrl-C (or ``iterations`` frames elapse).

    Rejects ``--watch`` on a MUTATING command (``setup`` / ``run`` / ``cancel`` /
    ``resume`` / ``continue``) BEFORE any dispatch: a watched mutation would
    re-fire the write every tick and spend real model budget (F147 S3 review #3).
    Reads are fine.
    """
    command = registry.get(name)
    if name in SELF_STREAMING:
        # `run` already streams its live view to completion — run it ONCE (no
        # poll loop, no re-fire) instead of rejecting --watch.
        registry.dispatch(name, client, ctx, raw_args, json_mode=False)
        return
    if command is not None and command.mutating:
        raise CliError(
            f"--watch is for read commands; `{name}` mutates run state and can't "
            "be watched (a watched mutation would re-fire every tick and spend "
            f"budget). Run `{name}` once, then watch progress with: "
            "errorta status --watch  /  errorta log --watch",
            code="watch_on_mutation",
        )
    stream = out or sys.stdout
    tick = interval if interval is not None else (ctx.poll_interval or DEFAULT_INTERVAL)
    if command is not None and command.watch_mode == "stream":
        _run_stream(name, client, ctx, raw_args, stream, tick, iterations, sleep)
        return
    count = 0
    while True:
        try:
            _payload, text = registry.dispatch(name, client, ctx, raw_args, json_mode=False)
        except KeyError:
            print(f"unknown command: {name}", file=sys.stderr)
            return
        except CliError as exc:
            # A command that rejects --watch on one of its OWN mutating sub-verbs
            # (pm/runtime register mutating=False but guard internally) fails the
            # same way every tick — re-raise so the caller prints it once and
            # exits, instead of redrawing the rejection forever.
            if exc.code == "watch_on_mutation":
                raise
            text = f"error: {exc.message}"
        _draw(stream, text, clear)
        count += 1
        if iterations is not None and count >= iterations:
            return
        try:
            sleep(float(tick))
        except KeyboardInterrupt:
            return


# F151 — stream/tail mode: append only NEW events each tick, never repaint.
INITIAL_WINDOW = 200  # on the first tick, print at most this much backlog


def _entry_key(e: dict) -> tuple:
    return (e.get("at"), e.get("role"), e.get("member"), e.get("kind"), e.get("message"))


def _common_prefix_len(a: list, b: list) -> int:
    n = 0
    for x, y in zip(a, b):
        if _entry_key(x) == _entry_key(y):
            n += 1
        else:
            break
    return n


def _run_stream(
    name: str, client: Any, ctx: Context, raw_args: list[str],
    stream: TextIO, tick: float, iterations: int | None,
    sleep: Callable[[float], None],
) -> None:
    """Tail a stream command (log): print only entries appended since last tick.

    The team-log has no stable per-event key (it's re-sorted, entries mutate), so
    diff by content longest-common-prefix: append the suffix after the shared
    prefix; if the prefix diverged (mid-list insert / mutation / reset), reprint
    from the divergence point. Never clears the screen."""
    from .render.log import filtered_entries, render_entries
    shown: list = []
    first = True
    count = 0
    while True:
        try:
            payload, _text = registry.dispatch(name, client, ctx, raw_args, json_mode=False)
            entries = filtered_entries(payload)
        except KeyError:
            print(f"unknown command: {name}", file=sys.stderr)
            return
        except CliError as exc:
            if exc.code == "watch_on_mutation":
                raise  # deterministic rejection — surface once, don't tail forever
            stream.write(f"error: {exc.message}\n")
            stream.flush()
            entries = shown  # don't lose the cursor on a transient error
        lcp = _common_prefix_len(shown, entries)
        to_print = entries[-INITIAL_WINDOW:] if first else entries[lcp:]
        first = False
        for line in render_entries(to_print):
            stream.write(line + "\n")
        stream.flush()
        shown = entries
        count += 1
        if iterations is not None and count >= iterations:
            return
        try:
            sleep(float(tick))
        except KeyboardInterrupt:
            return


# Clear sequence for a redraw. The cursor is HOMED (`\x1b[H`) BEFORE the erase
# (`\x1b[2J`): erasing first while the cursor sits at the bottom of a full screen
# makes some terminals (macOS Terminal.app among them) scroll the old frame up
# into the scrollback instead of clearing it in place — which is exactly the
# "the watched view accumulates every tick" bug. This matches what `tput clear`
# emits (ESC[H ESC[2J) for the same ordering reason. We deliberately do NOT add
# `\x1b[3J` (drop scrollback): wiping the user's terminal history on every poll
# tick is hostile, and homing-then-erasing already fixes the accumulation.
_CLEAR_SCREEN = "\x1b[H\x1b[2J"


def _draw(stream: TextIO, text: str, clear: bool) -> None:
    try:
        is_tty = stream.isatty()
    except (ValueError, AttributeError):
        is_tty = False
    if clear and is_tty:
        # Escape codes ONLY on a real TTY — a piped `errorta log --watch | tee`
        # must stay plain text (no ANSI leaks into the captured stream).
        stream.write(_CLEAR_SCREEN)
    stream.write(text + "\n")
    stream.flush()
