"""``liverun`` — drive the live-run supervisor from the terminal (no Slack).

A live run is owned by the **sidecar**, so this command owns nothing: it is a
pure client of the ``/liverun/*`` routes, which reach the same
``errorta_liverun.supervisor.live_run_manager`` the Slack bridge drives. That
matters operationally — a run started here is stoppable from Slack and vice
versa — and structurally: the CLI never imports ``errorta_liverun`` (golden
invariant #1, ``test_import_boundary``), so every fact below arrives as JSON.

::

    liverun [status] [--profile P] [--watch]  GET  /liverun/status
    liverun profiles                          GET  /liverun/profiles
    liverun start <profile> [--project P]     POST /liverun/start
    liverun stop [<profile>] [--reason R]     POST /liverun/stop
    liverun resume <profile>                  POST /liverun/resume
    liverun fix pause <profile>               POST /liverun/fix/pause
    liverun fix resume <profile>              POST /liverun/fix/resume

**The confirmation gate is asymmetric, and deliberately so.** It mirrors the
trust classes the Slack verbs already carry (``docs/liverun/README.md``): turning
autonomy *on* is gated, turning it *off* never is.

* ``start`` / ``resume`` / ``fix resume`` are gated (``--yes`` or an interactive
  yes) and re-check the sole-owner invariant first. A start launches real
  commands on a real box; ``resume`` clears a hold that exists *because*
  something ban-class or cap-class happened; ``fix resume`` re-arms autonomous
  merging. None of those should be possible by accident from a script.
* ``stop`` and ``fix pause`` are **not** gated and do not guard sole-ownership.
  Making an operator confirm a stop — or refusing their stop because some other
  Errorta app is also on this host — is the failure mode that matters here. A
  subtractive action must never be the one that is hard to perform.

``status --watch`` polls every 5 s and **ends on a terminal phase** rather than
spinning on a finished run: the payload carries ``_watch_done`` and the shared
watch loop honours it. ``--watch`` is refused outright on the mutating
sub-actions — a watched start would relaunch every tick.
"""
from __future__ import annotations

from typing import Any

from ..client import SidecarClient
from ..errors import CliError
from ..registry import Command, Param, register, render_json
from ..render import liverun as _rl
from ..render import muted, render
from ..session import Context
from . import _base, _mutate

# Sub-actions that change supervisor state. ``--watch`` is refused on all of
# them; the ones that turn autonomy ON additionally take the confirmation gate.
_MUTATING = frozenset({"start", "stop", "resume", "fix"})

# How often ``status --watch`` polls. The supervisor ticks once a second but its
# probes are minutes apart, so a faster poll only adds noise and HTTP.
WATCH_INTERVAL_S = 5.0


def _profile_of(args: dict[str, Any], positional: Any) -> str:
    """The profile this invocation addresses: ``--profile`` wins, else the
    positional. Blank is a usage error for the sub-actions that require one."""
    named = str(args.get("profile") or "").strip()
    return named or str(positional or "").strip()


# --------------------------------------------------------------------------- #
# Sub-actions.
# --------------------------------------------------------------------------- #

def _profiles(client: SidecarClient) -> dict[str, Any]:
    return {"_kind": "profiles", **(client.get_json("/liverun/profiles") or {})}


def _status(client: SidecarClient, args: dict[str, Any], positional: Any) -> dict[str, Any]:
    params: dict[str, Any] = {}
    profile = _profile_of(args, positional)
    if profile:
        params["profile"] = profile
    project = str(args.get("project") or "").strip()
    if project:
        params["project_id"] = project
    payload = dict(client.get_json("/liverun/status", params=params or None) or {})
    payload["_kind"] = "status"
    if args.get("watch") and _rl.is_terminal(payload):
        # Tell the shared watch loop to stop: the run reached a phase nothing
        # will move it out of (or there is no live run at all).
        payload["_watch_done"] = True
    return payload


def _start(client: SidecarClient, ctx: Context, args: dict[str, Any],
           profile: str) -> dict[str, Any]:
    if not profile:
        return _base.usage("liverun start <profile> [--project <project-id>]")
    _mutate.guard_sole_owner(ctx)
    if not _mutate.confirm(ctx, args, f"start the live run '{profile}'",
                           note="launches the profile's real commands on a real host"):
        return {"_kind": "aborted"}
    body: dict[str, Any] = {"profile": profile}
    project = str(args.get("project") or "").strip()
    if project:
        body["project_id"] = project
    return {"_kind": "verdict", "_verb": f"start {profile}",
            **(client.post_json("/liverun/start", json=body) or {})}


