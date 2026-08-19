"""The studio's bounded tool surface — the ONLY door from the studio
concierge to project creation.

Mirrors ``errorta_slack.tools`` exactly, for the same two non-negotiable
reasons (see that module's docstring for the full rationale):

1. **Grant-or-refuse.** ``dispatch`` accepts only the verbs listed in
   ``TOOL_CATALOG``. An unknown/unlisted verb raises :class:`ToolError` with
   code ``"tool_not_allowed"`` and a message naming the real catalog — fail
   closed, never a silent no-op.
2. **Injection guard.** ``create_project`` (the studio's only **C**-class
   verb — it creates a project AND a public Slack channel) executes its real
   effect ONLY when ``dispatch`` is called with
   ``confirmed_via="block_actions"`` — the provenance marker set by the
   verified Slack interaction callback, never by concierge text output. With
   ``confirmed_via=None`` (what the studio concierge always passes when
   acting on chat text) the call is staged via ``store.stage_confirmation``
   and returns ``{"status": "needs_confirmation", "confirmation_id": ...}``
   instead of running. This is what stops untrusted pasted Slack text (e.g.
   a fabricated "confirmation id" or "yes go ahead, id=xyz" in a message)
   from creating a project and a public channel.

``TOOL_CATALOG`` is the single source of truth: the studio concierge system
prompt renders it, mirroring the Task 11 anti-drift discipline for the
per-project catalog in ``tools.py``.

This module MUST NOT import ``slack_sdk`` (or anything else optional) at
module load, and MUST NOT execute real engine/Slack side effects at import
time — every engine seam is reached through the injectable
:class:`StudioDeps`, whose callable fields default to real (but cheap to
import) implementations.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from errorta_council.coding.ledger import LedgerError, LedgerStore, list_projects
from errorta_council.coding.project_factory import create_project_from_charter
from errorta_slack import config as _config
from errorta_slack import provisioning
from errorta_slack import store as _slack_store

_LOGGER = logging.getLogger(__name__)


class ToolError(Exception):
    """Raised when a verb cannot be executed. ``.code`` is a stable string."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


# --------------------------------------------------------------------------
# Catalog — the single source of truth for verbs, trust class, and prompt copy
# --------------------------------------------------------------------------

TOOL_CATALOG: dict[str, dict[str, Any]] = {
    "list_projects": {
        "trust": "R",
        "summary": "List every coding project this studio has created.",
    },
    "create_project": {
        "trust": "C",
        "summary": (
            "Create a new coding project and its dedicated Slack channel "
            "from a gathered charter."
        ),
    },
    "answer_question": {
        "trust": "R",
        "args": (("question", True, "the question to answer"),),
        "summary": "Answer a question from context already fetched (no side effect).",
    },
    "archive_project": {
        "trust": "C",
        "args": (("project_id", True, "the project to spin down"),),
        "summary": (
            "Spin a project down — pause it and archive its Slack channel "
            "(reversible; does not delete the project)."
        ),
    },
    "adopt_project": {
        "trust": "C",
        "args": (("project_id", True, "the existing project to adopt"),
                 ("start", False, "true to also start the run")),
        "summary": (
            "Adopt an EXISTING project into Slack — open and bind its own "
            "channel (and seat a team if it has none)."
        ),
    },
}


# --------------------------------------------------------------------------
# project_id derivation — the Errorta ledger charset, never "." or ".."
# --------------------------------------------------------------------------

_ID_RUN = re.compile(r"[^a-z0-9._-]+")
_ID_DASH_RUN = re.compile(r"-{2,}")
_MAX_ID_LEN = 64


def _project_id_from_title(title: str) -> str:
    """Sanitize a human-typed project title into a safe Errorta project id.

    Distinct from ``provisioning.derive_channel_name`` (which is the Slack
    channel-name rule, its own separate charset) — this targets the ledger's
    ``^[A-Za-z0-9._-]{1,64}$`` project_id charset. Any run of characters
    outside ``[a-z0-9._-]`` collapses to a single ``-``; leading/trailing and
    repeated dashes are stripped/collapsed; the result is capped at 64 chars.
    An empty result, or one that is nothing but dots (which would resolve to
    ``"."``/``".."`` and could escape the ledger root), falls back to
    ``"project"``.
    """
    slug = _ID_RUN.sub("-", (title or "").strip().lower())
    slug = _ID_DASH_RUN.sub("-", slug).strip("-")
    slug = slug[:_MAX_ID_LEN].strip("-")
    if not slug or set(slug) <= {"."}:
        return "project"
    return slug


