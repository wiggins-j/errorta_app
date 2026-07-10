"""Redaction helpers for AIAR connection status and diagnostics."""

from __future__ import annotations


def mask_secret(raw: str | None) -> str | None:
    if not raw:
        return None
    if len(raw) <= 4:
        return "..."
    return "..." + raw[-4:]


def redact_text(text: str, secret: str | None) -> str:
    if secret and secret in text:
        return text.replace(secret, "<redacted>")
    return text
