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


# --------------------------------------------------------------------------- #
# F150 team builder — `create` / `add` + `--default`.
# --------------------------------------------------------------------------- #

# role flag / alias -> canonical coding role.
_ROLE_FLAGS = {
    "pm": "pm", "dev": "dev", "reviewer": "reviewer", "tester": "tester",
    "test": "tester", "prog": "dev", "programmer": "dev",
}


def _add_role_value(args: dict[str, Any], a: Any, b: Any) -> tuple[str, str]:
    """Resolve (role, value) for `team add`, from role flags OR positionals.

    flag form:  team add --dev <route>   -> role from the flag, value = a
    positional: team add dev <route>     -> role = a, value = b
    """
    flagged = sorted({_ROLE_FLAGS[f] for f in _ROLE_FLAGS if args.get(f)})
    if flagged:
        if len(flagged) > 1:
            raise CliError("pick exactly one role: --pm / --dev / --reviewer / --tester.")
        return flagged[0], (str(a).strip() if a else "")
    role = _ROLE_FLAGS.get(str(a or "").strip().lower(), str(a or "").strip().lower())
    return role, (str(b).strip() if b else "")


def _add_count(args: dict[str, Any]) -> int:
    raw = args.get("count")
    if raw is None or str(raw).strip() == "":
        return 1
    try:
        n = int(str(raw).strip())
    except ValueError as exc:
        raise CliError(f"--count must be a positive integer (got {raw!r}).") from exc
    if n < 1:
        raise CliError("--count must be >= 1.")
    return n


def _routes_index(client: SidecarClient) -> tuple[dict[str, list[str]], set[str]]:
    """({provider_class: [route_id,...]}, {all route_ids})."""
    data = client.get_json("/gateway/routes") or {}
    by_provider: dict[str, list[str]] = {}
    all_routes: set[str] = set()
    for r in data.get("routes") or []:
        rid, prov = r.get("route_id"), r.get("provider_class")
        if rid:
            all_routes.add(rid)
            if prov:
                by_provider.setdefault(prov, []).append(rid)
    return by_provider, all_routes


def _resolve_value(value: str, by_provider: dict[str, list[str]],
                   all_routes: set[str]) -> tuple[str | None, list[str] | None]:
    """value -> (route|None, pool|None). A known provider => multi pool; a known
    route id => single; else error."""
    if value in by_provider:
        pool = sorted(by_provider[value])
        if not pool:
            raise CliError(
                f"provider '{value}' has no available routes — connect it (`errorta connect`).")
        return None, pool
    if value in all_routes:
        return value, None
    raise CliError(f"unknown route or provider '{value}' — see `errorta models`.")


def _bucket(route_id: str) -> str:
    """Capability class by keyword in the route_id model suffix (not family)."""
    s = route_id.lower()
    # Light first: `mini`/`nano`/`haiku` are strong small-model signals that would
    # otherwise be swallowed by the reasoning check (e.g. `gpt-5-mini`).
    if any(k in s for k in ("haiku", "mini", "nano", ":7b", ":3b")):
        return "light"
    reasoning = (any(k in s for k in ("opus", "o1", "o3"))
                 or ("gpt-5" in s and "codex" not in s) or "-pro" in s)
    if reasoning:
        return "reasoning"
    if any(k in s for k in ("codex", "composer", "sonnet", "coder")):
        return "coding"
    return "mid"


def _pick(cands: list[tuple[str, str]], prefs: tuple[str, ...],
          exclude_provider: str | None = None) -> tuple[str, str] | None:
    """First (route_id, provider) matching the bucket preference, deterministic
    (sorted route_id tie-break). Falls back to best usable."""
    for want in prefs:
        pool = sorted((r, p) for (r, p) in cands if _bucket(r) == want and p != exclude_provider)
        if pool:
            return pool[0]
    pool = sorted((r, p) for (r, p) in cands if p != exclude_provider)
    return pool[0] if pool else None


