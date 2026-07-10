"""Per-format text extractors for F004.

Each extractor returns a list of `Chunk` dicts: {"text": str, "meta": {...}}.
The pipeline turns these into vector-store inserts.
"""
from __future__ import annotations

from typing import TypedDict


class Chunk(TypedDict):
    text: str
    meta: dict


class ExtractError(Exception):
    """Raised when an extractor cannot process a file (with a user-facing message)."""
