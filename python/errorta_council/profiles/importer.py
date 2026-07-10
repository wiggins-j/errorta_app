"""F047 — profile -> draft CouncilRoom + validation.

Importing a profile creates a DRAFT room dict (status_hint="draft", new id,
revision 0). It never runs anything. It reports missing providers/tools as
warnings but does NOT silently remap an unavailable provider to local/fake —
the member's intended route is preserved and flagged so the user decides.
"""
from __future__ import annotations

import uuid
from typing import Any

from .schema import (
    PROFILE_SECTIONS,
    assert_secret_free,
    validate_profile_shape,
)

# Provider classes that never need configuration.
_ALWAYS_AVAILABLE = frozenset({"local", "fake"})

# Required room sections with safe defaults — CouncilRoom.from_dict reads these
# unconditionally, so a draft must always carry them even if the profile omits
# them. Mirrors the frontend's buildBlankRoom() defaults.
def _default_sections(member_ids: list[str]) -> dict[str, Any]:
    return {
        "topology": {
            "kind": "round_robin",
            "max_rounds": 1,
            "max_total_turns": max(2, len(member_ids)),
            "max_messages_per_member": 1,
            "speaker_order": list(member_ids),
        },
        "context_policy": {
            "default_context_access": "prompt_only",
            "default_transcript_access": "all_messages",
            "allow_full_context": True,
            "require_confirmation_for_remote_context": False,
            "require_confirmation_for_full_context": False,
        },
        "budget_policy": {
            "max_rounds": None,
            "max_messages_per_member": None,
            "max_total_model_calls": None,
            "max_remote_calls_per_run": 0,
            "max_remote_calls_per_day": None,
            "max_input_tokens_per_turn": 4096,
            "max_output_tokens_per_turn": 2048,
            "max_context_tokens_per_member": 4096,
            "max_estimated_usd_per_run": None,
            "max_estimated_usd_per_month": None,
        },
        "finalization_policy": {"mode": "transcript_only"},
    }

# Council room schema format version (mirrors errorta_council.schema.FORMAT_VERSION
# without importing it here to keep this module light; the value is stable).
_ROOM_FORMAT_VERSION = 1


def _provider_class_from_route(route_id: str) -> str:
    if not route_id:
        return "local"
    head_dot = route_id.split(".", 1)[0]
    head_slash = route_id.split("/", 1)[0]
    return head_dot if len(head_dot) <= len(head_slash) else head_slash


def _complete_member(member: dict[str, Any]) -> dict[str, Any]:
    """Fill the room-schema-required fields a profile strips, with safe
    defaults, so the draft is a valid CouncilRoom member."""
    mid = str(member.get("id") or f"m-{uuid.uuid4().hex[:6]}")
    route_id = member.get("gateway_route_id") or member.get("route_id")
    provider_kind = str(
        member.get("provider_kind")
        or member.get("provider")
        or (_provider_class_from_route(str(route_id or "")) if route_id else "local")
    )
    model = str(member.get("model") or "")
    completed = dict(member)
    completed.update(
        id=mid,
        name=str(member.get("name") or mid),
        role=str(member.get("role") or "member"),
        enabled=bool(member.get("enabled", True)),
        gateway_route_id=route_id,
        provider_kind=provider_kind,
        provider=member.get("provider", provider_kind),
        model=model,
        provider_display=str(member.get("provider_display") or provider_kind),
        model_display=str(member.get("model_display") or model or mid),
        catalog_version=member.get("catalog_version"),
        context_access=str(member.get("context_access") or "prompt_only"),
        transcript_access=str(member.get("transcript_access") or "own_messages"),
        turn_limits=dict(member.get("turn_limits") or {}),
        generation=dict(member.get("generation") or {}),
        system_prompt=str(member.get("system_prompt") or ""),
        metadata=dict(member.get("metadata") or {}),
    )
    return completed