def _assemble_default(client: SidecarClient, ctx: Context) -> dict[str, Any]:
    """F150 --default: 1 pm / 3 dev / 1 reviewer / 1 tester, models chosen from the
    user's usable providers by the documented keyword policy."""
    providers = (client.get_json("/gateway/providers") or {}).get("providers") or []
    usable = {p.get("provider_class") for p in providers
              if p.get("connected") is True or p.get("configured") is True}
    by_provider, _all = _routes_index(client)
    cands = [(rid, prov) for prov, rids in by_provider.items()
             if prov in usable for rid in rids]
    if not cands:
        raise CliError("no usable providers — run `errorta connect <provider>` first.")

    pm = _pick(cands, ("reasoning", "coding", "mid"))
    dev = _pick(cands, ("coding", "mid"))
    dev_prov = dev[1] if dev else None
    reviewer = _pick(cands, ("reasoning", "coding", "mid"), exclude_provider=dev_prov) \
        or _pick(cands, ("reasoning", "coding", "mid"))
    tester = _pick(cands, ("mid", "coding"))

    draft: dict[str, Any] = {"members": [], "room_id": None}
    plan = [("pm", pm, 1), ("dev", dev, 3), ("reviewer", reviewer, 1), ("tester", tester, 1)]
    assignments: list[dict[str, str]] = []
    for role, pick, count in plan:
        if not pick:
            continue
        rid, prov = pick
        draft = teamdraft.add_members(draft, role, count, route=rid)
        assignments.append({"role": role, "count": str(count), "route": rid,
                            "why": f"{_bucket(rid)} route from {prov}"})
    teamdraft.save(ctx.home, ctx.project_id or "", draft)
    return {"_kind": "draft", "draft": draft, "assignments": assignments}


def _call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    sub = str(args.get("sub") or "show").lower()
    a = args.get("a")
    b = args.get("b")

    if sub in ("show", ""):
        return _show(client, ctx)

    if sub == "create":
        # Note (don't silently swallow) if we're replacing a non-empty draft.
        prior = teamdraft.load(ctx.home, ctx.project_id or "")
        replaced = len(prior.get("members") or [])
        teamdraft.clear(ctx.home, ctx.project_id or "")
        if args.get("default"):
            out = _assemble_default(client, ctx)
        else:
            teamdraft.save(ctx.home, ctx.project_id or "", {"members": [], "room_id": None})
            out = {"_kind": "draft", "draft": teamdraft.load(ctx.home, ctx.project_id or "")}
        if replaced:
            out["replaced"] = replaced
        return out

    if sub == "add":
        role, value = _add_role_value(args, a, b)
        if role not in teamdraft.CODING_ROLES:
            return _base.usage("team add --<pm|dev|reviewer|tester> <route|provider> [--count N]")
        if not value:
            return _base.usage("team add --<role> <route|provider> [--count N]")
        count = _add_count(args)
        if role == "pm" and count > 1:
            raise CliError("a coding team has one PM (use --count 1).")
        by_provider, all_routes = _routes_index(client)
        route, pool = _resolve_value(value, by_provider, all_routes)
        draft = teamdraft.load(ctx.home, ctx.project_id or "")
        new_draft = teamdraft.add_members(draft, role, count, route=route, pool=pool)
        teamdraft.save(ctx.home, ctx.project_id or "", new_draft)
        return {"_kind": "draft", "draft": new_draft}

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
        "team [show] | create [--default] | add --<role> <route|provider> [--count N] "
        "| set <role> <route> | pool <role> <r,r> | mode <role> single|multi "
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
        body = _rt.render_draft(payload.get("draft"))
        replaced = payload.get("replaced")
        if replaced:
            body = render(muted(f"(replaced a {replaced}-member draft)")) + "\n" + body
        assignments = payload.get("assignments")
        if assignments:
            lines = [render(muted("default team — auto-assigned from your providers:"))]
            for asg in assignments:
                n = asg.get("count", "1")
                suffix = f" ×{n}" if n not in ("1", 1) else ""
                lines.append(render(muted(
                    f"  {asg.get('role')}{suffix}: {asg.get('route')}  ({asg.get('why')})")))
            lines.append(render(muted(
                "note: extra devs are capacity — parallel work ramps up as the PM "
                "splits the backlog.")))
            return "\n".join(lines) + "\n" + body
        return body
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
            Param("sub", "show|create|add|set|pool|mode|enable|disable|room|preflight|apply|clear",
                  default="show"),
            Param("a", "role / route / room id (subcommand arg).", default=None),
            Param("b", "route / routes / mode (subcommand arg).", default=None),
            Param("yes", "Skip the apply confirmation (required non-interactively).",
                  is_flag=True),
            # F150 team builder:
            Param("codingteam", "team create: build a coding team (the default type).",
                  is_flag=True),
            Param("default", "team create: auto-assemble a default team from your providers.",
                  is_flag=True),
            Param("count", "team add: number of members of the role (default 1).",
                  default=None),
            Param("pm", "team add: role = PM.", is_flag=True),
            Param("dev", "team add: role = developer.", is_flag=True),
            Param("reviewer", "team add: role = reviewer.", is_flag=True),
            Param("tester", "team add: role = tester.", is_flag=True),
            Param("test", "team add: alias of --tester.", is_flag=True),
            Param("prog", "team add: alias of --dev.", is_flag=True),
            Param("programmer", "team add: alias of --dev.", is_flag=True),
        ),
    )
)
