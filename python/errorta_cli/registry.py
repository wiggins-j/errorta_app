"""The single command registry — one definition per command (F147 spec §5.2).

This is the parity backbone. A :class:`Command` declares its name, help, argv
params, the route ``call`` (``call(client, ctx, args) -> payload``) and a
``render(payload, verbosity, json_mode) -> str``. **Both** front-ends — the argv
Typer app and the slash REPL — resolve a command from this ONE registry and run
it through :func:`dispatch`, so the two surfaces are identical by construction
(golden invariant #3, ``test_registry_parity``).

``--json`` is handled centrally in :func:`dispatch`: it bypasses the human
renderer and prints the raw route payload. Each command's ``render`` still
receives ``json_mode`` so a command may customize, but the default helpers below
short-circuit to JSON.
"""
from __future__ import annotations

import json as _json
import shlex
from dataclasses import dataclass
from typing import Any, Callable

from .client import SidecarClient
from .errors import CliError
from .session import Context
from .verbosity import Verbosity

# A command's route call and renderer signatures.
CallFn = Callable[[SidecarClient, Context, dict[str, Any]], Any]
RenderFn = Callable[[Any, Verbosity, bool], str]


@dataclass(frozen=True)
class Param:
    """One argv/slash parameter for a command."""

    name: str
    help: str = ""
    required: bool = False
    is_flag: bool = False
    default: Any = None


@dataclass(frozen=True)
class Command:
    """One capability, reachable identically via argv and slash."""

    name: str
    help: str
    call: CallFn
    render: RenderFn
    params: tuple[Param, ...] = ()
    # A command that WRITES run state (starts/cancels/resumes a run, confirms
    # setup). ``--watch`` is rejected on these: a watched mutation would re-fire
    # the write every poll tick and spend real model budget (F147 S3 review #3).
    mutating: bool = False
    # F151: extra names that resolve to this command (e.g. ``stop`` -> ``cancel``).
    aliases: tuple[str, ...] = ()
    # R1: a closed-arg command REJECTS unmatched tokens (``_extra``) so nothing is
    # silently lost (see :func:`reject_unconsumed_extra`). A command that
    # legitimately consumes free-form tokens (e.g. ``pm`` reads ``-i`` out of
    # ``_extra``) opts OUT of that check with ``allow_extra=True``.
    allow_extra: bool = False
    # F151: how ``--watch`` renders. "snapshot" (default) = full re-render + clear
    # each tick (status/tasks/…); "stream" = tail (append only new events; log).
    watch_mode: str = "snapshot"
    # F158: a command with BOTH tail-able and snapshot sub-verbs (e.g. `pm chat`
    # streams, `pm changes` snapshots) sets this to pick the mode from the
    # resolved args; default returns the static ``watch_mode``.
    watch_mode_fn: Callable[[dict[str, Any]], str] | None = None
    # F158: stream-mode hooks so a command supplies its own tail extractor +
    # per-entry renderer; default (None) uses the team-log implementation.
    stream_entries_fn: Callable[[Any], list] | None = None
    stream_render_fn: Callable[[list], list[str]] | None = None

    def watch_mode_for(self, args: dict[str, Any]) -> str:
        """Resolve the watch mode for THIS invocation (sub-verb aware)."""
        if self.watch_mode_fn is not None:
            return self.watch_mode_fn(args)
        return self.watch_mode


# --------------------------------------------------------------------------- #
# Registry storage.
# --------------------------------------------------------------------------- #

_REGISTRY: dict[str, Command] = {}
# F151: alias -> canonical name. Kept SEPARATE from _REGISTRY so all_commands() /
# names() stay canonical (no duplicate entries, no double-dispatch in the parity
# tests that loop every command).
_ALIASES: dict[str, str] = {}


def register(command: Command) -> Command:
    """Register a command (idempotent replace by name)."""
    _REGISTRY[command.name] = command
    for alias in command.aliases:
        _ALIASES[alias] = command.name
    return command


def get(name: str) -> Command | None:
    cmd = _REGISTRY.get(name)
    if cmd is not None:
        return cmd
    canonical = _ALIASES.get(name)
    return _REGISTRY.get(canonical) if canonical else None


def aliases() -> dict[str, str]:
    """Alias -> canonical-name map (a copy)."""
    return dict(_ALIASES)


def all_commands() -> tuple[Command, ...]:
    return tuple(_REGISTRY[name] for name in sorted(_REGISTRY))


def names() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


# --------------------------------------------------------------------------- #
# Shared parsing — the same for both front-ends.
# --------------------------------------------------------------------------- #

