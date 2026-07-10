"""Prompt-signature hashing — the grounding store's key.

A prompt's signature is a SHA-256 over its normalized form (stripped,
lower-cased, internal whitespace collapsed to single spaces). This is the
v0.1 key strategy; minor rephrasing intentionally misses the prior correction
(documented limitation; embedding-based keys are deferred to F024).
"""

from __future__ import annotations

import hashlib
import re

_WS = re.compile(r"\s+")


def normalize_prompt(prompt: str) -> str:
    """Strip, lower-case, and collapse internal whitespace to single spaces."""
    return _WS.sub(" ", (prompt or "").strip().lower())


def prompt_signature(prompt: str) -> str:
    """Return the SHA-256 hex digest of the normalized prompt."""
    return hashlib.sha256(normalize_prompt(prompt).encode("utf-8")).hexdigest()