def _stop(client: SidecarClient, args: dict[str, Any], profile: str) -> dict[str, Any]:
    # No gate, by design (see the module docstring): stopping is never the hard
    # thing to do. With no selector the sidecar addresses the newest live run.
    body: dict[str, Any] = {}
    if profile:
        body["profile"] = profile
    project = str(args.get("project") or "").strip()
    if project:
        body["project_id"] = project
    reason = str(args.get("reason") or "").strip()
    if reason:
        body["reason"] = reason
    return {"_kind": "verdict", "_verb": f"stop {profile}".strip(),
            **(client.post_json("/liverun/stop", json=body) or {})}


def _resume(client: SidecarClient, ctx: Context, args: dict[str, Any],
            profile: str) -> dict[str, Any]:
    if not profile:
        return _base.usage("liverun resume <profile>")
    _mutate.guard_sole_owner(ctx)
    if not _mutate.confirm(
        ctx, args, f"clear the hold on '{profile}'",
        note="the hold was set by a ban signal or a failure cap — look at why first",
    ):
        return {"_kind": "aborted"}
    return {"_kind": "verdict", "_verb": f"resume {profile}",
            **(client.post_json("/liverun/resume", json={"profile": profile}) or {})}


def _fix(client: SidecarClient, ctx: Context, args: dict[str, Any],
         verb: str, profile: str) -> dict[str, Any]:
    if verb not in ("pause", "resume") or not profile:
        return _base.usage("liverun fix pause|resume <profile>")
    if verb == "pause":
        # Subtractive and immediate — no gate, matching `pause_fix_loop`'s R class.
        return {"_kind": "verdict", "_verb": f"fix pause {profile}",
                **(client.post_json("/liverun/fix/pause", json={"profile": profile}) or {})}
    _mutate.guard_sole_owner(ctx)
    if not _mutate.confirm(ctx, args, f"re-arm the fix loop for '{profile}'",
                           note="the loop may merge and deploy a fix on its own"):
        return {"_kind": "aborted"}
    return {"_kind": "verdict", "_verb": f"fix resume {profile}",
            **(client.post_json("/liverun/fix/resume", json={"profile": profile}) or {})}


# --------------------------------------------------------------------------- #
# Dispatch + render.
# --------------------------------------------------------------------------- #

def _call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> Any:
    action = str(args.get("action") or "").strip().lower()
    p1, p2 = args.get("p1"), args.get("p2")
    if action in _MUTATING and args.get("watch"):
        raise CliError(
            f"--watch is for read views; `liverun {action}` changes supervisor "
            "state and can't be watched (it would re-fire every tick). Run it "
            "once, then follow the run with: errorta liverun status --watch",
            code="watch_on_mutation",
        )
    if action in ("", "status"):
        return _status(client, args, p1)
    if action == "profiles":
        return _profiles(client)
    if action == "start":
        return _start(client, ctx, args, _profile_of(args, p1))
    if action == "stop":
        return _stop(client, args, _profile_of(args, p1))
    if action == "resume":
        return _resume(client, ctx, args, _profile_of(args, p1))
    if action == "fix":
        return _fix(client, ctx, args, str(p1 or "").strip().lower(),
                    _profile_of(args, p2))
    return _base.usage(
        "liverun [ status [--profile P] [--watch] | profiles | "
        "start <profile> [--project P] | stop [<profile>] [--reason R] | "
        "resume <profile> | fix pause|resume <profile> ]")


def _render(payload: Any, verbosity: Any, json_mode: bool) -> str:
    if json_mode:
        return render_json(payload)
    usage = payload.get("_usage") if isinstance(payload, dict) else None
    if usage:
        return render(muted(f"usage: {usage}"))
    kind = (payload or {}).get("_kind") if isinstance(payload, dict) else None
    if kind == "aborted":
        return render(muted("aborted — nothing changed."))
    if kind == "profiles":
        return _rl.render_profiles(payload, verbosity)
    if kind == "verdict":
        return _rl.render_verdict(payload, str(payload.get("_verb") or "liverun"))
    # Default (and the bare invocation) is the status view.
    return _rl.render_status(payload, verbosity)


register(Command(
    name="liverun",
    help="Drive the live-run supervisor: profiles, start/stop, status, fix pause/resume.",
    call=_call,
    render=_render,
    # NOT `mutating=True`: the command's READ sub-actions (status/profiles) are
    # the watchable ones, so the --watch refusal is per-sub-action in `_call`.
    mutating=False,
    watch_interval=WATCH_INTERVAL_S,
    params=(
        Param("action", "sub-action (blank = status).", default=""),
        Param("p1", "profile name (or 'pause'/'resume' after 'fix').", default=None),
        Param("p2", "profile name (after 'fix pause'/'fix resume').", default=None),
        Param("profile", "profile to address (overrides the positional).", is_flag=False),
        Param("project", "project id to address / bind the run to.", is_flag=False),
        Param("reason", "stop: an operator note recorded with the stop.", is_flag=False),
        Param("watch", "status: re-render every 5s until the run ends.", is_flag=True),
        Param("yes", "Skip the confirmation prompt (required non-interactively).",
              is_flag=True),
    ),
))