def split_slash(line: str) -> tuple[str, list[str]]:
    """Parse a REPL line ``/name arg1 --flag`` into ``(name, raw_args)``.

    A leading ``/`` is optional. Tokenization is quote-aware (:func:`shlex.split`)
    so a multi-word argument stays one token — ``/pm ask "fix the login bug"`` ->
    ``("pm", ["ask", "fix the login bug"])``. Unbalanced quotes (a typo) fall back
    to plain whitespace splitting rather than crashing the REPL.
    """
    text = line.strip()
    if text.startswith("/"):
        text = text[1:]
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    if not parts:
        return "", []
    return parts[0], parts[1:]


def extract_json_flag(
    raw_args: list[str], command: Command | None = None
) -> tuple[bool, list[str]]:
    """Strip a global ``--json`` without stealing a command-owned value."""
    if command is None:
        if "--json" in raw_args:
            return True, [a for a in raw_args if a != "--json"]
        return False, list(raw_args)

    by_name = {f"--{p.name}": p for p in command.params}
    positionals = [p for p in command.params if not p.is_flag]
    filled_positionals: set[str] = set()
    pos_i = 0
    detected = False
    rest: list[str] = []
    i = 0
    while i < len(raw_args):
        token = raw_args[i]
        while (
            pos_i < len(positionals)
            and positionals[pos_i].name in filled_positionals
        ):
            pos_i += 1
        param = by_name.get(token)
        if param is not None:
            rest.append(token)
            if not param.is_flag and i + 1 < len(raw_args):
                rest.append(raw_args[i + 1])
                filled_positionals.add(param.name)
                i += 1
        elif token == "--json":
            required_positional = (
                pos_i < len(positionals) and positionals[pos_i].required
            )
            if required_positional:
                rest.append(token)
                filled_positionals.add(positionals[pos_i].name)
                pos_i += 1
            else:
                detected = True
        else:
            rest.append(token)
            if not token.startswith("--") and pos_i < len(positionals):
                filled_positionals.add(positionals[pos_i].name)
                pos_i += 1
        i += 1
    return detected, rest


def resolve_args(command: Command, raw_args: list[str]) -> dict[str, Any]:
    """Map ``raw_args`` onto ``command.params``.

    Flags (``--flag``) set the matching flag param True; ``--name value`` sets a
    value param; bare tokens fill the positional (non-flag) params in order.
    Unmatched extras are preserved under ``_extra`` so nothing is silently lost.
    """
    by_name = {p.name: p for p in command.params}
    flag_names = {p.name for p in command.params if p.is_flag}
    args: dict[str, Any] = {p.name: p.default for p in command.params}
    positionals = [p for p in command.params if not p.is_flag]
    filled_positionals: set[str] = set()
    extra: list[str] = []

    pos_i = 0
    i = 0
    while i < len(raw_args):
        token = raw_args[i]
        if token.startswith("--"):
            key = token[2:]
            if key in flag_names:
                args[key] = True
            elif key in by_name:
                if i + 1 < len(raw_args):
                    args[key] = raw_args[i + 1]
                    filled_positionals.add(key)
                    i += 1
                else:
                    # R1: a value-option given with no following value is a user
                    # error — surface it, don't silently coerce it to True (which
                    # a command would then treat as a truthy string). Matches the
                    # "needs a value" contract in `app._extract_post_globals`.
                    raise CliError(f"--{key} needs a value")
            else:
                extra.append(token)
        else:
            while (
                pos_i < len(positionals)
                and positionals[pos_i].name in filled_positionals
            ):
                pos_i += 1
            if pos_i < len(positionals):
                args[positionals[pos_i].name] = token
                filled_positionals.add(positionals[pos_i].name)
                pos_i += 1
            else:
                extra.append(token)
        i += 1

    if extra:
        args["_extra"] = extra
    missing = [
        p.name for p in command.params
        if p.required and args.get(p.name) in (None, "")
    ]
    if missing:
        raise CliError(f"missing required argument(s): {', '.join(missing)}")
    return args


# ``Param`` carries no type declaration (only name/help/required/is_flag/default),
# so there is no schema type to coerce a value to — value-options stay strings and
# each command coerces its own (e.g. ``turns`` reads ``int(limit)``). Optional typed
# coercion is intentionally NOT added here: inventing a type system on ``Param`` is
# out of scope and would risk a false-reject on the one parser every command shares.


def reject_unconsumed_extra(command: Command, args: dict[str, Any]) -> None:
    """Surface tokens ``resolve_args`` could not map onto ``command.params``.

    ``_extra`` collects tokens that matched no flag, no value-option, and no free
    positional slot. For a command with a closed arg set (the default) an unmatched
    token is a user error — a typo'd flag or a stray argument — so we raise instead
    of dropping it: "nothing silently lost" becomes enforced, not conventional.

    Opt-out: a command that legitimately consumes free-form tokens sets
    ``allow_extra=True`` (e.g. ``pm`` reads ``-i`` out of ``_extra``).

    ``--watch`` is exempt: it is a framework-level token owned by the watch layer
    (``watch.arm_dashboard`` injects it; self-streaming ``run`` strips it) and it
    reaches ``resolve_args`` unmatched only on commands without a ``watch`` param,
    where it is a no-op — not a user error.
    """
    if command.allow_extra:
        return
    extra = args.get("_extra")
    if not extra:
        return
    leftover = [token for token in extra if token != "--watch"]
    if leftover:
        raise CliError(
            f"unexpected argument(s) for '{command.name}': {' '.join(leftover)}"
        )


