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
    """Per-invocation global options captured on the root callback.

    R7: a FRESH instance is created per ``app()`` invocation and threaded through
    the dispatch path via Click's per-invocation ``ctx.obj`` (see
    :func:`ctx.ensure_object` below), rather than mutating a shared module-level
    singleton. Two independent invocations therefore never share this state.
    """

    home: Optional[str] = None
    verbosity: Optional[str] = None
    no_spawn: bool = False
    json: bool = False
    poll_interval: Optional[float] = None
    no_onboarding: bool = False
    extra: dict[str, object] = field(default_factory=dict)


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
    # Per-invocation state: Click hands each subcommand a child context that
    # inherits `obj`, so setting it here threads a fresh `_Globals` down to the
    # subcommand handlers without a shared module global (R7).
    g = ctx.ensure_object(_Globals)
    g.home = home
    g.verbosity = verbosity
    g.no_spawn = no_spawn
    g.json = json_out
    g.poll_interval = poll_interval
    g.no_onboarding = no_onboarding
    if ctx.invoked_subcommand is None:
        _launch_repl(g)
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
def _sidecar_status(ctx: typer.Context) -> None:
    g = ctx.ensure_object(_Globals)
    home = config.resolve_home(g.home)
    info = sidecar.status(home)
    if g.json:
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
def _sidecar_stop(ctx: typer.Context) -> None:
    g = ctx.ensure_object(_Globals)
    home = config.resolve_home(g.home)
    result = sidecar.stop(home)
    typer.echo(_json.dumps(result, default=str) if g.json else _stop_line(result))


@sidecar_app.command("restart")
def _sidecar_restart(ctx: typer.Context) -> None:
    g = ctx.ensure_object(_Globals)
    home = config.resolve_home(g.home)
    try:
        handle = sidecar.restart(home, our_commit=config.build_commit())
    except CliError as exc:
        _fail(exc)
        return
    if g.json:
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
        g = ctx.ensure_object(_Globals)
        _run_registry_command(command_name, list(ctx.args), g)

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


def _run_registry_command(name: str, raw_args: list[str], g: _Globals) -> None:
    """Resolve the sidecar, dispatch through the shared registry, print, exit.

    ``g`` is this invocation's :class:`_Globals` (threaded from the root callback
    via ``ctx.obj``), so no module-global state is read here (R7).
    """
    # Global options work in either position: before the subcommand (parsed by
    # the callback into `g`) or after it (in `raw_args`). Reconcile both here so
    # `errorta status --no-spawn` behaves like `errorta --no-spawn status`.
    try:
        post, raw_args = _extract_post_globals(raw_args, registry.get(name))
    except CliError as exc:
        _fail(exc)
        return
    home_override = post.get("home", g.home)
    verbosity_raw = post.get("verbosity", g.verbosity)
    no_spawn = g.no_spawn or post.get("no_spawn", False)
    json_mode = g.json or post.get("json", False)
    poll_interval = post.get("poll_interval", g.poll_interval)
    no_onboarding = g.no_onboarding or post.get("no_onboarding", False)

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
                   command_name=name, g=g)

    # `--watch` on a read command re-renders on the poll loop (never in --json/CI).
    if not json_mode:
        from . import watch as _watch

        decision = _watch.maybe_run_watch(name, ctx, raw_args)
        raw_args = decision.raw_args
        if decision.note:
            typer.echo(decision.note, err=True)
        if decision.handled:
            with SidecarClient(handle.base_url, token=handle.token) as client:
                try:
                    _watch.run_watch(name, client, ctx, raw_args)
                except KeyboardInterrupt:
                    pass
                except CliError as exc:
                    # e.g. `cancel --watch` — a mutating command rejects the loop.
                    _fail(exc)
            return

    with SidecarClient(handle.base_url, token=handle.token) as client:
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


