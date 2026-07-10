"""Ignore patterns and cloud-sync path detection for F005."""
from __future__ import annotations

import os
from typing import Iterable

# Default ignore list. Names are checked path-segment-by-segment.
DEFAULT_IGNORES: tuple[str, ...] = (
    "node_modules",
    "__pycache__",
    "__MACOSX",
    ".DS_Store",
    "Thumbs.db",
    ".git",
    ".venv",
    ".idea",
    ".vscode",
)

# Default supported extensions reused from the F004 extraction pipeline.
DEFAULT_SUPPORTED_EXTS: tuple[str, ...] = (
    ".pdf",
    ".docx",
    ".md",
    ".txt",
    ".xlsx",
    ".csv",
    ".html",
    ".htm",
    ".rtf",
)

# Substrings that indicate a cloud-synced root.
_CLOUD_SYNC_MARKERS: tuple[str, ...] = (
    "Dropbox",
    "iCloud Drive",
    "Mobile Documents/com~apple~CloudDocs",
    "OneDrive",
    "Google Drive",
    "GoogleDrive",
    "CloudStorage",
    "Box Sync",
)


def is_ignored(name: str, extra: Iterable[str] = ()) -> bool:
    """Return True if a single path segment should be ignored.

    Hidden files (starting with ``.``) are always ignored. The default list
    plus any extras the caller supplies are checked by exact match.
    """
    if not name:
        return True
    if name.startswith("."):
        return True
    if name in DEFAULT_IGNORES:
        return True
    if name in tuple(extra):
        return True
    return False


def is_cloud_sync_path(path: str) -> str | None:
    """If ``path`` looks like a cloud-sync folder, return the provider name.

    Returns None otherwise. Detection is best-effort substring match against
    common provider paths on macOS.
    """
    normalized = os.path.normpath(path)
    for marker in _CLOUD_SYNC_MARKERS:
        if marker in normalized:
            return marker
    return None


def is_supported(path: str, type_filter: Iterable[str] | None = None) -> bool:
    """Check if a file path should be ingested.

    If ``type_filter`` is provided, only those extensions (without dot, e.g.
    ``["pdf", "docx"]``) are considered supported. Otherwise the default
    extension list is used.
    """
    ext = os.path.splitext(path)[1].lower()
    if not ext:
        return False
    if type_filter:
        wanted = {("." + e.lstrip(".")).lower() for e in type_filter}
        return ext in wanted
    return ext in DEFAULT_SUPPORTED_EXTS
