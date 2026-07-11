"""The interactive slash REPL (prompt_toolkit).

Parses ``/name args`` against the SAME registry the argv front-end uses — parity
by construction (golden invariant #3). S1 keeps it minimal: command completion,
history, ``--json`` passthrough, a handful of builtins (``/help``, ``/verbosity``,
``/watch|/mute|/focus``, ``/quit``), and registry dispatch. Rich rendering and
the live poller arrive in S2.

The parse+dispatch core is factored into :func:`handle_line` so it is unit
testable without a live terminal; ``prompt_toolkit`` is imported lazily inside
:func:`run_repl`.
"""
from __future__ import annotations

from pathlib import Path

from . import registry
from .client import SidecarClient
from .errors import CliError
from .session import Context
from .verbosity import parse_level

# Builtin (non-route) REPL verbs.
_QUIT = {"quit", "exit", "q"}
_BUILTINS = {"help", "verbosity", "watch", "mute", "focus", "unfocus"} | _QUIT


def is_quit(line: str) -> bool:
    name, _ = registry.split_slash(line)
    return name in _QUIT


def handle_line(line: str, ctx: Context, client: SidecarClient) -> str:
    """Handle one REPL line; return the text to print (never raises CliError)."""
    name, raw_args = registry.split_slash(line)
    if not name:
        return ""
    if name in _QUIT:
        return "bye"
    if name == "help":
        return _help_text()
    if name == "verbosity":
        return _set_verbosity(ctx, raw_args)
    if name in ("watch", "mute", "focus", "unfocus"):
        return _channel_op(ctx, name, raw_args)

    try:
        _payload, text = registry.dispatch(name, client, ctx, raw_args)
    except KeyError:
        return f"unknown command: /{name} (try /help)"
    except CliError as exc:
        return f"error: {exc.message}"
    return text


def _help_text() -> str:
    lines = ["Commands:"]
    for cmd in registry.all_commands():
        lines.append(f"  /{cmd.name:<12} {cmd.help}")
    lines.append("  /verbosity N   set global verbosity 0..5")
    lines.append("  /watch CH      force-show a channel; /mute CH; /focus CH; /unfocus")
    lines.append("  /quit          leave the session")
    return "\n".join(lines)


def _set_verbosity(ctx: Context, raw_args: list[str]) -> str:
    if not raw_args:
        return f"verbosity: {int(ctx.verbosity.level)}"
    ctx.verbosity.level = parse_level(raw_args[0])
    return f"verbosity: {int(ctx.verbosity.level)}"


def _channel_op(ctx: Context, op: str, raw_args: list[str]) -> str:
    if op == "unfocus":
        ctx.verbosity.set_focus(None)
        return "focus cleared"
    if not raw_args:
        return f"usage: /{op} <channel>"
    channel = raw_args[0]
    if op == "watch":
        ctx.verbosity.watch(channel)
        return f"watching {channel}"
    if op == "mute":
        ctx.verbosity.mute(channel)
        return f"muted {channel}"
    ctx.verbosity.set_focus(channel)
    return f"focused {channel}"


def run_repl(ctx: Context, client: SidecarClient, *, cwd: Path | None = None) -> None:
    """Run the interactive session until the user quits."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.history import InMemoryHistory

    completer = WordCompleter(
        [f"/{n}" for n in registry.names()] + [f"/{b}" for b in sorted(_BUILTINS)],
        sentence=True,
    )
    session: PromptSession = PromptSession(
        history=InMemoryHistory(), completer=completer
    )
    _banner(ctx)
    while True:
        try:
            line = session.prompt(_prompt(ctx))
        except (EOFError, KeyboardInterrupt):
            print("bye")
            return
        if not line.strip():
            continue
        if is_quit(line):
            print("bye")
            return
        name, raw_args = registry.split_slash(line)
        # `/log --watch` (etc.) tails on the poll loop until Ctrl-C, then returns
        # to the prompt. Only registry commands watch; builtins render once.
        if "--watch" in raw_args and registry.get(name) is not None:
            from . import watch as _watch

            try:
                _watch.run_watch(name, client, ctx, raw_args)
            except KeyboardInterrupt:
                pass
            continue
        text = handle_line(line, ctx, client)
        if text:
            print(text)


def _prompt(ctx: Context) -> str:
    label = ctx.project_id or "no-project"
    return f"errorta[{label}]> "


def _banner(ctx: Context) -> None:
    where = ctx.project_id or "no project bound to this directory"
    print(f"Errorta CLI — {where}. Type /help, /quit to leave.")
