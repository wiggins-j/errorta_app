"""S3 run-control commands: ``setup`` / ``run`` / ``cancel`` / ``resume`` /
``continue`` — the first MUTATING slice (F147 §8, §8.2, §12).

Every command here is registered ONCE in the shared registry, so it works
identically as ``errorta <cmd>`` and ``/<cmd>`` (golden invariant #3). Each
mutation:

* calls :func:`errorta_cli.commands._mutate.guard_sole_owner` before writing —
  defense-in-depth against a foreign desktop app co-driving the store (#5);
* is gated behind an interactive ``y/N`` or ``--yes`` (#7) — a run spends real
  model budget, so a script must opt in explicitly;
* sends the origin header on every request (the ``SidecarClient`` attaches it
  universally — #2).

Routes are grounded against the real ``coding.py`` (line refs inline). The
``run`` command additionally STREAMS the live view (reusing the S2 poller +
renderers) until the run reaches a terminal state, then sets the process exit
code by the terminal ``stop_reason`` class (:mod:`errorta_cli.runstream`).
"""
from __future__ import annotations

import json as _json
from typing import Any

from .. import runstream, teamdraft
from ..client import SidecarClient
from ..errors import EXIT_RUN_FAILED, CliError, PreflightFailed, SetupRequired
from ..registry import Command, Param, register, render_json
from ..render import is_no_project, muted, no_project, render
from ..render import runctl as _rr
from ..session import Context
from . import _base, _mutate


def _post_start(client: SidecarClient, path: str, body: dict[str, Any]) -> Any:
    """POST a start route, enriching a preflight 409 with the rendered unhealthy
    list so the exit-11 error message actually shows each provider's remediation
    (coding.py:2291 ``member_health_preflight_failed``)."""
    try:
        return client.post_json(path, json=body)
    except PreflightFailed as exc:
        raise PreflightFailed(
            f"{exc.message}\n{_rr.render_preflight(exc.unhealthy)}",
            code=exc.code,
            unhealthy=exc.unhealthy,
        ) from exc

# --------------------------------------------------------------------------- #
# Body builders.
# --------------------------------------------------------------------------- #

def _parse_members(raw: Any) -> list[dict[str, Any]]:
    """Parse a ``--members`` JSON array of member dicts (advanced path)."""
    try:
        parsed = _json.loads(str(raw))
    except (ValueError, TypeError) as exc:
        raise CliError(f"--members must be a JSON array: {exc}", code="bad_members")
    if not isinstance(parsed, list) or not all(isinstance(m, dict) for m in parsed):
        raise CliError("--members must be a JSON array of member objects",
                       code="bad_members")
    return parsed


def _team_body(args: dict[str, Any]) -> dict[str, Any]:
    """Build the ``{members?|room_id?}`` team body from ``--room`` / ``--members``.

    Empty when neither is given — a fresh ``/run`` then 400s (surfaced), while
    resume/continue recover the saved team from ``run_config`` (coding.py:2256).
    """
    body: dict[str, Any] = {}
    if args.get("members") is not None:
        body["members"] = _parse_members(args["members"])
    if args.get("room"):
        body["room_id"] = str(args["room"])
    return body


# (arg-name, _RunSetupConfirmBody field, coercion) — coding.py:2497.
_CONFIRM_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("room", "team_room_id", "str"),
    ("governance", "governance_mode", "str"),
    ("block-on-problems", "block_on_problems", "bool"),
    ("human-code-approval", "human_code_approval", "str"),
    ("max-review-rounds", "max_review_rounds", "int"),
    ("checkpoint-cadence", "checkpoint_cadence", "str"),
    ("checkpoint-n", "checkpoint_n", "int"),
    ("guardrail", "guardrail_enabled", "bool"),
    ("max-iterations", "max_iterations", "int"),
    ("max-model-calls", "max_model_calls", "int"),
    ("max-parallel", "max_parallel_workers", "int"),
    ("member-failure-limit", "member_failure_limit", "int"),
    ("preflight-enabled", "preflight_enabled", "bool"),
)


def _coerce(name: str, value: Any, kind: str) -> Any:
    if kind == "int":
        try:
            return int(str(value))
        except ValueError:
            raise CliError(f"--{name} must be an integer", code="bad_arg")
    if kind == "bool":
        text = str(value).strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off"):
            return False
        raise CliError(f"--{name} must be true or false", code="bad_arg")
    return str(value)


