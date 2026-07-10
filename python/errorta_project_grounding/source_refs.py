"""F088-04/05 — provenance helpers for the memory-ingestion layer.

Builds ``MemorySourceRef`` + ``MemoryFreshness`` and stable, source-derived
``memory_id``s from F087 ledger rows so ``sync_from_ledger`` is idempotent
(re-running it ``INSERT OR REPLACE``s the same row rather than duplicating it).

The sensitive-path screen is the SAME denylist the F088-03 corpus bootstrap
uses, imported (not re-declared) so the ingestion layer can never index a path
the bootstrap would itself refuse.
"""
from __future__ import annotations

import re
from pathlib import Path

from .bootstrap import DENY_PARTS, SENSITIVE_NAMES
from .memory_store import MemoryFreshness, _now

_ID_UNSAFE = re.compile(r"[^A-Za-z0-9]+")
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx")


def _slug(value: object, *, cap: int = 96) -> str:
    s = _ID_UNSAFE.sub("_", str(value)).strip("_")
    return s[:cap] or "x"


def memory_id(*parts: object) -> str:
    """Stable id from source parts. Two syncs of the same ledger row produce the
    same id, so the store's INSERT OR REPLACE keeps exactly one record."""
    return "mem_" + "_".join(_slug(p) for p in parts if p not in (None, ""))


def is_sensitive_path(path: str | None) -> bool:
    """True if ``path`` is on the bootstrap denylist (build/cache dirs), is a
    hidden/dotfile path, or names a secret. Mirrors F088-03 bootstrap's
    ``_skip_reason`` so durable code-chunk promotion never anchors a secret."""
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
    if name in {s.lower() for s in SENSITIVE_NAMES} or name.endswith(_SECRET_SUFFIXES):
        return True
    return False


def freshness(head: str | None, *, index_version: int = 1) -> MemoryFreshness:
    return MemoryFreshness(
        indexed_at=_now(),
        source_head=(str(head) if head else None),
        index_version=index_version,
    )


__all__ = ["memory_id", "is_sensitive_path", "freshness"]
