"""Redaction helpers for model-gateway previews and diagnostics."""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_\-]{10,}|sk-ant-[A-Za-z0-9_\-]{10,}|"
    r"ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{12,})\b"
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")


def redact_text(text: str) -> str:
    out = _TOKEN_RE.sub("<token-redacted>", text or "")
    out = _EMAIL_RE.sub("<email-redacted>", out)
    return out


def preview_text(text: str, *, limit: int = 500) -> str:
    redacted = redact_text(text)
    if len(redacted) <= limit:
        return redacted
    return f"{redacted[:limit]}..."
