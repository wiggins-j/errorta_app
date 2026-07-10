"""F088 — single shared safe-index policy.

Before this module, the corpus bootstrap (``bootstrap.py``) and the memory store
(``memory_store.py``) each carried their own, slightly different deny rules for
paths and secret-bearing filenames. A divergence there is a real leak risk: a
path the store would reject could still be indexed by bootstrap (or vice versa).
This module is the ONE place that decides:

* which path segments / filenames are never indexed (build dirs, caches, hidden
  dotfiles, key material), and
* whether a chunk of *content* looks like a secret (so a memory record carrying
  a leaked key is rejected, not just a suspicious filename).

It also defines the memory-content size cap so a single oversized record can't
bloat the index. Both subsystems import from here; the bootstrap re-exports the
legacy ``DENY_PARTS`` / ``SENSITIVE_NAMES`` names for any existing importer.
"""
from __future__ import annotations

import re
from pathlib import Path

# Directories whose contents are never indexed (generated / vendored / VCS).
DENY_PARTS: frozenset[str] = frozenset({
    ".git", ".errorta", "node_modules", ".venv", "venv", "dist", "build",
    "coverage", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    "target", ".ssh", ".aws", ".gcloud", "diagnostics",
})
# Filenames that name secret material.
SENSITIVE_NAMES: frozenset[str] = frozenset({
    ".env", ".env.local", "id_rsa", "id_ed25519", "known_hosts", "credentials",
    "credentials.json", ".netrc", ".pgpass",
})
SECRET_SUFFIXES: tuple[str, ...] = (".pem", ".key", ".p12", ".pfx")

# A single memory record's content is capped so the index can't be bloated by
# one oversized row (and so a runaway dump can't smuggle a huge secret blob).
MAX_MEMORY_CONTENT_BYTES = 16_384

# High-confidence secret signatures. Conservative on purpose: a false positive
# only blocks ONE record from indexing, but a false negative leaks a credential
# into project memory. Patterns mirror errorta_diagnostics redaction intent.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),            # AWS access key id
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}"),     # Anthropic key
    re.compile(r"\bsk-[A-Za-z0-9]{32,}"),           # OpenAI-style key
    re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),         # GitHub PAT
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack token
    re.compile(r"(?i)\b(aws_secret_access_key|api[_-]?secret)\b\s*[=:]\s*\S{12,}"),
)


def is_sensitive_path(path: str | None) -> bool:
    """True if ``path`` is on the deny-list (build/cache/VCS dir), is a
    hidden/dotfile path, or names secret material. The single rule both the
    bootstrap planner and the memory store consult."""
    if not path:
        return False
    p = Path(path)
    segs = [seg for seg in p.parts if seg]
    lowered = {seg.lower() for seg in segs}
    if lowered & {d.lower() for d in DENY_PARTS}:
        return True
    if any(seg.startswith(".") for seg in segs):
        return True
    name = p.name.lower()
    if name in {s.lower() for s in SENSITIVE_NAMES} or name.endswith(SECRET_SUFFIXES):
        return True
    return False


def content_has_secret(text: str | None) -> bool:
    """True if ``text`` contains a high-confidence secret signature."""
    if not text:
        return False
    return any(pat.search(text) for pat in _SECRET_PATTERNS)


__all__ = [
    "DENY_PARTS", "SENSITIVE_NAMES", "SECRET_SUFFIXES",
    "MAX_MEMORY_CONTENT_BYTES", "is_sensitive_path", "content_has_secret",
]
