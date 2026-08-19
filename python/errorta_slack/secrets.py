"""Slack app/bot token store at ``${ERRORTA_HOME}/slack/tokens.json``.

Mirrors ``errorta_app.provider_keys``'s 0600 JSON token-store pattern
(atomic tmp+rename write, never logs values, last-4 masking for GET
routes). This module MUST NOT import ``slack_sdk`` or any other optional
dependency at module load time — the Slack bridge is strictly optional
and disabled by default, so the rest of the sidecar must keep working
when ``slack-sdk`` isn't installed.

File schema::

    {"app_token": "xapp-...", "bot_token": "xoxb-..."}

Security notes:

- File is mode 0600 on Unix; best-effort (skipped with a debug log) on
  non-POSIX platforms.
- ``mask()`` returns the same keys with values reduced to
  ``"…<last4>"``. Use this for any GET/status route — never return raw
  tokens over HTTP or log them.
- ``REDACTION_PATTERNS`` matches ``xoxb-...`` / ``xapp-...`` shapes so
  callers (e.g. log/diagnostics redactors) can strip a leaked token out
  of free text even without knowing the exact configured value.
"""
from __future__ import annotations

import json
import logging
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

from errorta_slack import config

log = logging.getLogger(__name__)

# Slack token shapes: xoxb-... (bot), xapp-... (app-level). Both are
# hyphen-delimited segments of digits/letters; kept loose enough to match
# real tokens while being anchored on the distinctive prefix so it won't
# over-match unrelated text.
REDACTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bxoxb-[A-Za-z0-9-]{6,}\b"),
    re.compile(r"\bxapp-[A-Za-z0-9-]{6,}\b"),
]


def path() -> Path:
    """Return the canonical on-disk location for the Slack token file."""
    return config.slack_dir() / "tokens.json"


# Markers that only ever appear in a placeholder, never in a real token.
_PLACEHOLDER_MARKERS = ("...", "…", "<", ">")
# Long enough to reject "xapp-" and "xapp-..." while still accepting the short
# synthetic tokens the test suite uses. Deliberately NOT tuned to the real
# ~70-char length: this guard is here to catch an unedited paste, not to
# second-guess whatever Slack issues next.
_MIN_TOKEN_LEN = 10


def _validate_token(name: str, value: str, prefix: str) -> None:
    """Raise ``ValueError`` if ``value`` cannot possibly be a real token.

    The message names the field and the reason but NEVER echoes the value —
    it is user-facing and may be pasted into a bug report.
    """
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{name} is empty — nothing was written")
    if not text.startswith(prefix):
        raise ValueError(
            f"{name} must start with {prefix!r} (are the two arguments swapped?) "
            "— nothing was written"
        )
    if any(marker in text for marker in _PLACEHOLDER_MARKERS):
        raise ValueError(
            f"{name} still contains a placeholder — paste the real token from "
            "the Slack app's settings. Nothing was written."
        )
    if len(text) < _MIN_TOKEN_LEN:
        raise ValueError(
            f"{name} is too short to be a real token — nothing was written"
        )


def save_tokens(app_token: str, bot_token: str) -> None:
    """Atomically write both tokens to disk with mode 0600.

    Atomic write protects against power-loss / crash mid-write — tmpfile
    + ``os.replace`` so the file is either fully old or fully new, never
    half-written. Mode 0600 is set on the tmpfile BEFORE rename so
    there's no window where a wider mode is visible. Never logs the raw
    token values.

    Both values are validated BEFORE the file is touched, so a refused call
    leaves any existing tokens intact. This is not hypothetical: the
    documented setup one-liner carries ``xapp-...`` / ``xoxb-...``, it was
    run unedited, and the 8-character placeholders overwrote live
    credentials that could not be recovered from disk.
    """
    _validate_token("app_token", app_token, "xapp-")
    _validate_token("bot_token", bot_token, "xoxb-")

    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(
        {"app_token": app_token, "bot_token": bot_token}, indent=2, sort_keys=True
    ) + "\n"

    fd, tmp_path = tempfile.mkstemp(prefix=".tokens-", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        if os.name == "posix":
            os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
        else:
            log.debug("skipping 0600 chmod on non-POSIX platform (%s)", os.name)
        os.replace(tmp_path, p)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_tokens() -> dict[str, str] | None:
    """Read the on-disk tokens file. Returns ``None`` if absent/unreadable.

    Returns RAW token values — do not log this output; use ``mask()``
    for any operator-facing surface.
    """
    p = path()
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("tokens.json is unreadable (%s); treating as absent", exc)
        return None
    if not isinstance(raw, dict):
        return None
    app_token = raw.get("app_token")
    bot_token = raw.get("bot_token")
    if not isinstance(app_token, str) or not isinstance(bot_token, str):
        return None
    return {"app_token": app_token, "bot_token": bot_token}


def _mask_value(raw: str | None) -> str | None:
    """Reduce a token to ``"…<last4>"``. None/empty input returns None."""
    if not raw or not isinstance(raw, str):
        return None
    if len(raw) <= 4:
        return "…"
    return "…" + raw[-4:]


def mask() -> dict[str, Any]:
    """Return ``{"app_token", "bot_token"}`` with values masked to last-4.

    Safe to return over HTTP. Values are ``None`` when no tokens are
    configured.
    """
    tokens = load_tokens() or {}
    return {
        "app_token": _mask_value(tokens.get("app_token")),
        "bot_token": _mask_value(tokens.get("bot_token")),
    }


__all__ = [
    "REDACTION_PATTERNS",
    "load_tokens",
    "mask",
    "path",
    "save_tokens",
]
