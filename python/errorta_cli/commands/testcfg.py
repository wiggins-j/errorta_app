"""``test-commands`` / ``test-settings`` / ``test-runs`` — the merge-gate suite (§8).

Grounded against the real ``coding.py`` (file:line inline):

* ``test-commands [show]``   → ``GET  .../test-commands``    (2867)
* ``test-commands set --commands <json>`` → ``PUT .../test-commands`` (2877)
* ``test-settings [show]``   → ``GET  .../test-settings``    (2898)
* ``test-settings set --require-sandbox true|false`` → ``PUT .../test-settings`` (2903)
* ``test-runs``              → ``GET  .../test-runs``        (2893)

The two ``set`` paths are ``refuse_local_dataplane_if_remote`` writes (surface as
:class:`ResidencyRefused`, exit 4) that take the sole-owner guard + ``--yes``/
confirm gate; the reads don't. NB: distinct from the ``runtime test`` action.
"""
from __future__ import annotations

import json as _json
from typing import Any

from ..client import SidecarClient
from ..errors import CliError
from ..registry import Command, Param, register, render_json
from ..render import is_no_project, muted, no_project, render
from ..render import testcfg as _rt
from ..session import Context
from . import _base, _mutate


def _p(ctx: Context, leaf: str) -> str:
    return f"/coding/projects/{ctx.project_id}/{leaf}"


def _coerce_bool(name: str, value: Any) -> bool:
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    raise CliError(f"--{name} must be true or false", code="bad_arg")


# --------------------------------------------------------------------------- #
# test-commands.
# --------------------------------------------------------------------------- #

def _test_commands_call(
    client: SidecarClient, ctx: Context, args: dict[str, Any]
) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    if str(args.get("action") or "").lower() == "set":
        raw = args.get("commands")
        if raw is None:
            return _base.usage("test-commands set --commands '<json array>'")
        try:
            parsed = _json.loads(str(raw))
        except (ValueError, TypeError) as exc:
            raise CliError(f"--commands must be JSON: {exc}", code="bad_commands")
        _mutate.guard_sole_owner(ctx)
        if not _mutate.confirm(ctx, args, "set the project test commands",
                               note="changes the merge-gate test suite"):
            return {"_kind": "aborted"}
        return {"_kind": "commands",
                **(client.put_json(_p(ctx, "test-commands"), json={"commands": parsed}) or {})}
    return {"_kind": "commands", **(client.get_json(_p(ctx, "test-commands")) or {})}


def _test_settings_call(
    client: SidecarClient, ctx: Context, args: dict[str, Any]
) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    if str(args.get("action") or "").lower() == "set":
        if args.get("require-sandbox") is None:
            return _base.usage("test-settings set --require-sandbox true|false")
        value = _coerce_bool("require-sandbox", args["require-sandbox"])
        _mutate.guard_sole_owner(ctx)
        if not _mutate.confirm(ctx, args, "change the test sandbox setting",
                               note="controls whether test runs require the sandbox"):
            return {"_kind": "aborted"}
        return {"_kind": "settings",
                **(client.put_json(_p(ctx, "test-settings"),
                                   json={"require_sandbox": value}) or {})}
    return {"_kind": "settings", **(client.get_json(_p(ctx, "test-settings")) or {})}


def _test_runs_call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    return {"_kind": "runs", **(client.get_json(_p(ctx, "test-runs")) or {})}


def _make_render(read_fn):
    def _render(payload: Any, verbosity: Any, json_mode: bool) -> str:
        if json_mode:
            return render_json(payload)
        if is_no_project(payload):
            return no_project()
        usage = payload.get("_usage") if isinstance(payload, dict) else None
        if usage:
            return render(muted(f"usage: {usage}"))
        if (payload or {}).get("_kind") == "aborted":
            return render(muted("aborted — nothing changed."))
        return read_fn(payload)
    return _render


register(Command(
    name="test-commands",
    help="Show or set the project's merge-gate test commands.",
    call=_test_commands_call,
    render=_make_render(_rt.render_test_commands),
    params=(
        Param("action", "blank = show; 'set' to write.", default=""),
        Param("commands", "set: the commands as a JSON array.", is_flag=False),
        Param("yes", "Skip the confirmation prompt (required non-interactively).",
              is_flag=True),
    ),
))

register(Command(
    name="test-settings",
    help="Show or set the project test settings (require_sandbox).",
    call=_test_settings_call,
    render=_make_render(_rt.render_test_settings),
    params=(
        Param("action", "blank = show; 'set' to write.", default=""),
        Param("require-sandbox", "set: true|false.", is_flag=False),
        Param("yes", "Skip the confirmation prompt (required non-interactively).",
              is_flag=True),
    ),
))

register(Command(
    name="test-runs",
    help="List the recorded test-command runs.",
    call=_test_runs_call,
    render=_make_render(_rt.render_test_runs),
))
