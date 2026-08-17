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

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from errorta_council.coding.ledger import LedgerError, LedgerStore, list_projects
from errorta_council.coding.project_factory import create_project_from_charter
from errorta_slack import provisioning
from errorta_slack import store as _slack_store


class ToolError(Exception):
    """Raised when a verb cannot be executed. ``.code`` is a stable string."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


# --------------------------------------------------------------------------
# Catalog — the single source of truth for verbs, trust class, and prompt copy
# --------------------------------------------------------------------------

TOOL_CATALOG: dict[str, dict[str, str]] = {
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
        "summary": "Answer a question from context already fetched (no side effect).",
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
    invite_user_ids: list[str] = field(default_factory=list)
    available_routes: list[dict[str, Any]] | None = None


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


def create_project(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                    deps: "StudioDeps") -> dict[str, Any]:
    """Executes the real create — only ever reached by ``dispatch`` after
    the ``confirmed_via="block_actions"`` gate has already passed.

    Order matters (spec §6): the project is created FIRST, then the Slack
    channel, then the binding. If channel provisioning fails after the
    project was created, the project is not lost — its id is returned in
    the error result — and the channel is never bound to a half-created
    state.
    """
    charter = dict(args or {})
    title = str(charter.get("title") or charter.get("north_star") or "").strip()
    project_id = _project_id_from_title(title)

    try:
        deps.create_fn(project_id, charter, available_routes=deps.available_routes)
    except (ValueError, LedgerError) as exc:
        return {"status": "error", "detail": str(exc)}

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
    return {
        "status": "created",
        "project_id": project_id,
        "channel_id": chan["channel_id"],
        "channel_name": chan["name"],
    }


_VERB_IMPLS: dict[str, Callable[..., dict[str, Any]]] = {
    "list_projects": list_projects_verb,
    "create_project": create_project,
    "answer_question": answer_question,
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
        cid = deps.store.stage_confirmation(verb, safe_args, thread_ts)
        return {"status": "needs_confirmation", "confirmation_id": cid}
    impl = _VERB_IMPLS[verb]
    return impl(safe_args, channel_id=channel_id, thread_ts=thread_ts, deps=deps)


__all__ = ["ToolError", "StudioDeps", "TOOL_CATALOG", "dispatch"]
