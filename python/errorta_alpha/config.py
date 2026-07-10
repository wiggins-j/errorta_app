"""Build-time configuration for the alpha gate.

The gate is a **build-time** decision, not a user-flippable runtime toggle
(spec §13): production ships with it off so the app runs keyless. We read it
from an environment variable that the packaging step bakes in
(``ERRORTA_ALPHA_GATE``); it is never surfaced in the settings UI, so a tester
cannot flip the gate back on/off from inside the app.

Defaults are deliberately the **production** posture (gate off) so that a build
which forgets to set anything behaves like v1.0, never like a half-configured
alpha.
"""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

# 14-day offline grace (spec §1/§8). Applied as ``GRACE_DAYS * 86400`` seconds;
# all timestamps are epoch seconds UTC — no calendar math anywhere (spec §16).
GRACE_DAYS = 14

_DEFAULT_API_BASE = "https://api.errorta.app"

# Embedded Ed25519 license **public** key (base64 of the 32 raw bytes). The
# private half lives only as a Cloudflare Worker secret (set via
# `gen-keypair.mjs --set-secret`) and never enters this repo. Tests don't rely
# on this constant — they inject an ephemeral keypair via ERRORTA_ALPHA_PUBKEY.
#
# This is the REAL production key for the errorta-alpha Worker (set 2026-07-01).
# Rotate by re-running `--set-secret` and replacing this value.
LICENSE_PUBKEY_B64 = "6Qou1ApJRILYTZYxOi2Rg68MqXiO/MiW/fq9C3b5J1w="


def _bundled_build_info() -> dict:
    """Read the immutable build stamp from a frozen PyInstaller bundle."""
    paths = [Path(__file__).resolve().parent.parent / "errorta_app" / "_build_info.json"]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        paths.insert(0, Path(meipass) / "errorta_app" / "_build_info.json")
    for path in paths:
        try:
            data = json.loads(path.read_text("utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError, TypeError):
            continue
    return {}


def gate_enabled() -> bool:
    """True only when this build was packaged with the alpha gate on.

    Accepts ``1/true/yes/on`` (case-insensitive). Anything else — including
    unset — is the production posture: off.
    """
    if getattr(sys, "frozen", False):
        # A packaged tester must not be able to bypass licensing by changing the
        # process environment. Missing/malformed stamps fail to production-off.
        return _bundled_build_info().get("alpha_gate_enabled") is True
    raw = (os.environ.get("ERRORTA_ALPHA_GATE") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def api_base_url() -> str:
    """Base URL for the check-in service. Overridable for staging/tests via
    ``ERRORTA_ALPHA_API`` (e.g. a local stub Worker), else ``api.errorta.app``.
    Trailing slashes are trimmed so callers can join ``/v1/...`` cleanly."""
    raw = (os.environ.get("ERRORTA_ALPHA_API") or "").strip() or _DEFAULT_API_BASE
    return raw.rstrip("/")


def license_public_key_b64() -> str:
    """The base64 Ed25519 public key to verify license tokens against.

    Source/test runs may override it with ``ERRORTA_ALPHA_PUBKEY`` for staging
    keys and harnesses. Frozen builds always use the embedded constant so a
    process environment change cannot substitute an attacker-controlled key.
    """
    if getattr(sys, "frozen", False):
        return LICENSE_PUBKEY_B64
    return (os.environ.get("ERRORTA_ALPHA_PUBKEY") or "").strip() or LICENSE_PUBKEY_B64


def license_public_key_raw() -> bytes:
    """Decode the active public key to 32 raw bytes; raises on a malformed key."""
    key = license_public_key_b64()
    raw = base64.b64decode(key)
    if len(raw) != 32:
        raise ValueError("license public key must be 32 raw Ed25519 bytes")
    return raw
