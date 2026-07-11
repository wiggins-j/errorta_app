"""First-run onboarding (F147 spec §7, §11).

A friendly welcome shown when the store has no AI provider connected yet,
guiding the user to ``connect`` (and mentioning ``wizard`` / ``new``). It is
deliberately unobtrusive — golden invariant #3 (onboarding never blocks
``--json`` / non-interactive) is honored here:

* it prints to **stderr** so it never pollutes a command's stdout payload;
* it is suppressed in ``--json`` and in any non-interactive (non-TTY) session —
  those just error normally when unconfigured;
* it can be silenced entirely with ``--no-onboarding`` or
  ``ERRORTA_NO_ONBOARDING=1``;
* it fires ONLY when the store is *genuinely* unconfigured, so it stops nagging
  the moment the user connects a provider.

The decision is a **pure function** of the sidecar's provider probes so it unit
tests without a live terminal or a real sidecar: :func:`evaluate` takes a client
(mockable) and the ambient flags and returns the text to print, or ``None``.
:func:`has_real_provider` / :func:`should_show` / :func:`welcome_text` are the
factored, side-effect-free pieces.
"""
from __future__ import annotations

import os
from typing import Any, Mapping

# The env opt-out (mirrors the ``--no-onboarding`` flag).
ONBOARDING_ENV = "ERRORTA_NO_ONBOARDING"

# Providers that report ``configured: True`` unconditionally (no key / no binary
# needed — gateway.py:_provider_configured). They are ALWAYS present in the
# registry, so counting them as "configured" would make onboarding dead code;
# a genuinely fresh store is one where nothing OUTSIDE this set is set up.
_FREE_PROVIDERS = frozenset({"local", "fake"})

# Commands for which the welcome is redundant (the user is already configuring).
_SKIP_COMMANDS = frozenset({"connect"})


def _providers(payload: Any) -> list[dict]:
    """Extract the provider list from a ``GET /gateway/providers`` body."""
    if isinstance(payload, dict):
        items = payload.get("providers")
        if isinstance(items, list):
            return [p for p in items if isinstance(p, dict)]
    return []


def _keys_configured(keys_payload: Any) -> bool:
    """True if a ``GET /provider-keys`` mask shows any real key/custom entry."""
    if not isinstance(keys_payload, dict):
        return False
    for name, entry in keys_payload.items():
        if name == "custom":
            if isinstance(entry, list) and entry:
                return True
        elif isinstance(entry, dict) and entry.get("configured"):
            return True
    return False


def has_real_provider(providers_payload: Any, keys_payload: Any = None) -> bool:
    """Whether the store has a usable provider the user has actually set up.

    A provider counts when it is ``connected`` (a passed billable probe) or
    ``configured`` and NOT one of the always-on freebies (``local`` / ``fake``).
    The ``/provider-keys`` mask is a secondary corroborating signal (any api_key
    on file / any custom entry).
    """
    for provider in _providers(providers_payload):
        if provider.get("connected") is True:
            return True
        cls = str(provider.get("provider_class") or "")
        if provider.get("configured") and cls not in _FREE_PROVIDERS:
            return True
    return _keys_configured(keys_payload)


def opted_out(flag: bool = False, env: Mapping[str, str] | None = None) -> bool:
    """Whether onboarding is disabled via ``--no-onboarding`` or the env var."""
    if flag:
        return True
    source = os.environ if env is None else env
    raw = str(source.get(ONBOARDING_ENV, "")).strip().lower()
    return raw not in ("", "0", "false", "no", "off")


def should_show(
    providers_payload: Any,
    keys_payload: Any = None,
    *,
    interactive: bool,
    json_mode: bool,
    opted: bool,
    command: str | None = None,
) -> bool:
    """Pure decision: show the first-run welcome for this invocation?

    False (never nag) when: opted out, ``--json``, non-interactive, or the
    command is itself a setup command (``connect``). Otherwise True only when the
    store is genuinely unconfigured.
    """
    if opted or json_mode or not interactive:
        return False
    if command in _SKIP_COMMANDS:
        return False
    return not has_real_provider(providers_payload, keys_payload)


def welcome_text() -> str:
    """The guidance text (mentions ``connect`` + ``wizard`` + ``new``)."""
    return (
        "Welcome to Errorta — the headless Coding Council CLI.\n"
        "No AI provider is connected to this store yet. To get started:\n"
        "  errorta connect anthropic api   # or: openai / google / ollama / claudecode\n"
        "  errorta wizard                  # let the PM help you scope a project\n"
        "  errorta new <name> --repo PATH  # or set up a project directly\n"
        "Run `errorta connect status` anytime to see what's configured.\n"
        "(silence this with --no-onboarding or ERRORTA_NO_ONBOARDING=1)"
    )


def evaluate(
    client: Any,
    *,
    interactive: bool,
    json_mode: bool,
    opted: bool,
    command: str | None = None,
) -> str | None:
    """Probe the sidecar and return the welcome text to show, or ``None``.

    Best-effort: a probe failure (or any unexpected error) yields ``None`` —
    onboarding must never break or block the command the user actually ran. The
    cheap gates are checked first so a ``--json`` / non-interactive / opted-out /
    ``connect`` invocation makes NO network probe at all.
    """
    if opted or json_mode or not interactive or command in _SKIP_COMMANDS:
        return None
    try:
        providers = client.get_json("/gateway/providers")
        keys = client.get_json("/provider-keys")
    except Exception:  # noqa: BLE001 — onboarding is best-effort; never propagate
        return None
    if should_show(
        providers,
        keys,
        interactive=interactive,
        json_mode=json_mode,
        opted=opted,
        command=command,
    ):
        return welcome_text()
    return None
