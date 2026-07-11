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
    """Loop-render ``name`` until Ctrl-C (or ``iterations`` frames elapse)."""
    stream = out or sys.stdout
    tick = interval if interval is not None else (ctx.poll_interval or DEFAULT_INTERVAL)
    count = 0
    while True:
        try:
            _payload, text = registry.dispatch(name, client, ctx, raw_args, json_mode=False)
        except KeyError:
            print(f"unknown command: {name}", file=sys.stderr)
            return
        except CliError as exc:
            text = f"error: {exc.message}"
        _draw(stream, text, clear)
        count += 1
        if iterations is not None and count >= iterations:
            return
        try:
            sleep(float(tick))
        except KeyboardInterrupt:
            return


def _draw(stream: TextIO, text: str, clear: bool) -> None:
    try:
        is_tty = stream.isatty()
    except (ValueError, AttributeError):
        is_tty = False
    if clear and is_tty:
        stream.write("\x1b[2J\x1b[H")
    stream.write(text + "\n")
    stream.flush()
