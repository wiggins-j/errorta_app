"""F047 — declarative, secret-free Council profiles.

A profile is a portable, reviewable description of a Council room (members,
topology, policies) that can seed a *draft* room. ``CouncilRoom`` JSON remains
the canonical store; profiles are a portability layer, not an execution
authority. Profiles never contain secrets, and importing one never runs
anything or silently grants unavailable tools.
"""
from .exporter import export_room_to_profile
from .importer import import_profile_to_room_draft
from .schema import (
    PROFILE_FORMAT_VERSION,
    ProfileError,
    parse_profile_yaml,
    profile_to_yaml,
    validate_profile_shape,
)

__all__ = [
    "PROFILE_FORMAT_VERSION",
    "ProfileError",
    "export_room_to_profile",
    "import_profile_to_room_draft",
    "parse_profile_yaml",
    "profile_to_yaml",
    "validate_profile_shape",
]
