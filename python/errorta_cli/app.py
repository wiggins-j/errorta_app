"""The ``errorta`` root app — Typer argv front-end + ``main()`` entry.

Registers one argv command per registry entry (parity with the slash REPL by
construction), the global options ``--home / --verbosity / --no-spawn / --json``,
the ``errorta sidecar {status,stop,restart}`` lifecycle group, the hidden
``__serve__`` subcommand, and — for a bare ``errorta`` with no subcommand — the
interactive REPL.
"""
from __future__ import annotations

import json as _json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import typer

from . import config, registry, serve, sidecar
from .client import SidecarClient
from .errors import CliError
from .session import Context
from .verbosity import Verbosity, resolve_level

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    help="Errorta — headless Coding Council CLI (a sidecar client).",
)


@dataclass
class _Globals:
    """Global options captured on the root callback."""

    home: Optional[str] = None
    verbosity: Optional[str] = None
    no_spawn: bool = False
    json: bool = False
    poll_interval: Optional[float] = None
    no_onboarding: bool = False
    extra: dict[str, object] = field(default_factory=dict)


_G = _Globals()


# --------------------------------------------------------------------------- #
# Root callback: capture globals; launch the REPL when no subcommand is given.
# --------------------------------------------------------------------------- #

@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    home: Optional[str] = typer.Option(
        None, "--home", help="Override ERRORTA_HOME (isolated store)."
    ),
    verbosity: Optional[str] = typer.Option(
        None, "--verbosity", "-V", help="Global verbosity 0..5 or a name."
    ),
    no_spawn: bool = typer.Option(
        False, "--no-spawn", help="Never spawn a sidecar; error if none is running."
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit the raw route payload as JSON to stdout."
    ),
    poll_interval: Optional[float] = typer.Option(
        None, "--poll-interval", help="Seconds between --watch re-renders / poll ticks."
    ),
    no_onboarding: bool = typer.Option(
        False, "--no-onboarding", help="Suppress the first-run welcome hint."
    ),
) -> None:
    _G.home = home
    _G.verbosity = verbosity
    _G.no_spawn = no_spawn
    _G.json = json_out
    _G.poll_interval = poll_interval
    _G.no_onboarding = no_onboarding
    if ctx.invoked_subcommand is None:
        _launch_repl()
        raise typer.Exit()


# --------------------------------------------------------------------------- #
# Hidden `__serve__` — run the embedded sidecar in-process.
# --------------------------------------------------------------------------- #

@app.command("__serve__", hidden=True)
def _serve() -> None:
    """Run the embedded uvicorn sidecar (self-re-exec target)."""
    serve.run()


# --------------------------------------------------------------------------- #
# Sidecar lifecycle group.
# --------------------------------------------------------------------------- #

sidecar_app = typer.Typer(help="Manage the CLI-owned sidecar.")
app.add_typer(sidecar_app, name="sidecar")


@sidecar_app.command("status")
def _sidecar_status() -> None:
    home = config.resolve_home(_G.home)
    info = sidecar.status(home)
    if _G.json:
        typer.echo(_json.dumps(info, indent=2, default=str))
        return
    if not info["running"]:
        typer.echo("sidecar: not running (no live CLI sidecar for this ERRORTA_HOME)")
        return
    rec = info["record"] or {}
    typer.echo(
        f"sidecar: running on 127.0.0.1:{rec.get('port')} "
        f"(pid {rec.get('pid')}, started_by {rec.get('started_by')})"
    )


@sidecar_app.command("stop")
def _sidecar_stop() -> None:
    home = config.resolve_home(_G.home)
    result = sidecar.stop(home)
    typer.echo(_json.dumps(result, default=str) if _G.json else _stop_line(result))


@sidecar_app.command("restart")
def _sidecar_restart() -> None:
    home = config.resolve_home(_G.home)
    try:
        handle = sidecar.restart(home, our_commit=config.build_commit())
    except CliError as exc:
        _fail(exc)
        return
    if _G.json:
        typer.echo(_json.dumps({"port": handle.port, "pid": handle.pid}, default=str))
    else:
        typer.echo(f"sidecar: restarted on 127.0.0.1:{handle.port} (pid {handle.pid})")


def _stop_line(result: dict) -> str:
    if result.get("stopped"):
        return f"sidecar: stopped (pid {result.get('pid')})"
    return f"sidecar: nothing to stop ({result.get('reason', 'not running')})"


