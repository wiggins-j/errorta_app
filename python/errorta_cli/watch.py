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

# Spec 06 — the `watch` command IS a live run dashboard: it loops by default
# (`--once` renders a single snapshot). Both front-ends normalize a bare
# `errorta watch` into a watched invocation via `arm_dashboard`, so the loop reuses
# THIS harness (no new threading model).
DASHBOARD = "watch"


def _interval_arg(raw_args: list[str]) -> float | None:
    """Parse ``--interval N`` (the dashboard tick) out of ``raw_args``; None if
    absent or unparseable (so the caller keeps its existing default)."""
    for i, token in enumerate(raw_args):
        if token == "--interval" and i + 1 < len(raw_args):
            try:
                return float(raw_args[i + 1])
            except ValueError:
                return None
    return None


def arm_dashboard(name: str, raw_args: list[str], ctx: Context) -> list[str]:
    """Make ``errorta watch`` loop by default.

    Injects the ``--watch`` the poll harness keys on unless the caller asked for a
    single ``--once`` snapshot (or ``--json``, which is always one-shot). ``--interval
    N`` (default 2s) sets the tick via ``ctx.poll_interval``. Returns the possibly-
    extended ``raw_args``; a no-op for every other command.
    """
    if name != DASHBOARD or "--once" in raw_args or "--json" in raw_args:
        return raw_args
    interval = _interval_arg(raw_args)
    if interval is not None:
        ctx.poll_interval = interval
    elif ctx.poll_interval is None:
        ctx.poll_interval = 2.0
    if "--watch" not in raw_args:
        raw_args = [*raw_args, "--watch"]
    return raw_args


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
    # F158: resolve the watch mode per-invocation so a command with mixed sub-verbs
    # (e.g. `pm chat` streams, `pm changes` snapshots) picks the right one.
    mode = "snapshot"
    if command is not None:
        mode = command.watch_mode_for(registry.resolve_args(command, raw_args))
    if mode == "stream":
        _run_stream(name, client, ctx, raw_args, stream, tick, iterations, sleep,
                    command=command)
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
    *, command: Any | None = None,
) -> None:
    """Tail a stream command: print only entries appended since last tick.

    The source has no stable per-event key (the team-log is re-sorted and entries
    mutate; a chat transcript is append-mostly), so diff by content
    longest-common-prefix: append the suffix after the shared prefix; if the
    prefix diverged (mid-list insert / mutation / reset), reprint from the
    divergence point. Never clears the screen.

    F158: the entry extractor + per-entry renderer come from the command
    (``stream_entries_fn`` / ``stream_render_fn``); both default to the team-log
    implementation so `log --watch` is unchanged."""
    from .render.log import filtered_entries
    from .render.log import render_entries as _log_render
    extract = getattr(command, "stream_entries_fn", None) or filtered_entries
    render_entries = getattr(command, "stream_render_fn", None) or _log_render
    shown: list = []
    first = True
    count = 0
    while True:
        try:
            payload, _text = registry.dispatch(name, client, ctx, raw_args, json_mode=False)
            entries = extract(payload)
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
