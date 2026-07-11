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
from dataclasses import dataclass
from typing import Any, Callable

from .client import SidecarClient
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


# --------------------------------------------------------------------------- #
# Registry storage.
# --------------------------------------------------------------------------- #

_REGISTRY: dict[str, Command] = {}


def register(command: Command) -> Command:
    """Register a command (idempotent replace by name)."""
    _REGISTRY[command.name] = command
    return command


def get(name: str) -> Command | None:
    return _REGISTRY.get(name)


def all_commands() -> tuple[Command, ...]:
    return tuple(_REGISTRY[name] for name in sorted(_REGISTRY))


def names() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


# --------------------------------------------------------------------------- #
# Shared parsing — the same for both front-ends.
# --------------------------------------------------------------------------- #

def split_slash(line: str) -> tuple[str, list[str]]:
    """Parse a REPL line ``/name arg1 --flag`` into ``(name, raw_args)``.

    A leading ``/`` is optional. Simple whitespace splitting is sufficient for
    S1; quoted-argument handling is an S2 refinement.
    """
    text = line.strip()
    if text.startswith("/"):
        text = text[1:]
    parts = text.split()
    if not parts:
        return "", []
    return parts[0], parts[1:]


def extract_json_flag(raw_args: list[str]) -> tuple[bool, list[str]]:
    """Strip a global ``--json`` from ``raw_args``; return ``(json_mode, rest)``."""
    if "--json" in raw_args:
        return True, [a for a in raw_args if a != "--json"]
    return False, list(raw_args)


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
                    i += 1
                else:
                    args[key] = True
            else:
                extra.append(token)
        else:
            if pos_i < len(positionals):
                args[positionals[pos_i].name] = token
                pos_i += 1
            else:
                extra.append(token)
        i += 1

    if extra:
        args["_extra"] = extra
    return args


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
    detected_json, rest = extract_json_flag(raw_args)
    effective_json = detected_json if json_mode is None else json_mode
    args = resolve_args(command, rest)
    payload = command.call(client, ctx, args)
    text = command.render(payload, ctx.verbosity, effective_json)
    return payload, text


# --------------------------------------------------------------------------- #
# Render helpers.
# --------------------------------------------------------------------------- #

def render_json(payload: Any) -> str:
    """Stable pretty-printed JSON of the raw route payload (``--json`` output)."""
    return _json.dumps(payload, indent=2, sort_keys=True, default=str)


# Import the command modules for their registration side effects. Done at the
# bottom so the Command/registry symbols above are fully defined first.
from .commands import status as _status  # noqa: E402,F401