def _confirm_body(args: dict[str, Any]) -> dict[str, Any]:
    """Only the fields the user actually set — absent fields keep their current
    project value (coding.py:2497 "Every field is optional")."""
    body: dict[str, Any] = {}
    for name, field, kind in _CONFIRM_FIELDS:
        val = args.get(name)
        if val is not None:
            body[field] = _coerce(name, val, kind)
    return body


# --------------------------------------------------------------------------- #
# `setup` — the readiness gate (read / preflight / confirm).
# --------------------------------------------------------------------------- #

def _setup_call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    base = f"/coding/projects/{ctx.project_id}/run-setup"
    if args.get("preflight"):
        # POST /run-setup/preflight — a provider PROBE (no state mutation), so it
        # needs no sole-owner guard; the origin header (client) authorizes it.
        body = _team_body(args)
        return {"_preflight": client.post_json(f"{base}/preflight", json=body)}
    if args.get("confirm"):
        _mutate.guard_sole_owner(ctx)
        if not _mutate.confirm(ctx, args, "confirm run setup"):
            return {"_aborted": True}
        body = _confirm_body(args)
        return {"_confirmed": client.post_json(f"{base}/confirm", json=body)}
    return {"_setup": client.get_json(base)}


def _setup_render(payload: Any, verbosity: Any, json_mode: bool) -> str:
    if json_mode:
        return render_json(payload)
    if is_no_project(payload):
        return no_project()
    if payload.get("_aborted"):
        return render(muted("aborted — run setup not confirmed."))
    if "_preflight" in payload:
        unhealthy = (payload["_preflight"] or {}).get("unhealthy") or []
        return _rr.render_preflight(unhealthy)
    if "_confirmed" in payload:
        return render("run setup confirmed. Start with: errorta run --room <id> --yes")
    return _rr.render_setup(payload.get("_setup"))


# --------------------------------------------------------------------------- #
# `run` — start a fresh run + stream the live view to terminal.
# --------------------------------------------------------------------------- #

