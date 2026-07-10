"""Environment allowlist builder for ToolRunner.

The runner never forwards a process environment wholesale. Callers provide a
source mapping, the builder copies a small set of process essentials, and any
sensitive values must be explicit grants.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .types import EnvGrant

DEFAULT_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
)

SECRET_NAME_MARKERS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "PRIVATE_KEY",
    "API_KEY",
    "ACCESS_KEY",
    "SESSION",
    "COOKIE",
    "CREDENTIAL",
    "AUTH",
)


@dataclass(frozen=True)
class RunnerEnv:
    values: dict[str, str]
    allowlisted_names: tuple[str, ...] = ()
    explicit_names: tuple[str, ...] = ()
    stripped_names: tuple[str, ...] = ()
    redaction_values: dict[str, str] = field(default_factory=dict)

    def safe_projection(self) -> dict[str, object]:
        return {
            "names": sorted(self.values.keys()),
            "allowlisted_names": sorted(self.allowlisted_names),
            "explicit_names": sorted(self.explicit_names),
            "stripped_names": sorted(self.stripped_names),
        }


class EnvGrantError(ValueError):
    """Invalid runner env allowlist or explicit grant."""


def is_secret_env_name(name: str) -> bool:
    normalized = name.upper()
    return any(marker in normalized for marker in SECRET_NAME_MARKERS)


def _validate_env_name(name: str) -> str:
    if not name or "=" in name or "\x00" in name:
        raise EnvGrantError("invalid_env_name")
    return name


def _string_env_values(raw: Mapping[str, object]) -> dict[str, str]:
    env: dict[str, str] = {}
    for key, value in raw.items():
        name = _validate_env_name(str(key))
        text = str(value)
        if "\x00" in text:
            raise EnvGrantError("invalid_env_value")
        env[name] = text
    return env


def build_runner_env(
    *,
    source_env: Mapping[str, object],
    allowlist: tuple[str, ...] | list[str] | set[str] | None = None,
    explicit_env: tuple[EnvGrant, ...] | list[EnvGrant] | Mapping[str, str] | None = None,
) -> RunnerEnv:
    """Build the bounded environment for a runner process.

    Secret-looking names are never copied from ``source_env``. They may only
    appear through ``explicit_env``, which records redaction metadata for output
    sanitization.
    """

    source = _string_env_values(source_env)
    requested_allowlist = tuple(DEFAULT_ENV_ALLOWLIST) + tuple(allowlist or ())
    values: dict[str, str] = {}
    copied: list[str] = []
    stripped: list[str] = []

    for raw_name in requested_allowlist:
        name = _validate_env_name(str(raw_name))
        if name not in source:
            continue
        if is_secret_env_name(name):
            stripped.append(name)
            continue
        values[name] = source[name]
        copied.append(name)

    explicit_grants: list[EnvGrant]
    if explicit_env is None:
        explicit_grants = []
    elif isinstance(explicit_env, Mapping):
        explicit_grants = [
            EnvGrant(name=str(name), value=str(value))
            for name, value in explicit_env.items()
        ]
    else:
        explicit_grants = list(explicit_env)

    redaction_values: dict[str, str] = {}
    explicit_names: list[str] = []
    for grant in explicit_grants:
        name = _validate_env_name(grant.name)
        values[name] = grant.value
        explicit_names.append(name)
        if grant.value:
            redaction_values[name] = grant.value

    return RunnerEnv(
        values=values,
        allowlisted_names=tuple(dict.fromkeys(copied)),
        explicit_names=tuple(dict.fromkeys(explicit_names)),
        stripped_names=tuple(dict.fromkeys(stripped)),
        redaction_values=redaction_values,
    )


def sanitize_text(text: str, *, redaction_values: Mapping[str, str] | None = None) -> str:
    """Remove granted secret values and non-printable control bytes from logs."""

    sanitized = "".join(
        ch if ch == "\n" or ch == "\t" or ord(ch) >= 32 else "\uFFFD"
        for ch in text
    )
    for name, value in (redaction_values or {}).items():
        if len(value) >= 3:
            sanitized = sanitized.replace(value, f"[redacted-env:{name}]")
    return sanitized


__all__ = [
    "DEFAULT_ENV_ALLOWLIST",
    "EnvGrantError",
    "RunnerEnv",
    "build_runner_env",
    "is_secret_env_name",
    "sanitize_text",
]