# --------------------------------------------------------------------------- #
# F149 shell integration — top-level so it never spawns a sidecar (it is eval'd
# from the user's rc file on every shell startup).
# --------------------------------------------------------------------------- #

@app.command("shell-init")
def _shell_init(
    shell: str = typer.Argument("zsh", help="Shell to emit the hook for: zsh | bash."),
) -> None:
    """Print the shell hook that auto-cds into a project after `errorta new`."""
    from .shellinit import render_hook
    try:
        typer.echo(render_hook(shell), nl=False)
    except CliError as exc:
        _fail(exc)


# --------------------------------------------------------------------------- #
# Registry commands → argv commands.
# --------------------------------------------------------------------------- #

def _register_argv_commands() -> None:
    for command in registry.all_commands():
        _add_argv_command(command)


def _add_argv_command(command: registry.Command) -> None:
    command_name = command.name

    def _handler(ctx: typer.Context) -> None:
        _run_registry_command(command_name, list(ctx.args))

    # F151: register the canonical name + each alias as its own Typer command
    # (Typer resolves subcommands by registered name), all dispatching under the
    # canonical name.
    for exposed in (command.name, *command.aliases):
        app.command(
            name=exposed,
            help=command.help if exposed == command.name
            else f"Alias of `{command.name}`.",
            context_settings={
                "allow_extra_args": True,
                "ignore_unknown_options": True,
            },
        )(_handler)


def _run_registry_command(name: str, raw_args: list[str]) -> None:
    """Resolve the sidecar, dispatch through the shared registry, print, exit."""
    # Global options work in either position: before the subcommand (parsed by
    # the callback into `_G`) or after it (in `raw_args`). Reconcile both here so
    # `errorta status --no-spawn` behaves like `errorta --no-spawn status`.
    try:
        post, raw_args = _extract_post_globals(raw_args)
    except CliError as exc:
        _fail(exc)
        return
    home_override = post.get("home", _G.home)
    verbosity_raw = post.get("verbosity", _G.verbosity)
    no_spawn = _G.no_spawn or post.get("no_spawn", False)
    json_mode = _G.json or post.get("json", False)
    poll_interval = post.get("poll_interval", _G.poll_interval)
    no_onboarding = _G.no_onboarding or post.get("no_onboarding", False)

    home = config.resolve_home(home_override)
    verbosity = Verbosity(level=resolve_level(verbosity_raw))
    ctx = Context.build(
        home_override=home_override, verbosity=verbosity, poll_interval=poll_interval,
        cwd=Path.cwd(),
    )

    try:
        handle = sidecar.resolve(
            home, allow_spawn=not no_spawn, our_commit=config.build_commit()
        )
    except CliError as exc:
        _fail(exc)
        return
    ctx.handle = handle
    if handle.commit_mismatch:
        typer.echo(
            "warning: this CLI and the running sidecar were built from "
            "different commits; behavior may differ.",
            err=True,
        )
    _maybe_onboard(handle, json_mode=json_mode, no_onboarding=no_onboarding,
                   command_name=name)

    # `--watch` on a read command re-renders on the poll loop (never in --json/CI).
    if "--watch" in raw_args and not json_mode:
        from . import watch as _watch

        if name in _watch.SELF_STREAMING:
            # `run` already streams live to completion — --watch is redundant.
            # Drop it and fall through to the normal dispatch (which streams AND
            # sets the terminal exit code), with a gentle note.
            typer.echo(
                f"note: `{name}` already streams live; --watch has no extra effect.",
                err=True,
            )
            raw_args = [a for a in raw_args if a != "--watch"]
        else:
            with SidecarClient(handle.base_url) as client:
                try:
                    _watch.run_watch(name, client, ctx, raw_args)
                except KeyboardInterrupt:
                    pass
                except CliError as exc:
                    # e.g. `cancel --watch` — a mutating command rejects the loop.
                    _fail(exc)
            return

    with SidecarClient(handle.base_url) as client:
        try:
            payload, text = registry.dispatch(
                name, client, ctx, raw_args, json_mode=json_mode
            )
        except KeyError:
            typer.echo(f"unknown command: {name}", err=True)
            raise typer.Exit(code=1) from None
        except CliError as exc:
            _fail(exc)
            return
    typer.echo(text)
    # A command may PRINT its result and still want a non-zero exit (the run
    # command stamps `_exit_code` when a run ends in a failure-class stop_reason).
    code = registry.exit_code_for(payload)
    if code:
        raise typer.Exit(code=code)


