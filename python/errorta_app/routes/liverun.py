"""``/liverun`` — the operator control surface for the live-run supervisor.

A live run is owned by the **sidecar process**, not by whoever asked for it: the
supervisor thread, its evidence bundle, its teardown and its fix loop all live in
``errorta_liverun.supervisor.live_run_manager``, the same module-level singleton
the Slack bridge reaches through (``errorta_slack.tools``). These routes are a
second door onto that one manager, so ``errorta liverun start osrs`` and
``start_live_run osrs`` in Slack address the *same* run — one can stop what the
other started, and neither owns a second copy of the state machine.

That is also why every handler imports the manager **lazily**, through
:func:`_manager`. Importing ``errorta_liverun`` at module scope would pull the
supervisor (and, transitively, the tunnel/remote-runner packages) into every
sidecar boot just to register a router, and it would deny tests the seam they
need to drive these routes against a fake manager with no profiles on disk.

Trust model, mirroring ``routes/coding.py``: loopback only, a trusted
``x-errorta-origin`` (``tauri-ui`` *or* ``cli`` — the CLI is a first-class caller
here, that is the whole point of the module) plus the R3 bearer token, and a
fail-closed residency refusal. The residency guard covers the **reads** too, not
just the mutations: a live run is a local-disk data plane end to end (profiles,
``events.jsonl``, the evidence bundle, the pause markers), so under remote
residency there is nothing here that could be answered honestly — a profile list
read off the laptop would describe runs the remote box will never see.

Profile names are validated against the same shape ``LiveRunManager`` enforces
(``^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$``) and refused with 422 *before* the manager
is touched: the name is interpolated into filesystem paths (the profile YAML, the
pause markers), so it is checked at the edge rather than trusted inward.

A ``start`` the manager *refuses* is still a 200 carrying the manager's own
``{"status": "refused", "reason": ...}`` dict. The refusal reasons
(``already_running``, ``project_has_live_run``, ``profile_invalid:*``, a caps
verdict) are the answer the operator asked for, not a transport error — turning
them into a 4xx would strip the reason down to an error string and lose the
``run_id`` an ``already_running`` refusal carries.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ._residency_proxy import refuse_local_dataplane_if_remote

router = APIRouter(prefix="/liverun", tags=["liverun"])

# Kept in lockstep with ``errorta_liverun.supervisor._PROFILE_NAME_RE`` (which is
# unbounded); the length cap is this edge's own, because the name reaches the
# filesystem from here.
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# An operator's free-text stop note, capped and folded into ONE line before it is
# recorded. See :func:`_stop_reason` for why it is never used as the reason itself.
_REASON_MAX = 120


# --------------------------------------------------------------------------- #
# Lazy seams. Each is a function (not a module-level import) so the supervisor
# package is imported on first use, and so a test can substitute a fake.
# --------------------------------------------------------------------------- #

def _manager() -> Any:
    """The process-wide live-run manager — the one Slack drives."""
    from errorta_liverun.supervisor import live_run_manager

    return live_run_manager


def _list_profiles() -> list[dict[str, Any]]:
    from errorta_liverun.profile import list_profiles

    return list_profiles()


def _marker_state(profile_name: str) -> dict[str, bool]:
    """The two operator holds, read off disk for ``profile_name``.

    ``paused`` is the hard hold (:func:`errorta_liverun.supervisor.paused_marker`)
    that refuses every *start* until a human resumes; ``fix_paused`` is the
    narrower hold that keeps a run going but refuses the fix loop. A live
    snapshot already reports ``fix_paused``; this fills both in for the case that
    matters most to an operator — the run is *gone* and they need to know which
    hold is the reason nothing will start.
    """
    from errorta_liverun.supervisor import fix_paused_marker, paused_marker

    return {"paused": paused_marker(profile_name).exists(),
            "fix_paused": fix_paused_marker(profile_name).exists()}


# --------------------------------------------------------------------------- #
# Guards.
# --------------------------------------------------------------------------- #

def _require_tauri_origin(request: Request) -> None:
    """Trusted loopback origin (``tauri-ui`` OR ``cli``) + the R3 bearer token.

    The shared guard rather than a ``tauri-ui``-only one: the CLI is the intended
    caller of this router, not a tolerated one.
    """
    from errorta_app.origin import require_ui_or_cli_origin

    require_ui_or_cli_origin(request)


def _guard(request: Request, path: str) -> None:
    _require_tauri_origin(request)
    refuse_local_dataplane_if_remote(path)


def _profile_name(raw: Any, *, required: bool) -> Optional[str]:
    """Validate a profile name, or 422. ``None``/blank is allowed when optional."""
    text = str(raw or "").strip()
    if not text:
        if required:
            raise HTTPException(
                status_code=422,
                detail={"code": "bad_profile_name", "message": "a profile name is required"},
            )
        return None
    if not _PROFILE_NAME_RE.match(text):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "bad_profile_name",
                "message": (
                    "a profile name must start alphanumeric and contain only "
                    "letters, digits, '.', '_' or '-' (max 64 characters)"
                ),
            },
        )
    return text


def _stop_reason(raw: Any) -> str:
    """Build the recorded stop reason from an operator's optional note.

    The note is never the reason *itself*. ``Supervisor`` decides whether a stop
    is a bug worth an autonomous fix cycle by matching the reason against
    ``^(stall|launch_step_failed):`` — so a raw operator note of ``"stall: it hung"``
    would talk the fix loop into starting a cycle for a stop a human ordered. The
    note is therefore always carried *inside* an ``operator_stop`` reason, which
    that pattern can never match, and folded to one capped line so it stays a
    single readable field in ``events.jsonl``.
    """
    note = " ".join(str(raw or "").split())[:_REASON_MAX]
    return f"operator_stop:{note}" if note else "operator_stop"


# --------------------------------------------------------------------------- #
# Bodies.
# --------------------------------------------------------------------------- #

class StartBody(BaseModel):
    profile: str = Field(min_length=1, max_length=64)
    project_id: Optional[str] = Field(default=None, max_length=128)


class StopBody(BaseModel):
    profile: Optional[str] = Field(default=None, max_length=64)
    project_id: Optional[str] = Field(default=None, max_length=128)
    reason: Optional[str] = Field(default=None, max_length=512)


class ProfileBody(BaseModel):
    profile: str = Field(min_length=1, max_length=64)


# --------------------------------------------------------------------------- #
# Routes.
# --------------------------------------------------------------------------- #

@router.get("/profiles")
def get_profiles(request: Request) -> dict:
    """Every authored profile with its validity verdict.

    Invalid profiles are listed *with the failing rule* rather than hidden — an
    operator's first question after a refused start is which rule the YAML broke.
    """
    _guard(request, "/liverun/profiles")
    return {"profiles": _list_profiles()}


@router.post("/start")
def post_start(request: Request, body: StartBody) -> dict:
    """Launch ``profile``'s steps and hand the run to a supervisor thread.

    Returns the manager's verdict verbatim — ``{"status": "started", "run_id": ...}``
    or a ``refused`` dict with the reason (see the module docstring).
    """
    _guard(request, "/liverun/start")
    name = _profile_name(body.profile, required=True)
    project_id = (body.project_id or "").strip() or None
    return _manager().start(name, project_id=project_id)


@router.post("/stop")
def post_stop(request: Request, body: StopBody) -> dict:
    """Stop the addressed live run: evidence, then teardown, then the literals.

    With neither ``profile`` nor ``project_id`` the manager addresses the most
    recently started live run — the single-run case the sidecar is operated in.
    ``{"status": "empty"}`` when there is nothing live to stop.
    """
    _guard(request, "/liverun/stop")
    name = _profile_name(body.profile, required=False)
    project_id = (body.project_id or "").strip() or None
    return _manager().stop(profile_name=name, project_id=project_id,
                           reason=_stop_reason(body.reason))


@router.get("/status")
def get_status(
    request: Request,
    profile: Optional[str] = Query(default=None),
    project_id: Optional[str] = Query(default=None),
) -> dict:
    """The live snapshot: phase, reason, elapsed, per-probe last-ok age, caps,
    literals, fix-cycle headroom — plus the two operator holds read off disk.

    ``fix_cycles_today`` and ``fix_paused`` come from a live snapshot; both are
    filled in here for the ``empty`` case as well, because "nothing is running"
    and "nothing *can* run, it is paused" are different answers.
    """
    _guard(request, "/liverun/status")
    name = _profile_name(profile, required=False)
    pid = (project_id or "").strip() or None
    result = dict(_manager().status(profile_name=name, project_id=pid) or {})

    # Name the profile the holds belong to: the caller's selector, else the one
    # the snapshot (or the last remembered run) reports.
    last = result.get("last") if isinstance(result.get("last"), dict) else {}
    holds_for = name or result.get("profile") or (last or {}).get("profile_name")
    result.setdefault("fix_cycles_today", None)
    # Re-check the shape even though the manager only ever runs validated names:
    # `holds_for` may come from remembered run state, and it is about to be
    # interpolated into a path. A read is not a write, but the rule should not
    # depend on which of the two this happens to be.
    if holds_for and _PROFILE_NAME_RE.match(str(holds_for)):
        result.setdefault("profile", holds_for)
        for key, value in _marker_state(str(holds_for)).items():
            result.setdefault(key, value)
    else:
        result.setdefault("paused", False)
        result.setdefault("fix_paused", False)
    return result


@router.post("/resume")
def post_resume(request: Request, body: ProfileBody) -> dict:
    """Clear the hard ``paused_awaiting_human`` hold on ``profile``.

    Human-only in Slack (``HUMAN_ONLY_VERBS``) because the hold exists precisely
    so a person looks first; at this door the operator IS the person, and the
    CLI's own ``--yes`` gate is what makes the intent explicit.
    """
    _guard(request, "/liverun/resume")
    return _manager().resume(_profile_name(body.profile, required=True))


@router.post("/fix/pause")
def post_fix_pause(request: Request, body: ProfileBody) -> dict:
    """Stop ``profile`` fixing: no new cycle, and an in-flight one is asked to
    abort. Live runs keep running — this is a hold on autonomous merging."""
    _guard(request, "/liverun/fix/pause")
    return _manager().pause_fix(_profile_name(body.profile, required=True))


@router.post("/fix/resume")
def post_fix_resume(request: Request, body: ProfileBody) -> dict:
    """Re-arm ``profile``'s fix loop."""
    _guard(request, "/liverun/fix/resume")
    return _manager().resume_fix(_profile_name(body.profile, required=True))


__all__ = ["router"]
