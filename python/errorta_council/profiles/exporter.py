"""F047 — CouncilRoom -> secret-free profile."""
from __future__ import annotations

from typing import Any

from .schema import PROFILE_FORMAT_VERSION, PROFILE_SECTIONS, assert_secret_free

# Room fields that are runtime/identity only — never exported into a profile.
_RUNTIME_ROOM_FIELDS = frozenset({
    "id",
    "revision",
    "created_at",
    "updated_at",
    "last_validated_at",
    "status_hint",
    "format_version",
    "ui",
})

# Per-member fields that are runtime/derived bindings — stripped on export so a
# profile carries intent (which model, what access), not a catalog snapshot.
_RUNTIME_MEMBER_FIELDS = frozenset({
    "catalog_version",
    "provider_display",
    "model_display",
    "last_validated_at",
})


def _clean_member(member: dict[str, Any]) -> dict[str, Any]:
    return {
        k: v for k, v in member.items() if k not in _RUNTIME_MEMBER_FIELDS
    }


def export_room_to_profile(room: dict[str, Any]) -> dict[str, Any]:
    """Produce a portable, secret-free profile from a room dict.

    Keeps configuration (members, topology, policies) and drops runtime/identity
    fields (id, revision, timestamps, status, ui). Refuses to emit if the room
    somehow carries a secret-looking key.
    """
    profile: dict[str, Any] = {
        "format_version": PROFILE_FORMAT_VERSION,
        "name": str(room.get("name") or "Untitled council"),
        "description": str(room.get("description") or ""),
    }
    if room.get("preset_id"):
        profile["preset_id"] = room["preset_id"]
    members = room.get("members")
    profile["members"] = [
        _clean_member(m) for m in members if isinstance(m, dict)
    ] if isinstance(members, list) else []
    for section in PROFILE_SECTIONS:
        value = room.get(section)
        if value not in (None, {}, []):
            profile[section] = value
    # Final guard: never emit a profile that carries a secret-looking key, and
    # never leak a runtime room field that slipped through a nested _extra.
    for runtime_field in _RUNTIME_ROOM_FIELDS:
        profile.pop(runtime_field, None)
    assert_secret_free(profile)
    return profile
