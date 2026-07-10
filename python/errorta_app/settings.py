"""Persistent sidecar settings for Errorta.

F032 keeps this intentionally small: a single JSON file under the canonical
Errorta data root controls process-wide log verbosity.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from errorta_app.paths import errorta_home

log = logging.getLogger(__name__)

DEFAULT_SETTINGS: dict[str, str] = {"log_level": "info"}
ALLOWED_LOG_LEVELS = frozenset({"info", "debug"})

# F040-01 — persisted per-provider CLI binary overrides. Set via the native
# file picker in the provider-keys panel; honored ahead of PATH by the gateway
# resolver (the value is read HERE in the app and passed into the gateway as a
# parameter — the gateway never reads settings.json).
CLI_BINARY_PROVIDERS = frozenset({"claude_cli", "codex_cli", "cursor_cli"})


def path() -> Path:
    """Return the persistent sidecar settings path."""
    p = errorta_home() / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _normalize_log_level(value: Any) -> str:
    level = str(value).strip().lower()
    if level not in ALLOWED_LOG_LEVELS:
        raise ValueError("log_level must be one of: info, debug")
    return level


def _normalize_cli_binaries(value: Any) -> dict[str, str]:
    """Keep only known providers mapping to non-empty absolute path strings.

    Unknown keys, non-string values, and blank paths are dropped. Existence /
    executability is enforced at the route layer (the picker), not here — a
    stored path that later vanishes should round-trip, not crash the load.
    """
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for provider, raw_path in value.items():
        if provider not in CLI_BINARY_PROVIDERS:
            continue
        if not isinstance(raw_path, str):
            continue
        p = raw_path.strip()
        if p:
            out[provider] = p
    return out


def _normalize_tools(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    searxng_url = str(value.get("searxng_url") or "").strip()
    return {"searxng_url": searxng_url} if searxng_url else {}


def _normalize_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    merged: dict[str, Any] = dict(DEFAULT_SETTINGS)
    if raw:
        merged.update(raw)
    normalized: dict[str, Any] = {
        "log_level": _normalize_log_level(merged.get("log_level"))
    }
    cli_binaries = _normalize_cli_binaries(merged.get("cli_binaries"))
    if cli_binaries:
        normalized["cli_binaries"] = cli_binaries
    tools = _normalize_tools(merged.get("tools"))
    if tools:
        normalized["tools"] = tools
    # F129: absence means derive from configured providers; an explicit empty
    # list intentionally disables every dynamic model family.
    if "model_family_allowlist" in merged:
        value = merged.get("model_family_allowlist")
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError("model_family_allowlist must be a list of strings")
        normalized["model_family_allowlist"] = sorted({v.strip() for v in value if v.strip()})
    # F120: pre-run member-health preflight toggle (default on). Only persisted
    # when explicitly turned off so a fresh settings file stays minimal.
    if "member_health_preflight" in merged:
        normalized["member_health_preflight"] = bool(
            merged.get("member_health_preflight"))
    # F121: sticky coding-run defaults blob (the readiness-gate pre-fill seed).
    run_defaults = _normalize_run_defaults(merged.get(_RUN_DEFAULTS_KEY))
    if run_defaults:
        normalized[_RUN_DEFAULTS_KEY] = run_defaults
    return normalized


def load() -> dict[str, Any]:
    """Read settings from disk, creating the default file if needed."""
    settings_path = path()
    if not settings_path.exists():
        settings = dict(DEFAULT_SETTINGS)
        save(settings)
        return settings

    try:
        raw = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Failed to read settings from %s: %s", settings_path, exc)
        return dict(DEFAULT_SETTINGS)

    if not isinstance(raw, dict):
        log.warning("Ignoring invalid settings payload at %s", settings_path)
        return dict(DEFAULT_SETTINGS)

    try:
        return _normalize_settings(raw)
    except ValueError as exc:
        log.warning("Ignoring invalid settings value at %s: %s", settings_path, exc)
        return dict(DEFAULT_SETTINGS)


def save(settings: dict[str, Any]) -> dict[str, Any]:
    """Atomically persist settings with owner-only permissions."""
    normalized = _normalize_settings(settings)
    settings_path = path()
    fd, tmp_name = tempfile.mkstemp(
        prefix=".settings-",
        suffix=".json",
        dir=str(settings_path.parent),
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(normalized, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, settings_path)
        os.chmod(settings_path, 0o600)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    log.info("Saved settings to %s", settings_path)
    return normalized


def get_tools_settings() -> dict[str, str]:
    tools = load().get("tools")
    return _normalize_tools(tools)


def update_tools_settings(*, searxng_url: str | None = None) -> dict[str, str]:
    current = load()
    tools = _normalize_tools(current.get("tools"))
    if searxng_url is not None:
        cleaned = searxng_url.strip()
        if cleaned:
            tools["searxng_url"] = cleaned
        else:
            tools.pop("searxng_url", None)
    if tools:
        current["tools"] = tools
    else:
        current.pop("tools", None)
    return _normalize_tools(save(current).get("tools"))


def get_model_family_allowlist() -> list[str] | None:
    current = load()
    if "model_family_allowlist" not in current:
        return None
    return list(current.get("model_family_allowlist") or [])


def set_model_family_allowlist(families: list[str] | None) -> list[str] | None:
    current = load()
    if families is None:
        current.pop("model_family_allowlist", None)
    else:
        current["model_family_allowlist"] = list(families)
    saved = save(current)
    if "model_family_allowlist" not in saved:
        return None
    return list(saved.get("model_family_allowlist") or [])


# F121 — sticky/learned coding-run defaults. Whatever config the user finalizes
# in the readiness gate becomes the pre-fill for their NEXT new project. We store
# the *resolved* config (not the preset name) so it survives preset re-tuning,
# under a single namespaced blob. The blob is intentionally schema-light: it is a
# UI pre-fill seed, re-validated by the real setters on confirm. Unknown keys are
# dropped on load so a future-shaped blob can't break the gate.
_RUN_DEFAULTS_KEY = "coding_run_defaults"
_RUN_DEFAULTS_ALLOWED = frozenset({
    "governance_mode",
    "block_on_problems",
    "human_code_approval",
    "max_review_rounds",
    "checkpoint_cadence",
    "checkpoint_n",
    "guardrail_enabled",
    "grounding_enabled",
    "max_iterations",
    "max_model_calls",
    "max_parallel_workers",
    "member_failure_limit",
    "preflight_enabled",
    "team_room_id",
})


def _normalize_run_defaults(value: Any) -> dict[str, Any]:
    """Keep only known keys with plain JSON-scalar values. Schema-light by design
    (the real setters re-validate on confirm); this just prevents a malformed or
    future-shaped blob from breaking the gate pre-fill."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    for key, val in value.items():
        if key not in _RUN_DEFAULTS_ALLOWED:
            continue
        if isinstance(val, (str, int, float, bool)) or val is None:
            out[key] = val
    return out