# --------------------------------------------------------------------------
# Deps — every engine seam ``dispatch`` reaches through, all injectable
# --------------------------------------------------------------------------


@dataclass
class StudioDeps:
    """Every engine seam ``dispatch`` reaches through — all injectable so
    tests run egress-free with fakes."""

    store: Any = _slack_store
    ledger_factory: Callable[[str], Any] = LedgerStore
    web_client: Any = None
    create_fn: Callable[..., Any] = create_project_from_charter
    list_projects_fn: Callable[[], list[dict[str, Any]]] = list_projects
    provision_fn: Callable[..., dict[str, Any]] = provisioning.create_project_channel
    provision_archive_fn: Callable[..., dict[str, Any]] = provisioning.archive_channel
    invite_user_ids: list[str] = field(default_factory=list)
    available_routes: list[dict[str, Any]] | None = None
    # fix/slack-studio-default-team: explicit team the studio hands to
    # ``create_fn`` as ``members=`` when spinning up a project, bypassing
    # ``resolve_team``/``available_routes`` (see ``create_project`` below).
    # ``None`` (the default) means "read config.load()['studio_default_team']
    # at call time"; tests inject a list here to avoid touching config.
    default_team: list[dict[str, Any]] | None = None
    # Slice 4 §3.1: `adopt_project(start=True)` starts the run through this
    # seam. `None` (not the real function) so `tools._default_start_run`'s lazy
    # `errorta_app.routes.coding` import stays deferred to first real use and
    # never runs at StudioDeps() construction — the ToolDeps.start_run_fn
    # pattern (tools.py:243). Called as
    # `start_run_fn(project_id, resume=<bool>, continue_=<bool>)`.
    start_run_fn: Callable[..., dict[str, Any]] | None = None


# --------------------------------------------------------------------------
# Verb implementations — each takes (args, *, channel_id, thread_ts, deps)
# --------------------------------------------------------------------------


def list_projects_verb(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                        deps: "StudioDeps") -> dict[str, Any]:
    return {"projects": deps.list_projects_fn()}


def answer_question(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                     deps: "StudioDeps") -> dict[str, Any]:
    # Pure Q&A grounded in context already fetched by an earlier hop — no
    # side effect, no engine call.
    return {"status": "ok"}


