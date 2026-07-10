"""Context-window overflow classification.

Provider errors can include raw prompt fragments, so this module returns only a
stable reason code plus hashes/safe hints.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ContextOverflow:
    reason_code: str = "context_window_exceeded"
    provider_hint: str | None = None
    message_sha256: str | None = None

    def to_event_payload(self, *, retryable: bool) -> dict[str, object]:
        payload: dict[str, object] = {
            "reason": self.reason_code,
            "retryable": retryable,
        }
        if self.provider_hint:
            payload["provider_hint"] = self.provider_hint
        if self.message_sha256:
            payload["detail_sha256"] = self.message_sha256
        return payload


_OVERFLOW_PATTERNS = (
    re.compile(r"context[_ -]?length[_ -]?exceeded", re.I),
    re.compile(r"context window", re.I),
    re.compile(r"maximum context", re.I),
    re.compile(r"max(?:imum)? token", re.I),
    re.compile(r"prompt is too long", re.I),
    re.compile(r"input.*token.*exceed", re.I),
    re.compile(r"too many tokens", re.I),
    re.compile(r"num_ctx", re.I),
)

_PROVIDER_HINTS = {
    "claude_cli": ("claude cli", "claude_cli"),
    "codex_cli": ("codex cli", "codex_cli"),
    "cursor_cli": ("cursor cli", "cursor_cli"),
    "anthropic": ("anthropic", "claude"),
    "openai": ("openai", "context_length_exceeded", "gpt-"),
    "google": ("google", "gemini"),
    "local": ("ollama", "num_ctx", "local"),
}


def classify_context_overflow(exc: BaseException) -> ContextOverflow | None:
    text = _safe_exception_text(exc)
    if not text:
        return None
    if not any(pattern.search(text) for pattern in _OVERFLOW_PATTERNS):
        return None
    lowered = text.lower()
    provider_hint = None
    for provider, needles in _PROVIDER_HINTS.items():
        if any(needle in lowered for needle in needles):
            provider_hint = provider
            break
    return ContextOverflow(
        provider_hint=provider_hint,
        message_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )


def _safe_exception_text(exc: BaseException) -> str:
    parts = [type(exc).__name__, str(exc)]
    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        if value is not None:
            parts.append(str(value))
    return " ".join(p for p in parts if p).strip()[:4000]


__all__ = ["ContextOverflow", "classify_context_overflow"]
