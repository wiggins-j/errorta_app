"""CLI-local team draft — the source of truth for team *assembly* (F147 S4, §7.2).

The single most important team finding (survey §4 + spec §7.2): **a coding project
stores no room and no CRUD route returns the full team.** The engine keeps the team
in the ledger's ``run_config.json``, written ONLY through
``POST /run-setup/confirm`` (or ``POST /run``) with an explicit ``members`` list.
The one read-only projection, ``GET /model-usage``, is *derived and lossy* — it
drops ``coding_role`` (its ``role`` field is the raw ``CouncilMember`` role),
omits the ``enabled`` flag, and single-mode members carry no role at all. So it
CANNOT round-trip a team edit.

Therefore ``team set/pool/mode/enable`` assemble members in a **CLI-local draft**
and ``team apply`` pushes that draft to the existing ``/run-setup/confirm`` route
— exactly the ``members`` shape S3's ``setup --confirm`` / ``run --members``
already consume (``coding.py:_resolve_members`` → ``_ensure_coding_roles`` →
``_validate_member_ids``). The draft is genuine client-side scratch that the engine
never reads; it is namespaced under ``${ERRORTA_HOME}/cli-team-drafts/`` (a
``cli-`` prefixed dir the engine ignores) and honors ``--home`` isolation. This is
a deliberate, documented departure from the spec's "no new config files" note,
which refers to the *shared engine store*: a pre-confirm draft is not engine state.

Member shape produced (the minimal set the run-setup body validates):

    {"id": "<coding_role>", "role": "member", "enabled": true,
     "model_mode": "single", "gateway_route_id": "<route>",
     "metadata": {"coding_role": "<role>"}}

Multi-mode swaps ``gateway_route_id`` for ``"model_mode": "multi",
"model_pool": ["route", ...]``.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

# Coding roles, in the canonical order (mirrors coding.py `_DEFAULT_ROLE_ORDER`).
CODING_ROLES = ("pm", "dev", "reviewer", "tester")


def _drafts_dir(home: Path) -> Path:
    return Path(home) / "cli-team-drafts"


def draft_path(home: Path, project_id: str) -> Path:
    """The draft file for one project (``cli-`` namespaced, engine-ignored)."""
    return _drafts_dir(home) / f"{project_id}.json"


def load(home: Path, project_id: str) -> dict[str, Any]:
    """Load the draft, or a fresh empty draft (``{"members": [], "room_id": None}``).

    A malformed file reads as empty (never raises) so a corrupt scratch draft can
    always be re-assembled from scratch rather than wedging the command.
    """
    p = draft_path(home, project_id)
    if not p.exists():
        return {"members": [], "room_id": None}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"members": [], "room_id": None}
    if not isinstance(data, dict):
        return {"members": [], "room_id": None}
    members = data.get("members")
    return {
        "members": [m for m in members if isinstance(m, dict)] if isinstance(members, list) else [],
        "room_id": data.get("room_id") if isinstance(data.get("room_id"), str) else None,
    }


def save(home: Path, project_id: str, draft: dict[str, Any]) -> None:
    """Atomically persist the draft (tmpfile + ``os.replace``)."""
    p = draft_path(home, project_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(draft, indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".team-draft-", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def clear(home: Path, project_id: str) -> None:
    """Delete the draft file (reset to empty)."""
    p = draft_path(home, project_id)
    try:
        p.unlink()
    except FileNotFoundError:
        pass


def exists(home: Path, project_id: str) -> bool:
    return draft_path(home, project_id).exists()


# --------------------------------------------------------------------------- #
# Member mutation — assemble the `members` list run-setup consumes.
# --------------------------------------------------------------------------- #

def _find(members: list[dict[str, Any]], role: str) -> dict[str, Any] | None:
    for m in members:
        if (m.get("metadata") or {}).get("coding_role") == role or m.get("id") == role:
            return m
    return None


def _new_member(role: str) -> dict[str, Any]:
    return {
        "id": role,
        "role": "member",
        "enabled": True,
        "model_mode": "single",
        "metadata": {"coding_role": role},
    }


def set_route(draft: dict[str, Any], role: str, route: str) -> dict[str, Any]:
    """Set a role to SINGLE mode on ``route`` (creating the member if absent)."""
    members = list(draft.get("members") or [])
    member = _find(members, role)
    if member is None:
        member = _new_member(role)
        members.append(member)
    member["model_mode"] = "single"
    member["gateway_route_id"] = route
    member.pop("model_pool", None)
    return {"members": members, "room_id": draft.get("room_id")}


def set_pool(draft: dict[str, Any], role: str, routes: list[str]) -> dict[str, Any]:
    """Set a role to MULTI mode over ``routes`` (creating the member if absent)."""
    members = list(draft.get("members") or [])
    member = _find(members, role)
    if member is None:
        member = _new_member(role)
        members.append(member)
    member["model_mode"] = "multi"
    member["model_pool"] = list(routes)
    member.pop("gateway_route_id", None)
    return {"members": members, "room_id": draft.get("room_id")}


def set_mode(draft: dict[str, Any], role: str, mode: str) -> dict[str, Any]:
    """Flip a role between ``single`` and ``multi`` (member must already exist)."""
    members = list(draft.get("members") or [])
    member = _find(members, role)
    if member is None:
        raise KeyError(role)
    member["model_mode"] = mode
    return {"members": members, "room_id": draft.get("room_id")}


def set_enabled(draft: dict[str, Any], role: str, enabled: bool) -> dict[str, Any]:
    """Enable/disable a member (member must already exist)."""
    members = list(draft.get("members") or [])
    member = _find(members, role)
    if member is None:
        raise KeyError(role)
    member["enabled"] = enabled
    return {"members": members, "room_id": draft.get("room_id")}


def set_room(draft: dict[str, Any], room_id: str) -> dict[str, Any]:
    """Back the team with a Council room (clears any explicit members)."""
    return {"members": [], "room_id": room_id}


def to_run_body(draft: dict[str, Any]) -> dict[str, Any]:
    """The ``{members?|room_id?}`` body S3's run / run-setup already consume."""
    members = draft.get("members") or []
    if members:
        return {"members": members}
    if draft.get("room_id"):
        return {"room_id": draft["room_id"]}
    return {}