def _granted_tool_families(tool_policy: dict[str, Any] | None) -> list[str]:
    policy = tool_policy or {}
    out: list[str] = []
    for family in (
        "web_fetch", "web_search", "code_read", "code_write", "code_exec",
    ):
        sub = policy.get(family)
        if isinstance(sub, dict) and bool(sub.get("enabled")):
            out.append(family)
    explicit = policy.get("enabled_tool_ids")
    if isinstance(explicit, list):
        out.extend(str(t) for t in explicit)
    return sorted(set(out))


def import_profile_to_room_draft(
    profile: dict[str, Any],
    *,
    available_provider_classes: set[str] | None = None,
    available_tool_ids: set[str] | None = None,
    now: str,
    new_id: str | None = None,
) -> dict[str, Any]:
    """Return ``{"room": <draft room dict>, "validation": {...}}``.

    ``available_provider_classes`` is the set of *configured* providers (e.g.
    from /gateway/providers). ``available_tool_ids`` is the configured tool set
    (e.g. from the F045 catalog). Both default to the always-available set so
    the importer is usable in tests without the live registries.
    """
    assert_secret_free(profile)
    shape_errors = validate_profile_shape(profile)

    available_providers = (
        set(available_provider_classes) if available_provider_classes is not None else set()
    ) | _ALWAYS_AVAILABLE
    available_tools = set(available_tool_ids) if available_tool_ids is not None else set()

    raw_members = profile.get("members") if isinstance(profile.get("members"), list) else []
    members = [_complete_member(m) for m in raw_members if isinstance(m, dict)]

    # Missing providers — never remapped, only reported.
    missing_providers: list[dict[str, Any]] = []
    for m in members:
        if not m.get("enabled", True):
            continue
        pc = _provider_class_from_route(str(m.get("gateway_route_id") or ""))
        if pc not in available_providers:
            missing_providers.append({
                "member_id": m["id"],
                "route_id": m.get("gateway_route_id"),
                "provider_class": pc,
            })

    # Tool requests — reported; unavailable ones flagged. They remain *requests*:
    # the room's tool_policy still gates each tool at runtime via F041 consent.
    requested_tools = _granted_tool_families(profile.get("tool_policy"))
    missing_tools = [t for t in requested_tools if t not in available_tools]

    room: dict[str, Any] = {
        "format_version": _ROOM_FORMAT_VERSION,
        "id": new_id or f"room-{uuid.uuid4().hex[:12]}",
        "name": str(profile.get("name") or "Imported council"),
        "description": str(profile.get("description") or ""),
        "members": members,
        "status_hint": "draft",
        "revision": 0,
        "created_at": now,
        "updated_at": now,
        "last_validated_at": None,
    }
    if profile.get("preset_id"):
        room["preset_id"] = profile["preset_id"]
    # Required sections get defaults; a profile may carry a PARTIAL section
    # (e.g. topology with only kind+max_rounds), so MERGE the profile's keys
    # onto the default rather than replacing — otherwise from_dict trips on a
    # required field the profile omitted.
    defaults = _default_sections([m["id"] for m in members])
    for section, default in defaults.items():
        room[section] = {**default, **(profile.get(section) or {})}
    # Other (optional) sections are taken verbatim.
    for section in PROFILE_SECTIONS:
        if section in defaults:
            continue
        if section in profile and profile[section] not in (None, {}, []):
            room[section] = profile[section]

    validation = {
        "ok": len(shape_errors) == 0,
        "errors": shape_errors,
        "missing_providers": missing_providers,
        "requested_tools": requested_tools,
        "missing_tools": missing_tools,
        "warnings": _warnings(missing_providers, missing_tools),
    }
    return {"room": room, "validation": validation}


def _warnings(
    missing_providers: list[dict[str, Any]], missing_tools: list[str]
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for mp in missing_providers:
        out.append({
            "code": "provider_not_configured",
            "detail": f"member {mp['member_id']} needs provider "
            f"{mp['provider_class']!r} (route {mp['route_id']!r}) — configure it before running",
        })
    for tool in missing_tools:
        out.append({
            "code": "tool_not_available",
            "detail": f"profile requests tool {tool!r} which is not configured; "
            "it stays a request and still needs approval at runtime",
        })
    return out