def dispatch(
    name: str,
    client: SidecarClient,
    ctx: Context,
    raw_args: list[str],
    *,
    json_mode: bool | None = None,
) -> tuple[Any, str]:
    """Look up + run a command. The ONE code path both front-ends share.

    Returns ``(payload, rendered_text)``. Raises ``KeyError`` for an unknown
    command name (front-ends translate that into a user-facing message).
    """
    command = get(name)
    if command is None:
        raise KeyError(name)
    detected_json, rest = extract_json_flag(raw_args, command)
    effective_json = detected_json if json_mode is None else json_mode
    args = resolve_args(command, rest)
    # R1: enforce "nothing silently lost" for closed-arg commands (both front-ends
    # share this path, so the check is uniform across argv and slash).
    reject_unconsumed_extra(command, args)
    # Surface the effective --json mode to the command's call (S3 run gating needs
    # it before the payload exists). Read-only for reads; mutations may branch.
    ctx.json_mode = effective_json
    payload = command.call(client, ctx, args)
    text = command.render(payload, ctx.verbosity, effective_json)
    return payload, text


def exit_code_for(payload: Any) -> int:
    """The process exit code a command wants AFTER its text is printed.

    Most commands return ``0`` (success) or raise a ``CliError`` (mapped exit
    code) instead. The S3 ``run`` command, however, must PRINT its terminal
    status (human summary or ``--json`` block) *and* exit non-zero when the run
    ended in a failure-class ``stop_reason`` — so it stamps ``_exit_code`` on its
    returned payload and the argv front-end honors it here. Returns ``0`` for any
    payload without the marker (every read command).
    """
    if isinstance(payload, dict):
        code = payload.get("_exit_code")
        if isinstance(code, int):
            return code
    return 0


# --------------------------------------------------------------------------- #
# Render helpers.
# --------------------------------------------------------------------------- #

def render_json(payload: Any) -> str:
    """Stable pretty-printed JSON of the raw route payload (``--json`` output)."""
    return _json.dumps(payload, indent=2, sort_keys=True, default=str)


# Command modules register themselves as an IMPORT side effect (each calls
# `register()` at its own import). R7: those imports are driven on an explicit,
# idempotent `ensure_registered()` call — NOT at module-import time — so importing
# `errorta_cli.registry` on its own has no side effects and the package can be
# embedded/tested without import-order or shared-state hazards. Kept importlib-
# based so a linter can't see the imports as "unused" and strip them.
import importlib as _importlib  # noqa: E402

_COMMAND_MODULES = (
    "status", "log", "decisions", "tasks", "prs", "tokens", "turns",
    "attention", "runtime", "team", "models", "governance", "pm", "gate",
    "watch",  # Spec 06 — live run dashboard (composes existing reads; no new route)
    "runctl",  # S3 — setup / run / cancel / resume / continue (mutations)
    "connect", "wizard",  # S4 — provider onboarding + conversational setup
    "project", "focus",  # S5 — lifecycle (new/import/projects/open/switch/delete)
                         #      + north-star / focus steering
    "interject", "task", "files",  # S6 — mid-run steering + file/worktree edit/accept
    "publish", "grounding", "testcfg",  # S7 — publish + grounding + test-command config
    # NB: `runtime` (S2 read) is listed once; S7 rewrote it in place with the
    # runtime-control sub-actions. It already appears in this tuple above.
)

# Guards `ensure_registered()` so repeated calls are a cheap no-op.
_REGISTERED = False


def ensure_registered() -> None:
    """Populate the registry by importing every command module (idempotent).

    Registration is an import side effect of each ``errorta_cli.commands.*`` module
    (they call :func:`register` at import). R7 drives those imports on this explicit,
    idempotent call instead of at module-import time, so importing
    ``errorta_cli.registry`` alone leaves the registry empty until a front-end
    (``app.main`` / ``repl.run_repl``) asks for it. Calling twice is a no-op — the
    first call flips ``_REGISTERED`` and Python's import cache makes any redundant
    ``import_module`` a lookup. Parity note: this imports the FULL
    ``_COMMAND_MODULES`` set (kept in lockstep with ``cli.spec``'s bundled list), so
    both the registry-parity and packaging-parity tests still see every command.
    """
    global _REGISTERED
    if _REGISTERED:
        return
    for _name in _COMMAND_MODULES:
        _importlib.import_module(f".commands.{_name}", __package__)
    _REGISTERED = True