def _apply_autonomy(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> None:
    """F151: --autonomous / --checkpoint-cadence sugar. POST a POLICY-ONLY
    run-setup/confirm before starting — the confirm merges the autonomy policy
    (preserves team + other knobs) and marks setup confirmed. Never bundles the
    team (a bare policy confirm must not touch it)."""
    body: dict[str, Any] = {}
    if args.get("autonomous"):
        body["checkpoint_cadence"] = "off"
    if args.get("checkpoint-cadence"):
        body["checkpoint_cadence"] = str(args["checkpoint-cadence"])
    if not body:
        return
    client.post_json(f"/coding/projects/{ctx.project_id}/run-setup/confirm", json=body)


def _run_call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    _mutate.guard_sole_owner(ctx)
    if not _mutate.confirm(ctx, args, "start a run"):
        return {"_aborted": True}
    _apply_autonomy(client, ctx, args)
    body = _team_body(args)
    # A fresh /run REQUIRES a team in the request — the engine only recovers the
    # saved team from run_config on resume/continue, not a fresh start
    # (coding.py:2333). So when the user didn't pass --members/--room, fall back
    # to the team they assembled with `team set` / `team apply` (the CLI-local
    # draft), which is exactly the shape /run wants.
    if "members" not in body and "room_id" not in body:
        draft = teamdraft.load(ctx.home, ctx.project_id)
        if draft.get("members"):
            body["members"] = draft["members"]
        elif draft.get("room_id"):
            body["room_id"] = str(draft["room_id"])
    # POST /coding/projects/{id}/run (coding.py:2445). Typed 409s (preflight /
    # setup-required / run-in-progress) are raised by the client before we stream.
    try:
        _post_start(client, f"/coding/projects/{ctx.project_id}/run", body)
    except SetupRequired as exc:
        # The engine's message is GUI-oriented ("Open Run setup"); point a CLI
        # user at the actual command that confirms setup.
        raise SetupRequired(
            "run setup isn't confirmed yet. Confirm it first:\n"
            "  errorta team apply --yes        (applies your team + confirms setup)\n"
            "  errorta setup --confirm --yes   (confirm with current settings)\n"
            "then `errorta run`.",
            code=exc.code,
        ) from exc
    except CliError as exc:
        # Re-message the engine's raw "no members" 400 with CLI guidance (the
        # draft fallback above handles the normal `team set`/`team apply` flow;
        # this only fires when the project truly has no team).
        if "no members" in str(getattr(exc, "message", "") or exc):
            raise CliError(
                "no team set for this project. Assemble one first:\n"
                "  errorta team set pm <route>\n"
                "  errorta team set dev <route>\n"
                "(routes come from `errorta connect status`), then `errorta run`. "
                "Or pass --members / --room, or run `errorta wizard`.",
                code="no_team",
            ) from exc
        raise
    if args.get("detach"):
        return {"_detach": True, "project_id": ctx.project_id}
    try:
        if ctx.json_mode:
            final = runstream.block_until_terminal(client, ctx.project_id)
        else:
            final = runstream.stream_run(client, ctx)
    except KeyboardInterrupt:
        # Ctrl-C DETACHES the view; it does NOT cancel the run (§8.2).
        return {"_detached": True, "project_id": ctx.project_id}
    except runstream.RunStreamDetached:
        # Lost the sidecar mid-stream after repeated retries — the run keeps
        # going in the background. Detach gracefully (exit 0), never exit 9.
        return {"_detached": True, "project_id": ctx.project_id}
    payload: dict[str, Any] = {"_terminal": True, "run": final}
    if runstream.classify_exit(final) == EXIT_RUN_FAILED:
        payload["_exit_code"] = EXIT_RUN_FAILED
    return payload


def _run_json_view(payload: dict[str, Any]) -> dict[str, Any]:
    if is_no_project(payload):
        return {"error": "no_project"}
    if payload.get("_aborted"):
        return {"aborted": True}
    if payload.get("_detach"):
        return {"run_started": True, "project_id": payload.get("project_id")}
    if payload.get("_detached"):
        return {"detached": True, "project_id": payload.get("project_id")}
    run = payload.get("run") or {}
    state = run.get("state") or {}
    return {
        "stop_reason": runstream.terminal_stop_reason(run),
        "state": state,
        "counters": state.get("counters") or {},
        "running": bool(run.get("running")),
    }


def _run_render(payload: Any, verbosity: Any, json_mode: bool) -> str:
    if json_mode:
        return render_json(_run_json_view(payload))
    if is_no_project(payload):
        return no_project()
    if payload.get("_aborted"):
        return render(muted("aborted — run not started."))
    if payload.get("_detach"):
        return _rr.render_started(payload["project_id"], detached=True)
    if payload.get("_detached"):
        return _rr.render_detached(payload["project_id"])
    run = payload.get("run")
    reason = runstream.terminal_stop_reason(run)
    return _rr.render_run_terminal(run, reason=reason, gloss_text=runstream.gloss(reason))


# --------------------------------------------------------------------------- #
# `cancel` / `resume` / `continue`.
# --------------------------------------------------------------------------- #

def _cancel_call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    _mutate.guard_sole_owner(ctx)
    if not _mutate.confirm(ctx, args, "cancel the run"):
        return {"_aborted": True}
    # POST /run/cancel (coding.py:2484) — sets cancel_requested; observed at the
    # NEXT turn boundary, not instantly.
    return {"_cancel": client.post_json(f"/coding/projects/{ctx.project_id}/run/cancel", json={})}


def _cancel_render(payload: Any, verbosity: Any, json_mode: bool) -> str:
    if json_mode:
        return render_json(payload)
    if is_no_project(payload):
        return no_project()
    if payload.get("_aborted"):
        return render(muted("aborted — cancel not requested."))
    return render(
        "cancel requested — the run stops at its next turn boundary (not instant). "
        "errorta status to watch."
    )


def _resume_like_call(client: SidecarClient, ctx: Context, args: dict[str, Any],
                      *, path: str) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    _mutate.guard_sole_owner(ctx)
    if not _mutate.confirm(ctx, args, "resume the run"):
        return {"_aborted": True}
    _apply_autonomy(client, ctx, args)  # F151: policy re-read on the fresh worker
    body = _team_body(args)
    return {"_started": client.post_json(f"/coding/projects/{ctx.project_id}/{path}", json=body)}


def _resume_call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    # POST /run/resume (coding.py:2452) — interrupted-only; 409 "run is not
    # recoverable" / "workspace_integrity_failed" surface as LockBusy with the
    # real detail string.
    return _resume_like_call(client, ctx, args, path="run/resume")


def _continue_call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    # POST /run/continue (coding.py:2459) — F100 stopped-at-gate; 409 "run is not
    # continuable" otherwise.
    return _resume_like_call(client, ctx, args, path="run/continue")


def _make_resume_render(verb: str):
    def _render_fn(payload: Any, verbosity: Any, json_mode: bool) -> str:
        if json_mode:
            return render_json(payload)
        if is_no_project(payload):
            return no_project()
        if payload.get("_aborted"):
            return render(muted(f"aborted — run not {verb}d."))
        return render(
            f"run {verb}d — track it with: errorta status  /  errorta log --watch"
        )
    return _render_fn


# --------------------------------------------------------------------------- #
# Registration.
# --------------------------------------------------------------------------- #

_TEAM_PARAMS = (
    Param("room", "Council room id to use as the team.", is_flag=False),
    Param("members", "Advanced: JSON array of member objects (overrides --room).",
          is_flag=False),
)
_YES_PARAM = Param("yes", "Skip the confirmation prompt (required non-interactively).",
                   is_flag=True)
# F151: one-flag autonomy on run / continue / resume (sugar over the setup
# checkpoint-cadence knob — no checkpoint stops).
_AUTONOMY_PARAMS = (
    Param("autonomous", "Run without stopping at checkpoints (checkpoint-cadence off).",
          is_flag=True),
    Param("checkpoint-cadence", "Set checkpoint cadence (off|per_milestone|"
          "every_n_tasks|on_merge_ready).", is_flag=False),
)


register(Command(
    name="setup",
    help="Show / preflight / confirm run setup (the readiness gate).",
    call=_setup_call,
    render=_setup_render,
    params=(
        Param("preflight", "Probe the selected team's providers (member health).",
              is_flag=True),
        Param("confirm", "Apply the resolved config + mark run setup confirmed.",
              is_flag=True),
        _YES_PARAM,
        Param("room", "Council room id (team_room_id).", is_flag=False),
        Param("governance", "Governance mode.", is_flag=False),
        Param("block-on-problems", "Block on open problems (true/false).", is_flag=False),
        Param("human-code-approval", "Human code-approval policy.", is_flag=False),
        Param("max-review-rounds", "Max review rounds (int).", is_flag=False),
        Param("checkpoint-cadence", "Checkpoint cadence.", is_flag=False),
        Param("checkpoint-n", "Checkpoint N (int).", is_flag=False),
        Param("guardrail", "Guardrail enabled (true/false).", is_flag=False),
        Param("max-iterations", "Max iterations (int).", is_flag=False),
        Param("max-model-calls", "Max model calls (int).", is_flag=False),
        Param("max-parallel", "Max parallel workers (int).", is_flag=False),
        Param("member-failure-limit", "Member failure limit (int).", is_flag=False),
        Param("preflight-enabled", "Member-health preflight on/off (true/false).",
              is_flag=False),
    ),
    mutating=True,
))

register(Command(
    name="run",
    help="Start a fresh run and stream the live view until it finishes.",
    call=_run_call,
    render=_run_render,
    params=(
        *_TEAM_PARAMS,
        *_AUTONOMY_PARAMS,
        _YES_PARAM,
        Param("detach", "Fire the run and return immediately (no live stream).",
              is_flag=True),
    ),
    mutating=True,
))

register(Command(
    name="cancel",
    help="Request cancellation of the running run (observed next turn boundary).",
    call=_cancel_call,
    render=_cancel_render,
    params=(_YES_PARAM,),
    mutating=True,
    aliases=("stop",),
))

register(Command(
    name="resume",
    help="Resume an interrupted run (recovers its saved team).",
    call=_resume_call,
    render=_make_resume_render("resume"),
    params=(*_TEAM_PARAMS, *_AUTONOMY_PARAMS, _YES_PARAM),
    mutating=True,
))

register(Command(
    name="continue",
    help="Continue a run that stopped at a governance gate (F100).",
    call=_continue_call,
    render=_make_resume_render("continue"),
    params=(*_TEAM_PARAMS, *_AUTONOMY_PARAMS, _YES_PARAM),
    mutating=True,
))