def get_coding_run_defaults() -> dict[str, Any]:
    """The user's last-used coding-run config (empty dict if none saved yet).

    A brand-new install returns ``{}`` so the gate seeds from the built-in
    Careful preset (front-end). After the first confirm, this returns the
    resolved config the user finalized."""
    try:
        return _normalize_run_defaults(load().get(_RUN_DEFAULTS_KEY))
    except Exception:  # noqa: BLE001 — a settings hiccup must not break the gate
        return {}


def set_coding_run_defaults(cfg: dict[str, Any]) -> dict[str, Any]:
    """Persist the resolved coding-run config as the next-project pre-fill seed.

    Returns the normalized blob that was stored. Called on readiness-gate
    confirm (F121 D3)."""
    normalized = _normalize_run_defaults(cfg)
    current = load()
    if normalized:
        current[_RUN_DEFAULTS_KEY] = normalized
    else:
        current.pop(_RUN_DEFAULTS_KEY, None)
    save(current)
    return normalized


def member_health_preflight_enabled() -> bool:
    """F120: whether the pre-run member-health preflight runs (default on).

    Default ON for CLI/subscription routes — the probe is cheap and catches a
    logged-out provider before a run spins for minutes. A user who accepts the
    risk can disable it (then F120-02's in-loop accounting still catches it)."""
    try:
        return bool(load().get("member_health_preflight", True))
    except Exception:  # noqa: BLE001 — a settings hiccup must default to ON (safe)
        return True


def get_cli_binary(provider: str) -> str | None:
    """Return the persisted CLI binary override for ``provider`` (or ``None``).

    This is the *app-side* read of the override that the gateway resolver
    consumes as a parameter — keeping the gateway stdlib-only and unaware of
    ``settings.json``.
    """
    if provider not in CLI_BINARY_PROVIDERS:
        return None
    binaries = load().get("cli_binaries") or {}
    value = binaries.get(provider)
    return value if isinstance(value, str) and value else None


def set_cli_binary(provider: str, binary_path: str) -> dict[str, Any]:
    """Persist a CLI binary override for ``provider``. Returns saved settings.

    Caller (the route) is responsible for validating the path is an existing
    executable file before calling this.
    """
    if provider not in CLI_BINARY_PROVIDERS:
        raise ValueError(f"unknown cli provider: {provider!r}")
    p = (binary_path or "").strip()
    if not p:
        raise ValueError("binary path is required")
    current = load()
    binaries = dict(current.get("cli_binaries") or {})
    binaries[provider] = p
    current["cli_binaries"] = binaries
    return save(current)


def clear_cli_binary(provider: str) -> dict[str, Any]:
    """Remove the CLI binary override for ``provider``. Returns saved settings."""
    if provider not in CLI_BINARY_PROVIDERS:
        raise ValueError(f"unknown cli provider: {provider!r}")
    current = load()
    binaries = dict(current.get("cli_binaries") or {})
    binaries.pop(provider, None)
    if binaries:
        current["cli_binaries"] = binaries
    else:
        current.pop("cli_binaries", None)
    return save(current)


def apply_log_level(level: str) -> str:
    """Apply log verbosity to root and uvicorn loggers."""
    normalized = _normalize_log_level(level)
    numeric = logging.DEBUG if normalized == "debug" else logging.INFO
    for logger in (
        logging.getLogger(),
        logging.getLogger("uvicorn"),
        logging.getLogger("uvicorn.error"),
        logging.getLogger("uvicorn.access"),
    ):
        logger.setLevel(numeric)
        for handler in logger.handlers:
            handler.setLevel(numeric)
    return normalized
