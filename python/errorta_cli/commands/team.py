"""``team`` — assemble + apply the coding team (F147 §7.2).

**Team source of truth (the decision behind this whole command).** A coding
project stores no room and no CRUD route returns the full team — the engine keeps
it in the ledger's ``run_config.json``, written ONLY via ``POST /run-setup/confirm``
(or ``POST /run``) with an explicit ``members`` list. The one read-only projection
(``GET /model-usage``) is *derived and lossy* (drops ``coding_role`` + ``enabled``;
single-mode members carry no role). So:

* ``team`` (show) renders the CLI-local **draft** if one exists (the full members
  the CLI itself assembled), else falls back to the ``model-usage`` projection with
  a documented limitation banner.
* ``team set/pool/mode/enable/disable`` mutate the local draft — the exact
  ``members`` shape S3's ``setup --confirm`` / ``run --members`` consume
  (:mod:`errorta_cli.teamdraft`). These are LOCAL scratch edits (no HTTP, no shared
  store write) so they need no sole-owner guard.
* ``team room`` lists Council rooms (``GET /council/rooms``); ``team room <id>``
  validates + selects one into the draft (``GET /council/rooms/{id}``).
* ``team preflight`` → ``POST /run-setup/preflight`` (a member-health PROBE — no
  guard, like ``setup --preflight``).
* ``team apply`` → ``POST /run-setup/confirm`` (the shared-store write — sole-owner
  + ``--yes`` gate).
"""
from __future__ import annotations

from typing import Any

from .. import teamdraft
from ..client import SidecarClient
from ..errors import CliError
from ..registry import Command, Param, register, render_json
from ..render import is_no_project, muted, no_project, render, usage_text
from ..render import team as _rt
from ..render.runctl import render_preflight
from ..session import Context
from . import _base, _mutate


def _show(client: SidecarClient, ctx: Context) -> dict[str, Any]:
    if teamdraft.exists(ctx.home, ctx.project_id or ""):
        draft = teamdraft.load(ctx.home, ctx.project_id or "")
        return {"_kind": "show", "source": "draft", "draft": draft}
    usage = client.get_json(f"/coding/projects/{ctx.project_id}/model-usage")
    return {"_kind": "show", "source": "projection", "usage": usage}


def _edit(ctx: Context, mutate) -> dict[str, Any]:
    """Apply a local-draft mutation, persist, and return the new draft."""
    draft = teamdraft.load(ctx.home, ctx.project_id or "")
    try:
        new_draft = mutate(draft)
    except KeyError as exc:
        return _base.usage(f"no such team member/role: {exc.args[0]} (set it first)")
    teamdraft.save(ctx.home, ctx.project_id or "", new_draft)
    return {"_kind": "draft", "draft": new_draft}


def _call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    sub = str(args.get("sub") or "show").lower()
    a = args.get("a")
    b = args.get("b")

    if sub in ("show", ""):
        return _show(client, ctx)

    if sub == "set":
        if not a or not b:
            return _base.usage("team set <role> <route_id>")
        return _edit(ctx, lambda d: teamdraft.set_route(d, str(a), str(b)))

    if sub == "pool":
        if not a or not b:
            return _base.usage("team pool <role> <route,route,...>")
        routes = [r.strip() for r in str(b).split(",") if r.strip()]
        return _edit(ctx, lambda d: teamdraft.set_pool(d, str(a), routes))

    if sub == "mode":
        if not a or str(b) not in ("single", "multi"):
            return _base.usage("team mode <role> single|multi")
        return _edit(ctx, lambda d: teamdraft.set_mode(d, str(a), str(b)))

    if sub in ("enable", "disable"):
        if not a:
            return _base.usage(f"team {sub} <role>")
        return _edit(ctx, lambda d: teamdraft.set_enabled(d, str(a), sub == "enable"))

    if sub == "clear":
        teamdraft.clear(ctx.home, ctx.project_id or "")
        return {"_kind": "cleared"}

    if sub == "room":
        if not a:
            rooms = client.get_json("/council/rooms")
            return {"_kind": "rooms", "rooms": rooms}
        # Validate the room exists, then select it into the draft (local write).
        client.get_json(f"/council/rooms/{a}")
        return _edit(ctx, lambda d: teamdraft.set_room(d, str(a)))

    if sub == "preflight":
        draft = teamdraft.load(ctx.home, ctx.project_id or "")
        body = teamdraft.to_run_body(draft)
        if not body:
            return _base.usage("team preflight needs a draft (team set ... or team room <id>)")
        result = client.post_json(
            f"/coding/projects/{ctx.project_id}/run-setup/preflight", json=body
        )
        return {"_kind": "preflight", "unhealthy": (result or {}).get("unhealthy") or []}

    if sub == "apply":
        _mutate.guard_sole_owner(ctx)
        if not _mutate.confirm(ctx, args, "apply the team to run setup",
                               note="writes the team into this project's run config",
                               interactive_prompt=False):
            return {"_kind": "aborted"}
        draft = teamdraft.load(ctx.home, ctx.project_id or "")
        run_body = teamdraft.to_run_body(draft)
        if not run_body:
            raise CliError("empty team draft — nothing to apply", code="empty_draft")
        # Map the run body onto the _RunSetupConfirmBody fields (coding.py:2497):
        # `members` stays; `room_id` becomes `team_room_id`.
        confirm_body: dict[str, Any] = {}
        if "members" in run_body:
            confirm_body["members"] = run_body["members"]
        if "room_id" in run_body:
            confirm_body["team_room_id"] = run_body["room_id"]
        confirmed = client.post_json(
            f"/coding/projects/{ctx.project_id}/run-setup/confirm", json=confirm_body
        )
        return {"_kind": "applied", "confirmed": confirmed}

    return _base.usage(
        "team [show] | set <role> <route> | pool <role> <r,r> | mode <role> single|multi "
        "| enable|disable <role> | room [<id>] | preflight | apply | clear"
    )


def _render(payload: Any, verbosity: Any, json_mode: bool) -> str:
    if json_mode:
        return render_json(payload)
    if is_no_project(payload):
        return no_project()
    usage = usage_text(payload)
    if usage is not None:
        return render(muted(f"usage: {usage}"))
    kind = (payload or {}).get("_kind")
    if kind == "aborted":
        return render(muted("aborted — team not applied."))
    if kind == "cleared":
        return render(muted("team draft cleared."))
    if kind == "preflight":
        return render_preflight(payload.get("unhealthy") or [])
    if kind == "applied":
        return render("team applied to run setup. Start with: errorta run --yes")
    if kind == "rooms":
        return _rt.render_rooms(payload.get("rooms"))
    if kind == "draft":
        return _rt.render_draft(payload.get("draft"))
    if kind == "show":
        return _rt.render_show(payload)
    return render(muted("team: nothing to show"))


register(
    Command(
        name="team",
        help="Show / assemble / apply the coding team (draft → run-setup).",
        call=_call,
        render=_render,
        params=(
            Param("sub", "show|set|pool|mode|enable|disable|room|preflight|apply|clear",
                  default="show"),
            Param("a", "role / room id (subcommand arg).", default=None),
            Param("b", "route / routes / mode (subcommand arg).", default=None),
            Param("yes", "Skip the apply confirmation (required non-interactively).",
                  is_flag=True),
        ),
    )
)
