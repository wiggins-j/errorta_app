"""Slack request-signature verification and team/user allowlist.

Implements Slack's v0 signing scheme (base string ``v0:{timestamp}:{body}``,
HMAC-SHA256 with the app's signing secret) plus a fail-closed team/user
allowlist gate.

This module MUST NOT import ``slack_sdk`` or any other optional dependency
at module load time — the Slack bridge is strictly optional and disabled by
default, so the rest of the sidecar must keep working when ``slack-sdk``
isn't installed. Only stdlib ``hmac``/``hashlib``/``time`` are used.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

# Slack replay-protection window, in seconds.
_TIMESTAMP_WINDOW_SECONDS = 300

_TIMESTAMP_HEADER = "x-slack-request-timestamp"
_SIGNATURE_HEADER = "x-slack-signature"


def _get_header(headers: dict[str, Any], name: str) -> str | None:
    """Case-insensitive header lookup."""
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == name:
            return value
    return None


def verify_signature(
    headers: dict[str, Any],
    body: bytes,
    signing_secret: str,
    *,
    now: float | None = None,
) -> bool:
    """Verify a Slack request's ``X-Slack-Signature`` (v0 scheme).

    Reads ``X-Slack-Request-Timestamp`` and ``X-Slack-Signature`` from
    ``headers`` case-insensitively. Fails closed: missing/malformed
    headers, a timestamp more than ``_TIMESTAMP_WINDOW_SECONDS`` from
    ``now`` (replay protection), or a signature mismatch all return
    ``False``. ``now`` is injectable (epoch seconds) for deterministic
    tests; defaults to ``time.time()``.
    """
    timestamp = _get_header(headers, _TIMESTAMP_HEADER)
    signature = _get_header(headers, _SIGNATURE_HEADER)
    if not timestamp or not signature:
        return False

    try:
        timestamp_value = float(timestamp)
    except (TypeError, ValueError):
        return False

    effective_now = now if now is not None else time.time()
    if abs(effective_now - timestamp_value) > _TIMESTAMP_WINDOW_SECONDS:
        return False

    try:
        body_text = body.decode("utf-8")
    except UnicodeDecodeError:
        return False

    base_string = f"v0:{timestamp}:{body_text}".encode("utf-8")
    digest = hmac.new(
        signing_secret.encode("utf-8"), base_string, hashlib.sha256
    ).hexdigest()
    expected_signature = f"v0={digest}"

    return hmac.compare_digest(expected_signature, signature)


def is_allowed(team_id: str, user_id: str, cfg: dict[str, Any]) -> bool:
    """Return True only if ``team_id`` and ``user_id`` are both allowlisted.

    Fail-closed: an empty (or missing) ``allowed_team_ids`` /
    ``allowed_user_ids`` list denies everyone — this is never an
    allow-all default.
    """
    allowed_teams = cfg.get("allowed_team_ids") or []
    allowed_users = cfg.get("allowed_user_ids") or []
    if not allowed_teams or not allowed_users:
        return False
    return team_id in allowed_teams and user_id in allowed_users


__all__ = ["verify_signature", "is_allowed"]
