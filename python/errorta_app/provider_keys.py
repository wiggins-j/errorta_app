"""F034 — provider API key store at ``~/.errorta/provider-keys.json``.

Per the user decision (2026-06-12, locked in
``docs/specs/F032-COUNCIL-MULTI-PROVIDER-ROADMAP.md`` §"Locked decisions"),
keys live in a settings JSON file edited in-app, masked on read, and
written with mode 0600.

File schema::

    {
      "anthropic": {"api_key": "sk-ant-..."},
      "openai":    {"api_key": "sk-..."},
      "google":    {"api_key": "..."},
      "custom":    [
        {"alias": "lmstudio-local",
         "base_url": "http://127.0.0.1:1234/v1",
         "auth_header": "Authorization",
         "auth_prefix": "Bearer ",
         "api_key": "lm-studio",
         "api_style": "openai_chat_completions"}
      ]
    }

Fixed-provider entries (``anthropic``, ``openai``, ``google``) carry a
single ``api_key`` field. The ``custom`` entry is a LIST so the
operator can configure multiple custom endpoints (LM Studio, vLLM, a
RunPod box, etc.) each addressed by ``custom.<alias>`` in route_ids.

Security notes:

- File is mode 0600 on Unix; logged but not enforced on Windows.
- ``mask_all()`` returns the same shape with API keys reduced to
  ``"…<last4>"``. Use this for ``GET /provider-keys``.
- The store NEVER logs keys at any log level. Caller responsibility:
  don't echo a key into a generic ``logger.info(load_all())`` —
  always mask first.
"""
from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Literal, TypedDict

from errorta_app.paths import errorta_home

log = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Typed shapes
# ----------------------------------------------------------------------


class FixedProviderConfig(TypedDict, total=False):
    """Anthropic / OpenAI / Google config."""

    api_key: str


# api_style values the custom handler understands.
ApiStyle = Literal["openai_chat_completions", "anthropic_messages", "raw"]


class CustomProviderConfig(TypedDict, total=False):
    """One ``custom`` entry. Operator addresses it as ``custom.<alias>``."""

    alias: str
    base_url: str
    api_key: str
    auth_header: str
    auth_prefix: str
    api_style: ApiStyle
    # Optional default model used when caller passes only the alias.
    model: str


class ProviderKeysFile(TypedDict, total=False):
    """The on-disk shape."""

    anthropic: FixedProviderConfig
    openai: FixedProviderConfig
    google: FixedProviderConfig
    custom: list[CustomProviderConfig]


# Fixed provider names — extend here when new fixed providers ship.
FIXED_PROVIDERS = ("anthropic", "openai", "google")

# Empty defaults for first-write.
_DEFAULTS: ProviderKeysFile = {
    "anthropic": {},
    "openai": {},
    "google": {},
    "custom": [],
}


# ----------------------------------------------------------------------
# File path
# ----------------------------------------------------------------------


def path() -> Path:
    """Return the canonical on-disk location.

    Always routed through ``errorta_app.paths.errorta_home`` so that
    ERRORTA_HOME-based test isolation works correctly. NEVER hardcode
    ``Path.home() / ".errorta"`` — that breaks the tmp_path fixtures
    every test in the suite relies on.
    """
    return errorta_home() / "provider-keys.json"


# ----------------------------------------------------------------------
# Load / save
# ----------------------------------------------------------------------


def load_all() -> ProviderKeysFile:
    """Read the on-disk keys file, applying defaults for missing fields.

    Creates an empty default file on first call. Returns RAW keys —
    do not log this output; use ``mask_all`` for any operator surface.
    """
    p = path()
    if not p.exists():
        # First-time bootstrap. Write empty defaults so subsequent reads
        # find the structure.
        save_all(_DEFAULTS)
        return dict(_DEFAULTS)  # type: ignore[return-value]

    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning(
            "provider-keys.json is unreadable (%s); returning defaults", exc
        )
        return dict(_DEFAULTS)  # type: ignore[return-value]

    # Coerce to expected shape with defaults filled in. Don't mutate raw.
    out: ProviderKeysFile = {}
    for name in FIXED_PROVIDERS:
        entry = raw.get(name) or {}
        if not isinstance(entry, dict):
            entry = {}
        out[name] = entry  # type: ignore[literal-required]
    custom_raw = raw.get("custom") or []
    if not isinstance(custom_raw, list):
        custom_raw = []
    # Each custom entry must be a dict; drop malformed ones silently.
    out["custom"] = [c for c in custom_raw if isinstance(c, dict)]
    return out


