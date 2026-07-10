"""F047 — Council profile schema, YAML (safe) parsing, and secret guard."""
from __future__ import annotations

import re
from typing import Any

PROFILE_FORMAT_VERSION = 1

# Key substrings that indicate a leaked secret. Export refuses to emit them;
# import refuses to accept them. NOTE: a bare "token" is deliberately NOT here —
# legitimate budget keys (``max_input_tokens_per_turn`` etc.) contain "tokens"
# as a count, not a credential. Real token secrets are named with a qualifier
# (access_token / bearer_token / …), which these compounds catch precisely.
_SECRET_KEY_HINTS = (
    "api_key",
    "apikey",
    "secret",
    "password",
    "passwd",
    "passphrase",
    "private_key",
    "client_secret",
    "access_token",
    "refresh_token",
    "auth_token",
    "bearer_token",
    "session_token",
    "id_token",
    "authorization",
    "bearer",
    "oauth",
    "credential",
)

# Config sections a profile carries (everything else in a room is runtime).
PROFILE_SECTIONS = (
    "topology",
    "context_policy",
    "budget_policy",
    "finalization_policy",
    "steward_policy",
    "tool_policy",
    "child_run_policy",
    "context_efficiency",
    "escalation_policy",
    "escalation_roster",
)


class ProfileError(ValueError):
    """A profile is malformed or carries forbidden content (e.g. a secret)."""


def _walk_keys(obj: Any):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield str(key)
            yield from _walk_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_keys(item)


def assert_secret_free(profile: dict[str, Any]) -> None:
    """Raise ProfileError if any key looks like a secret. Defense in depth —
    rooms never store keys (those live in provider-keys.json), but a hand-edited
    profile must not be able to smuggle one in either direction."""
    lowered = re.compile("|".join(re.escape(h) for h in _SECRET_KEY_HINTS))
    for key in _walk_keys(profile):
        if lowered.search(key.lower()):
            raise ProfileError(f"profile_contains_secret_key: {key!r}")


def validate_profile_shape(profile: Any) -> list[dict[str, str]]:
    """Structural validation. Returns a list of ``{code, detail}`` errors
    (empty = valid shape). Does NOT validate providers/tools — that needs the
    live catalog and lives in the importer."""
    errors: list[dict[str, str]] = []
    if not isinstance(profile, dict):
        return [{"code": "not_a_mapping", "detail": "profile must be a mapping"}]
    fv = profile.get("format_version")
    if fv is None:
        errors.append({"code": "missing_format_version", "detail": "format_version required"})
    else:
        try:
            fv_int = int(fv)
        except (TypeError, ValueError):
            fv_int = None
        if fv_int != PROFILE_FORMAT_VERSION:
            errors.append({
                "code": "unsupported_format_version",
                "detail": f"expected {PROFILE_FORMAT_VERSION}, got {fv!r}",
            })
    if not str(profile.get("name") or "").strip():
        errors.append({"code": "missing_name", "detail": "name required"})
    members = profile.get("members")
    if not isinstance(members, list) or not members:
        errors.append({"code": "missing_members", "detail": "at least one member required"})
    else:
        for i, m in enumerate(members):
            if not isinstance(m, dict) or not str(m.get("id") or "").strip():
                errors.append({"code": "member_missing_id", "detail": f"members[{i}] needs an id"})
    try:
        assert_secret_free(profile)
    except ProfileError as exc:
        errors.append({"code": "secret_key", "detail": str(exc)})
    return errors


def parse_profile_yaml(text: str) -> dict[str, Any]:
    """Safe-parse a YAML profile. Raises ProfileError on bad YAML / non-mapping."""
    import yaml

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:  # pragma: no cover - error text varies
        raise ProfileError(f"invalid_yaml: {str(exc)[:200]}") from None
    if not isinstance(data, dict):
        raise ProfileError("profile_must_be_a_mapping")
    return data


def profile_to_yaml(profile: dict[str, Any]) -> str:
    import yaml

    assert_secret_free(profile)
    return yaml.safe_dump(profile, sort_keys=False, allow_unicode=True, width=100)