def _extract_post_globals(raw_args: list[str]) -> tuple[dict[str, object], list[str]]:
    """Pull global options that appear *after* the subcommand out of ``raw_args``.

    Returns ``(overrides, remaining_args)``. Recognizes ``--json``, ``--no-spawn``
    (flags), and ``--home VALUE`` / ``--verbosity|-V VALUE`` /
    ``--poll-interval VALUE`` (value options), so a global flag is honored
    whether it precedes or follows the subcommand.
    """
    overrides: dict[str, object] = {}
    rest: list[str] = []
    i = 0
    while i < len(raw_args):
        token = raw_args[i]
        if token == "--json":
            overrides["json"] = True
        elif token == "--no-spawn":
            overrides["no_spawn"] = True
        elif token == "--no-onboarding":
            overrides["no_onboarding"] = True
        elif token in ("--home", "--verbosity", "-V", "--poll-interval"):
            if token == "--poll-interval":
                key = "poll_interval"
            elif token in ("--verbosity", "-V"):
                key = "verbosity"
            else:
                key = "home"
            if i + 1 >= len(raw_args):
                raise CliError(f"{token} needs a value") from None
            if key == "poll_interval":
                try:
                    overrides[key] = float(raw_args[i + 1])
                except ValueError:
                    raise CliError("--poll-interval must be a number") from None
            else:
                overrides[key] = raw_args[i + 1]
            i += 1
        else:
            rest.append(token)
        i += 1
    return overrides, rest


def _fail(exc: CliError) -> None:
    typer.echo(f"error: {exc.message}", err=True)
    raise typer.Exit(code=exc.exit_code)


# --------------------------------------------------------------------------- #
# First-run onboarding (F147 §7, §11).
# --------------------------------------------------------------------------- #

def _maybe_onboard(
    handle: sidecar.SidecarHandle,
    *,
    json_mode: bool,
    no_onboarding: bool,
    command_name: str | None,
) -> None:
    """Print the first-run welcome to stderr when the store is unconfigured.

    Gated cheaply BEFORE any network probe so a ``--json`` / non-interactive /
    opted-out / ``connect`` invocation costs nothing (golden invariant #3 —
    onboarding never blocks the scriptable surface). The definitive decision
    (including the provider probe) lives in the pure, unit-tested
    :func:`onboarding.evaluate`.
    """
    from . import onboarding
    from .commands._mutate import is_interactive

    opted = onboarding.opted_out(no_onboarding)
    interactive = is_interactive()
    # Cheap gate first — no network probe for a --json / non-interactive / opted
    # invocation (invariant #3). `evaluate` still makes the definitive decision
    # (and skips setup commands like `connect`).
    if opted or json_mode or not interactive:
        return
    with SidecarClient(handle.base_url) as client:
        text = onboarding.evaluate(
            client,
            interactive=interactive,
            json_mode=json_mode,
            opted=opted,
            command=command_name,
            home=config.resolve_home(_G.home),
        )
    if text:
        typer.echo(text, err=True)


# --------------------------------------------------------------------------- #
# REPL launch.
# --------------------------------------------------------------------------- #

def _launch_repl() -> None:
    from . import repl  # deferred: prompt_toolkit imported only when needed

    home = config.resolve_home(_G.home)
    verbosity = Verbosity(level=resolve_level(_G.verbosity))
    ctx = Context.build(
        home_override=_G.home, verbosity=verbosity, poll_interval=_G.poll_interval,
        cwd=Path.cwd(),
    )
    try:
        handle = sidecar.resolve(
            home, allow_spawn=not _G.no_spawn, our_commit=config.build_commit()
        )
    except CliError as exc:
        typer.echo(f"error: {exc.message}", err=True)
        raise typer.Exit(code=exc.exit_code)
    ctx.handle = handle
    _maybe_onboard(handle, json_mode=_G.json, no_onboarding=_G.no_onboarding,
                   command_name=None)
    with SidecarClient(handle.base_url) as client:
        repl.run_repl(ctx, client, cwd=Path.cwd())


# Register argv commands at import time so `errorta --help` lists them.
_register_argv_commands()


def main() -> None:
    """``console_scripts`` entry point.

    Special-cases the frozen self-re-exec: a frozen ``errorta __serve__`` must
    run the embedded sidecar without going through Typer's option parsing.
    """
    if len(sys.argv) >= 2 and sys.argv[1] == "__serve__":
        serve.run()
        return
    app()


if __name__ == "__main__":
    main()