def _default_team_members(team_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expand minimal ``{"coding_role", "gateway_route_id"}`` specs (the
    ``studio_default_team`` config shape) into full team-member dicts, in the
    canonical shape ``recipes.resolve_team`` produces --
    ``{"id", "role", "enabled", "model_mode", "metadata", "gateway_route_id",
    "provider_kind"}`` -- so ``create_project_from_charter`` can set them
    verbatim via ``members=``.

    ``id`` is ``f"{coding_role}-{n}"`` where ``n`` counts occurrences of that
    role within ``team_specs`` (1-based) -- e.g. three "dev" specs become
    "dev-1", "dev-2", "dev-3". ``provider_kind`` is the route prefix before
    the first "." (e.g. "claude_cli" from "claude_cli.opus"). Specs missing
    either field are skipped rather than producing a broken member.
    """
    counts: dict[str, int] = {}
    members: list[dict[str, Any]] = []
    for spec in team_specs:
        role = str(spec.get("coding_role") or "").strip()
        route = str(spec.get("gateway_route_id") or "").strip()
        if not role or not route:
            continue
        counts[role] = counts.get(role, 0) + 1
        members.append({
            "id": f"{role}-{counts[role]}",
            "role": "answerer",
            "enabled": True,
            "model_mode": "single",
            "metadata": {"coding_role": role},
            "gateway_route_id": route,
            "provider_kind": route.split(".", 1)[0],
        })
    return members


_VALID_CODING_ROLES = frozenset({"pm", "dev", "reviewer", "tester", "designer"})


def _designer_route(default_specs: list[dict[str, Any]]) -> str:
    """The route the studio seats a Designer on — the designer entry in the
    effective default team (``studio_default_team``), or ``claude_cli.opus``
    if none is configured."""
    for spec in default_specs:
        if str(spec.get("coding_role") or "").strip() == "designer":
            route = str(spec.get("gateway_route_id") or "").strip()
            if route:
                return route
    return "claude_cli.opus"


def _gate_designer_by_modality(
    team_specs: list[dict[str, Any]], modality: Any,
    default_specs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply the Designer modality gate (spec §1) to a studio team.

    The studio path passes ``members=`` explicitly, bypassing
    ``recipes.resolve_team`` (which is where the engine normally seats the
    Designer), so the gate is applied here instead — reusing the engine's OWN
    ``recipes._UI_MODALITIES`` frozenset so the two can never drift:

    * UI modality (``static`` / ``server`` / ``desktop``) → ensure exactly one
      Designer is present (append one on the configured designer route if the
      team doesn't already carry one).
    * anything else (``cli`` / ``binary`` / ``container`` / unspecified) →
      strip every Designer, so the whole design spec stays provably inert.
    """
    from errorta_council.coding.recipes import _UI_MODALITIES

    def _role(spec: dict[str, Any]) -> str:
        return str(spec.get("coding_role") or "").strip()

    is_ui = str(modality or "").strip().lower() in _UI_MODALITIES
    if not is_ui:
        return [spec for spec in team_specs if _role(spec) != "designer"]
    if any(_role(spec) == "designer" for spec in team_specs):
        return team_specs
    return list(team_specs) + [
        {"coding_role": "designer", "gateway_route_id": _designer_route(default_specs)}
    ]


def _charter_team_specs(
    charter: dict[str, Any], default_specs: list[dict[str, Any]],
) -> list[dict[str, str]] | None:
    """A validated per-role team from the charter's optional ``team`` field
    (the models the user actually asked for), or ``None`` to fall back to the
    configured default team.

    **Grounded-or-fall-back.** Every spec's ``coding_role`` must be a real
    role and its ``gateway_route_id`` must be one the operator has actually
    configured — a route present in ``default_specs`` (the
    connectivity-verified, known-good set). ANY malformed entry, unknown
    role, unknown route, or a team with no pm discards the WHOLE custom team
    and falls back to the default, rather than shipping a broken or
    half-hallucinated team. This lets "Opus for all roles" stick (opus is a
    configured route) while a model that invents a route id can't strand a
    project on a dead team.
    """
    raw = charter.get("team")
    if not isinstance(raw, list) or not raw:
        return None
    allowed = {
        str(s.get("gateway_route_id") or "").strip() for s in default_specs
    }
    allowed.discard("")
    specs: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        role = str(item.get("coding_role") or "").strip()
        route = str(item.get("gateway_route_id") or "").strip()
        if role not in _VALID_CODING_ROLES or route not in allowed:
            return None
        specs.append({"coding_role": role, "gateway_route_id": route})
    if not any(s["coding_role"] == "pm" for s in specs):
        return None
    return specs


def create_project(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                    deps: "StudioDeps") -> dict[str, Any]:
    """Executes the real create — only ever reached by ``dispatch`` after
    the ``confirmed_via="block_actions"`` gate has already passed.

    Order matters (spec §6): the project is created FIRST, then the Slack
    channel, then the binding. If channel provisioning fails after the
    project was created, the project is not lost — its id is returned in
    the error result — and the channel is never bound to a half-created
    state.

    The team is passed explicitly as ``members=`` (an expansion of
    ``deps.default_team``, or ``config.load()["studio_default_team"]`` when
    that's unset) rather than left to ``create_fn``'s own
    ``resolve_team(recipe, available_routes)`` fallback. That fallback probes
    ``pm_reference.list_available_routes()``, which reflects only routes the
    desktop app's Test button has marked "connected" -- on a machine where
    that hasn't happened it can return a route set that produces the wrong
    (or an empty) team, leaving the spun-up project without a working PM.
    Passing ``members`` explicitly bypasses that probe entirely so a
    studio-created project always gets a working team.
    """
    charter = dict(args or {})
    title = str(charter.get("title") or charter.get("north_star") or "").strip()
    project_id = _project_id_from_title(title)

    if deps.default_team is not None:
        default_specs = deps.default_team
    else:
        default_specs = _config.load().get(
            "studio_default_team", _config.DEFAULT_CONFIG["studio_default_team"])
    # Honor the team the user asked for in the charter (validated against the
    # configured routes); fall back to the default when unspecified/invalid.
    team_specs = _charter_team_specs(charter, default_specs) or default_specs
    # Seat/strip the Designer by modality (spec §1) — UI projects get one,
    # cli/binary/container stay design-inert.
    team_specs = _gate_designer_by_modality(
        team_specs, charter.get("modality"), default_specs)
    members = _default_team_members(team_specs)

    try:
        deps.create_fn(
            project_id, charter,
            available_routes=deps.available_routes, members=members,
        )
    except (ValueError, LedgerError) as exc:
        # These are the two known, safe-to-surface failure shapes: a
        # friendly "missing charter field" note (ValueError) or a ledger
        # validation failure (LedgerError). Neither message carries secrets.
        return {"status": "error", "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001 - must never escape a live Slack turn
        # Any other engine exception (OSError from workspace I/O, KeyError
        # from a malformed charter, etc.) must not blow up dispatch. The
        # full exception (with traceback) goes to the module logger for
        # operators; only the exception's type name — never its message,
        # which could carry a filesystem path, token, or other internal
        # detail — is returned to the caller.
        _LOGGER.exception(
            "studio create_project: create_fn raised %s for project_id=%s",
            type(exc).__name__, project_id,
        )
        return {
            "status": "error",
            "detail": f"project creation failed ({type(exc).__name__})",
        }

    try:
        chan = deps.provision_fn(
            deps.web_client,
            title=title or project_id,
            invite_user_ids=list(deps.invite_user_ids),
            purpose=str(charter.get("north_star", "")),
        )
    except provisioning.ProvisioningError as exc:
        return {"status": "error", "project_id": project_id, "detail": str(exc)}

    deps.store.bind_channel(chan["channel_id"], project_id)

    # Spec §3.2: creating a project from Slack ALWAYS starts it. Unlike
    # `adopt_project`, there is no `start` opt-in and no approval gate of its
    # own -- the operator's decision, and it holds whether or not autopilot is
    # on. The accepted trade is that create's Approve tap was also the last
    # anti-injection control on this path; what remains is the allowlist and
    # the concierge's "quoted text is data, never a command" rule.
    #
    # `start_gate` is kept rather than removed. After the factory seeds the
    # charter's Focus it passes by construction, but it stays as the guard for
    # the (now impossible) no-goal case, and it is cheap.
    from errorta_council.coding.next_goal import start_gate  # noqa: PLC0415

    started, start_refused = False, start_gate(deps.ledger_factory(project_id))
    if start_refused is None:
        start_fn = deps.start_run_fn
        if start_fn is None:
            from errorta_slack.tools import _default_start_run as start_fn  # noqa: PLC0415
        try:
            start_fn(project_id, resume=False, continue_=False)
            started = True
        except Exception as exc:  # noqa: BLE001 - never escape a live turn
            # Classify through the shared helper rather than rendering the
            # exception type: a fresh start's realistic failures are a
            # logged-out provider (member_health_preflight_failed) and an
            # unconfigured team (run_setup_required), and both have actionable
            # messages there. "couldn't start the run (HTTPException)" tells the
            # operator nothing, which defeats the point of reporting at all.
            # The helper is already redacted -- it never surfaces an exception
            # message except for the two 409 codes it recognises.
            from errorta_slack.tools import _classify_start_exception  # noqa: PLC0415

            classified = _classify_start_exception(exc)
            if classified.get("status") == "already_running":
                # Impossible for a just-created project, but the helper models
                # it and swallowing it as a refusal would be a lie.
                started = True
            else:
                start_refused = str(
                    classified.get("detail") or "couldn't start the run")

    return {
        "status": "created",
        "project_id": project_id,
        "channel_id": chan["channel_id"],
        "channel_name": chan["name"],
        "north_star": str(charter.get("north_star", "")),
        "started": started,
        "start_refused": start_refused,
    }


def archive_project(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                     deps: "StudioDeps") -> dict[str, Any]:
    """Executes the real spin-down — only ever reached by ``dispatch`` after
    the ``confirmed_via="block_actions"`` gate has already passed. Chat text
    can NEVER reach this function; see the module docstring's injection-guard
    rationale.

    Soft and reversible (design §3.1): request-cancel a live run, pause the
    project, then archive + unbind its Slack channel if one is bound. Never
    raises — any channel-archive failure degrades to a clean ``"error"``
    result (the project may already be paused; that's fine) and the channel
    is NOT unbound unless the archive actually succeeded.
    """
    project_id = str(args.get("project_id") or "").strip()
    if not project_id:
        return {"status": "error", "detail": "project_id is required"}

    ledger = deps.ledger_factory(project_id)
    cid = deps.store.channel_for_project(project_id)

    if ledger.get_run_state().get("status") == "running":
        ledger.set_run_state(cancel_requested=True)

    ledger.set_project_status("paused")

    if cid:
        try:
            deps.provision_archive_fn(deps.web_client, cid)
        except provisioning.ProvisioningError as exc:
            return {"status": "error", "project_id": project_id, "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001 - must never escape a live Slack turn
            _LOGGER.exception(
                "studio archive_project: provision_archive_fn raised %s for project_id=%s",
                type(exc).__name__, project_id,
            )
            return {
                "status": "error",
                "project_id": project_id,
                "detail": f"channel archive failed ({type(exc).__name__})",
            }
        deps.store.unbind(cid)

    return {"status": "archived", "project_id": project_id, "channel_id": cid}


def _stored_modality(ledger: Any) -> str:
    """The charter ``modality`` stored on the project's approved ``brainstorm``
    governance artifact (``project_factory.py:90-93`` writes the whole charter
    as its ``body_json``), or ``""`` when there is none.

    An adopted project may predate the studio entirely, so this is genuinely
    best-effort — and ``""`` is the safe answer: ``_gate_designer_by_modality``
    treats a non-UI modality by stripping the Designer, which keeps the design
    spec provably inert rather than seating a role the project can't use.
    """
    try:
        from errorta_council.coding.governance import GovernanceStore

        artifact = GovernanceStore.for_ledger(ledger).latest_approved_artifact("brainstorm")
    except Exception:  # noqa: BLE001 - no governance store / no artifact -> unknown
        return ""
    body = getattr(artifact, "body_json", None) if artifact is not None else None
    if not isinstance(body, dict):
        return ""
    return str(body.get("modality") or "")


def adopt_project(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                   deps: "StudioDeps") -> dict[str, Any]:
    """Executes the real adopt — only ever reached by ``dispatch`` after the
    ``confirmed_via="block_actions"`` gate has already passed. The inverse of
    ``archive_project``: takes an ALREADY-EXISTING ledger project under Slack
    management. It never creates a project — that is ``create_project``'s job.

    Order mirrors ``create_project`` (§3.1): every ledger write lands before
    the channel, and the binding is written LAST, so a provisioning failure
    never leaves a channel bound to a half-configured project.

    Note ``archive_project`` calls ``store.unbind``, which deletes the binding
    record (store.py:124) — there is no channel history, so re-adopting a
    previously archived project necessarily gets a NEW channel (suffixed by
    ``_create_channel_with_retry`` if the name is taken).
    """
    from errorta_council.coding.ledger import ProjectNotFound
    from errorta_council.coding.next_goal import start_gate

    project_id = str(args.get("project_id") or "").strip()
    if not project_id:
        return {"status": "error", "detail": "project_id is required"}

    ledger = deps.ledger_factory(project_id)
    try:
        project = ledger.get_project()
    except ProjectNotFound:
        return {"status": "error", "detail": f"no project named {project_id!r}"}
    except Exception as exc:  # noqa: BLE001 - must never escape a live Slack turn
        _LOGGER.exception(
            "studio adopt_project: get_project raised %s for project_id=%s",
            type(exc).__name__, project_id)
        return {"status": "error", "detail": f"couldn't read the project ({type(exc).__name__})"}

    existing = deps.store.channel_for_project(project_id)
    if existing:
        return {"status": "already_bound", "project_id": project_id,
                "channel_id": existing}

    team_seated = False
    try:
        members = [m for m in (ledger.get_run_config().get("members") or [])
                   if isinstance(m, dict)]
    except Exception:  # noqa: BLE001 - unreadable run config -> treat as no team
        members = []
    if not members:
        if deps.default_team is not None:
            default_specs = deps.default_team
        else:
            default_specs = _config.load().get(
                "studio_default_team", _config.DEFAULT_CONFIG["studio_default_team"])
        # An adopted project has no charter in hand, so recover its modality
        # from the stored brainstorm artifact when there is one; absent that,
        # the gate treats it as non-UI and strips the Designer.
        specs = _gate_designer_by_modality(
            list(default_specs), _stored_modality(ledger), default_specs)
        seated = _default_team_members(specs)
        if seated:
            try:
                ledger.set_run_config(room_id=None, members=seated)
                team_seated = True
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception(
                    "studio adopt_project: set_run_config raised %s for project_id=%s",
                    type(exc).__name__, project_id)
                return {"status": "error", "project_id": project_id,
                        "detail": f"couldn't seat a team ({type(exc).__name__})"}

    title = str(getattr(project, "id", "") or project_id)
    try:
        chan = deps.provision_fn(
            deps.web_client, title=title,
            invite_user_ids=list(deps.invite_user_ids),
            purpose=str(getattr(project, "north_star", "") or "")[:250],
        )
    except provisioning.ProvisioningError as exc:
        return {"status": "error", "project_id": project_id, "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001 - must never escape a live Slack turn
        _LOGGER.exception(
            "studio adopt_project: provision_fn raised %s for project_id=%s",
            type(exc).__name__, project_id)
        return {"status": "error", "project_id": project_id,
                "detail": f"channel creation failed ({type(exc).__name__})"}

    deps.store.bind_channel(chan["channel_id"], project_id)

    # Seed the outbound cursor with everything that has ALREADY happened.
    # An adopted project can carry months of team log; with an empty cursor the
    # first poll treats all of it as new and posts one Slack message per entry,
    # burying the channel before the team does anything. Create needs no
    # equivalent -- a new project has no history, and its first real milestone
    # should be posted.
    #
    # Best effort: a project whose ledger cannot be read here simply starts with
    # an empty cursor (the old behaviour). Failing the adopt over a cosmetic
    # backfill would be a worse trade.
    try:
        from errorta_slack import outbound  # noqa: PLC0415

        deps.store.advance_cursor(
            chan["channel_id"], outbound.current_marker_cursor(project_id))
    except Exception:  # noqa: BLE001
        _LOGGER.warning(
            "adopt_project: could not seed the outbound cursor for %s",
            project_id, exc_info=True)

    started, start_refused = False, None
    if bool(args.get("start")):
        start_refused = start_gate(ledger)
        if start_refused is None:
            start_fn = deps.start_run_fn
            if start_fn is None:
                from errorta_slack.tools import _default_start_run as start_fn  # noqa: PLC0415
            try:
                start_fn(project_id, resume=False, continue_=False)
                started = True
            except Exception as exc:  # noqa: BLE001
                start_refused = f"couldn't start the run ({type(exc).__name__})"

    return {
        "status": "adopted", "project_id": project_id,
        "channel_id": chan["channel_id"], "channel_name": chan["name"],
        "team_seated": team_seated, "started": started,
        "start_refused": start_refused,
    }


_VERB_IMPLS: dict[str, Callable[..., dict[str, Any]]] = {
    "list_projects": list_projects_verb,
    "create_project": create_project,
    "answer_question": answer_question,
    "archive_project": archive_project,
    "adopt_project": adopt_project,
}

assert set(_VERB_IMPLS) == set(TOOL_CATALOG), "TOOL_CATALOG and _VERB_IMPLS drifted"


# --------------------------------------------------------------------------
# dispatch — the ONLY entry point the studio concierge is allowed to call
# --------------------------------------------------------------------------


def dispatch(verb: str, args: dict[str, Any], *, channel_id: str, thread_ts: str,
             confirmed_via: str | None = None, deps: "StudioDeps") -> dict[str, Any]:
    spec = TOOL_CATALOG.get(verb)
    if spec is None:
        allowed = ", ".join(sorted(TOOL_CATALOG)) or "none"
        raise ToolError(
            "tool_not_allowed",
            f"tool_not_allowed: {verb!r} — this studio executes only: {allowed}",
        )
    safe_args = dict(args or {})
    if spec["trust"] == "C" and confirmed_via != "block_actions":
        # Staged, never executed, on this path — the whole point. Neither
        # create_fn nor provision_fn nor store.bind_channel is reachable
        # from here; only a verified block_actions callback resolves this
        # confirmation and re-enters dispatch with confirmed_via set.
        #
        # channel_id MUST be threaded onto the staged record: the shared
        # outbound.sweep_timeouts background loop reads channel_id off every
        # pending confirmation (it has no live Slack payload to read one
        # from) to post its auto-decided timeout outcome back to the right
        # channel. Omitting it here would silently strand a timed-out
        # create_project request — the requester would never be told.
        cid = deps.store.stage_confirmation(verb, safe_args, thread_ts, channel_id=channel_id)
        return {"status": "needs_confirmation", "confirmation_id": cid}
    impl = _VERB_IMPLS[verb]
    return impl(safe_args, channel_id=channel_id, thread_ts=thread_ts, deps=deps)


__all__ = ["ToolError", "StudioDeps", "TOOL_CATALOG", "dispatch"]