def save_all(keys: ProviderKeysFile) -> None:
    """Atomically write the keys file with mode 0600.

    Atomic write protects against power-loss / crash mid-write — we use
    tmpfile + ``os.replace`` so the file is either fully old or fully
    new, never half-written. Mode 0600 is set on the tmpfile BEFORE
    rename so there's no window where a wider mode is visible.
    """
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(keys, indent=2, sort_keys=True) + "\n"

    # tmpfile in the same dir so os.replace is atomic (same filesystem).
    fd, tmp_path = tempfile.mkstemp(
        prefix=".provider-keys-", suffix=".tmp", dir=str(p.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        # Mode 0600 — owner read/write only.
        if os.name == "posix":
            os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
        else:
            log.debug(
                "skipping 0600 chmod on non-POSIX platform (%s)", os.name
            )
        os.replace(tmp_path, p)
    except Exception:
        # Best-effort cleanup of the tmpfile on any failure.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ----------------------------------------------------------------------
# Per-provider helpers
# ----------------------------------------------------------------------


def upsert_fixed(provider: str, api_key: str) -> ProviderKeysFile:
    """Set the API key for one of anthropic / openai / google.

    Returns the updated full keys dict.
    """
    if provider not in FIXED_PROVIDERS:
        raise ValueError(f"unknown fixed provider: {provider!r}")
    if not isinstance(api_key, str) or not api_key:
        raise ValueError("api_key must be a non-empty string")
    keys = load_all()
    keys[provider] = {"api_key": api_key}  # type: ignore[literal-required]
    save_all(keys)
    return keys


def clear_fixed(provider: str) -> ProviderKeysFile:
    """Wipe one fixed provider's key.

    Sets the entry to ``{}`` (preserves the key for schema stability)
    rather than deleting it outright.
    """
    if provider not in FIXED_PROVIDERS:
        raise ValueError(f"unknown fixed provider: {provider!r}")
    keys = load_all()
    keys[provider] = {}  # type: ignore[literal-required]
    save_all(keys)
    return keys


def upsert_custom(entry: CustomProviderConfig) -> ProviderKeysFile:
    """Add or update one custom-provider entry, keyed on ``alias``.

    Validates required fields (``alias``, ``base_url``, ``api_style``).
    Returns the updated full keys dict.
    """
    alias = entry.get("alias", "").strip()
    if not alias:
        raise ValueError("custom entry missing 'alias'")
    base_url = entry.get("base_url", "").strip()
    if not base_url:
        raise ValueError("custom entry missing 'base_url'")
    api_style = entry.get("api_style", "")
    if api_style not in ("openai_chat_completions", "anthropic_messages", "raw"):
        raise ValueError(
            f"custom entry has unknown api_style: {api_style!r}"
        )
    keys = load_all()
    customs = list(keys.get("custom") or [])
    # Upsert by alias.
    customs = [c for c in customs if c.get("alias") != alias]
    customs.append(dict(entry))  # copy
    keys["custom"] = customs
    save_all(keys)
    return keys


def clear_custom(alias: str) -> ProviderKeysFile:
    """Remove the custom entry with this alias."""
    keys = load_all()
    customs = list(keys.get("custom") or [])
    keys["custom"] = [c for c in customs if c.get("alias") != alias]
    save_all(keys)
    return keys


def get_fixed_key(provider: str) -> str | None:
    """Return the raw API key for one fixed provider, or None.

    The result is sensitive — never log it. Used by handlers at call time.
    """
    if provider not in FIXED_PROVIDERS:
        return None
    keys = load_all()
    entry = keys.get(provider) or {}  # type: ignore[literal-required]
    raw = entry.get("api_key")
    return raw if isinstance(raw, str) and raw else None


def get_custom_entry(alias: str) -> CustomProviderConfig | None:
    """Return the full custom entry for ``alias``, or None.

    Sensitive — never log. Used by the custom handler at call time.
    """
    keys = load_all()
    for c in keys.get("custom") or []:
        if c.get("alias") == alias:
            return c
    return None


# ----------------------------------------------------------------------
# Masking
# ----------------------------------------------------------------------


def _mask_key(raw: str | None) -> str | None:
    """Reduce a key to ``"…<last4>"`` for safe display.

    Empty / None / shorter-than-4 inputs return ``"…"``. Used for the
    Settings UI surface; never call this from a log statement (just
    don't log keys at all).
    """
    if not raw or not isinstance(raw, str):
        return None
    if len(raw) <= 4:
        return "…"
    return "…" + raw[-4:]


def mask_all() -> dict[str, Any]:
    """Return the keys file with all raw keys replaced by ``…<last4>``.

    Safe to return over HTTP. Used by ``GET /provider-keys``.
    """
    keys = load_all()
    out: dict[str, Any] = {}
    for name in FIXED_PROVIDERS:
        entry = keys.get(name) or {}  # type: ignore[literal-required]
        raw = entry.get("api_key")
        out[name] = {
            "configured": bool(raw),
            "key_preview": _mask_key(raw),
        }
    custom_out = []
    for c in keys.get("custom") or []:
        custom_out.append({
            "alias": c.get("alias", ""),
            "base_url": c.get("base_url", ""),
            "api_style": c.get("api_style", ""),
            "auth_header": c.get("auth_header", ""),
            "auth_prefix": c.get("auth_prefix", ""),
            "model": c.get("model", ""),
            "configured": bool(c.get("api_key")),
            "key_preview": _mask_key(c.get("api_key")),
        })
    out["custom"] = custom_out
    return out


__all__ = [
    "ApiStyle",
    "CustomProviderConfig",
    "FixedProviderConfig",
    "FIXED_PROVIDERS",
    "ProviderKeysFile",
    "clear_custom",
    "clear_fixed",
    "get_custom_entry",
    "get_fixed_key",
    "load_all",
    "mask_all",
    "path",
    "save_all",
    "upsert_custom",
    "upsert_fixed",
]