def _extract_post_globals(
    raw_args: list[str], command: "registry.Command | None" = None
) -> tuple[dict[str, object], list[str]]:
    """Pull global options that appear *after* the subcommand out of ``raw_args``.

    Returns ``(overrides, remaining_args)``. Recognizes ``--json``, ``--no-spawn``
    (flags), and ``--home VALUE`` / ``--verbosity|-V VALUE`` /
    ``--poll-interval VALUE`` (value options), so a global flag is honored
    whether it precedes or follows the subcommand.

    R1 disambiguation: a global-looking token is NOT harvested when it is the VALUE
    of one of the subcommand's own value-options. Given ``command``, a value-option
    (``--name``) is passed through together with the token that follows it, so
    ``errorta log --grep --json`` keeps ``--json`` as the grep pattern instead of
    letting it be eaten as the global ``--json``. A global-looking token is also
    preserved when it fills a still-missing required positional. With
    ``command=None`` the old, schema-blind behavior is preserved for direct callers.
    """
    value_opts = (
        {f"--{p.name}": p for p in command.params if not p.is_flag}
        if command is not None
        else {}
    )
    positionals = (
        [p for p in command.params if not p.is_flag]
        if command is not None
        else []
    )
    overrides: dict[str, object] = {}
    rest: list[str] = []
    pos_i = 0
    filled_positionals: set[str] = set()
    i = 0
    while i < len(raw_args):
        token = raw_args[i]
        if token in value_opts:
            # A subcommand value-option owns the token that follows it; keep both so
            # a global-named value (`--home`, `--json`, …) isn't misread as a global.
            rest.append(token)
            if i + 1 < len(raw_args):
                rest.append(raw_args[i + 1])
                filled_positionals.add(value_opts[token].name)
                i += 1
            i += 1
            continue
        while (
            pos_i < len(positionals)
            and positionals[pos_i].name in filled_positionals
        ):
            pos_i += 1
        required_positional = (
            pos_i < len(positionals) and positionals[pos_i].required
        )
        if token.startswith("--") and required_positional:
            rest.append(token)
            filled_positionals.add(positionals[pos_i].name)
            pos_i += 1
            i += 1
            continue
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
            if not token.startswith("--") and pos_i < len(positionals):
                filled_positionals.add(positionals[pos_i].name)
                pos_i += 1
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
    g: _Globals,
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
    with SidecarClient(handle.base_url, token=handle.token) as client:
        text = onboarding.evaluate(
            client,
            interactive=interactive,
            json_mode=json_mode,
            opted=opted,
            command=command_name,
            home=config.resolve_home(g.home),
        )
    if text:
        typer.echo(text, err=True)


# --------------------------------------------------------------------------- #
# REPL launch.
# --------------------------------------------------------------------------- #

def _launch_repl(g: _Globals) -> None:
    from . import repl  # deferred: prompt_toolkit imported only when needed

    home = config.resolve_home(g.home)
    verbosity = Verbosity(level=resolve_level(g.verbosity))
    ctx = Context.build(
        home_override=g.home, verbosity=verbosity, poll_interval=g.poll_interval,
        cwd=Path.cwd(),
    )
    try:
        handle = sidecar.resolve(
            home, allow_spawn=not g.no_spawn, our_commit=config.build_commit()
        )
    except CliError as exc:
        typer.echo(f"error: {exc.message}", err=True)
        raise typer.Exit(code=exc.exit_code)
    ctx.handle = handle
    _maybe_onboard(handle, json_mode=g.json, no_onboarding=g.no_onboarding,
                   command_name=None, g=g)
    with SidecarClient(handle.base_url, token=handle.token) as client:
        repl.run_repl(ctx, client, cwd=Path.cwd())


# --------------------------------------------------------------------------- #
# Explicit, idempotent registration (R7).
# --------------------------------------------------------------------------- #

# Guards the one-time build of the argv (Typer) surface. Registry population is
# guarded separately in `registry.ensure_registered()`.
_ARGV_REGISTERED = False


def ensure_registered() -> None:
    """Populate the registry and materialize the argv commands (idempotent).

    R7: registration is EXPLICIT — called from :func:`main` and (for the registry
    half) the REPL entry — not an import side effect. Importing this module no
    longer builds the Typer surface. Calling twice is safe: ``registry.
    ensure_registered`` self-guards, and ``_ARGV_REGISTERED`` prevents re-adding
    the argv subcommands (Typer would otherwise accumulate duplicate entries).
    Registers the FULL registry set, so the registry-parity and packaging-parity
    tests still see every command.
    """
    global _ARGV_REGISTERED
    registry.ensure_registered()
    if _ARGV_REGISTERED:
        return
    _register_argv_commands()
    _ARGV_REGISTERED = True


def main() -> None:
    """``console_scripts`` entry point.

    Special-cases the frozen self-re-exec: a frozen ``errorta __serve__`` must
    run the embedded sidecar without going through Typer's option parsing.
    """
    if len(sys.argv) >= 2 and sys.argv[1] == "__serve__":
        serve.run()
        return
    ensure_registered()
    app()


if __name__ == "__main__":
    main()
