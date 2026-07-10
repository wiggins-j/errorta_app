"""F140 — guard a dev ``code_write`` against DESTROYING an existing file.

The failure this exists to prevent (observed live in a Coding Mode project): a
dev turn emitted ``code_write`` whose ``content`` was a placeholder sentinel
(e.g. ``PRESERVE_CURRENT_FILE_AND_APPLY``) instead of the real file body, so a
~2000-line module was replaced with a one-line stub — the whole file's code and
its shared API surface deleted in a single write. A reviewer caught it after the
fact, but a destructive write must never LAND: a dev should not be able to delete
the codebase by fumbling the edit tool.

:func:`classify_destructive_write` returns a sub-reason string when a write over
an existing file looks like destruction rather than a real edit, and ``None``
otherwise; :data:`BLOCKED_REASON` is the stable code raised/recorded when it
fires. It is a CATASTROPHIC-CASE safety net, not a semantic diff — it targets the
two unambiguous shapes of the live fumble:

* a bare "keep the existing file" placeholder marker written literally over a
  file that already had real content, and
* a genuinely LARGE (codebase-scale) file blanked out or collapsed to a small
  fraction of itself.

It is deliberately conservative on the collapse rules — they fire only for large
files — so ordinary edits, refactors, and even large deletions of moderate files
that still leave real code are allowed. It does NOT try to catch a hollow rewrite
that keeps a substantial byte count (all functions replaced with ``pass`` but the
file stays big); that subtler case is the reviewer's job. The bias is fail-safe:
a blocked write is re-queued and escalated (a human sees an attention Problem),
while an allowed destructive write silently deletes the file — so where it does
fire, it errs toward blocking.

Pure/dependency-free so it is unit-testable without git or a workspace.
"""
from __future__ import annotations

# Stable reason code surfaced on the blocked-write tool event / turn outcome.
# Mirrors ``TurnErrorCode.destructive_write_blocked`` so downstream comparisons
# against the enum value hold.
BLOCKED_REASON = "destructive_write_blocked"

# High-precision "keep the current file / leave it unchanged" markers. A model
# emits one meaning "don't touch this file", but the edit tool writes it
# LITERALLY, wiping the file. Kept to unambiguous multi-word phrases and
# sentinel-shaped tokens (NOT lone English words like "unchanged" or bare
# substrings like "and_apply" that occur in ordinary identifiers such as
# ``load_and_apply``) so a real short file that merely mentions a word isn't
# misread as a sentinel. Matched case-insensitively as a substring of SHORT new
# content only, and only when the OLD file already had real content.
_PLACEHOLDER_MARKERS: tuple[str, ...] = (
    "preserve_current_file",
    "preserve_existing",
    "preserve the existing file",
    "keep_existing",
    "keep existing code",
    "keep the existing file",
    "existing code unchanged",
    "... existing code ...",
    "# ... existing code",
    "// ... existing code",
    "rest of file unchanged",
    "rest of the file unchanged",
    "rest of the file remains",
    "remainder of the file unchanged",
    "unchanged from before",
    "no changes to this file",
    "do not change this file",
    "do not modify this file",
    "leave this file unchanged",
    "<unchanged>",
)

# An OLD file is worth protecting once it holds real content. Either enough bytes
# or enough non-blank lines qualifies (a dense, short-line file still counts).
_SUBSTANTIAL_BYTES = 300
_SUBSTANTIAL_LINES = 8

# A "large" (codebase-scale) file — only these are subject to the collapse rules,
# because shrinking a moderate file to a stub is often a legitimate refactor,
# whereas gutting a large module is the "delete the codebase" incident.
_LARGE_BYTES = 2000
_LARGE_LINES = 40

# A large file "collapsed to a stub": a handful of non-blank lines that are also a
# small fraction of the old size...
_STUB_MAX_LINES = 5
_STUB_MAX_FRACTION = 0.2
# ...or (regardless of line count) gutted to a tiny fraction of the old bytes —
# catches a hollow MULTI-line rewrite that keeps few real bytes.
_LARGE_SHRINK_FRACTION = 0.15

# A placeholder marker is only meaningful when the new content is short — a real
# multi-hundred-line source file is not a sentinel even if it mentions "unchanged".
# Sized to still catch a marker padded across a few comment lines.
_PLACEHOLDER_MAX_BYTES = 800
_PLACEHOLDER_MAX_LINES = 8


def _nonblank_count(text: str) -> int:
    return sum(1 for ln in text.splitlines() if ln.strip())


def _is_substantial(old_stripped: str) -> bool:
    return (
        len(old_stripped) >= _SUBSTANTIAL_BYTES
        or _nonblank_count(old_stripped) >= _SUBSTANTIAL_LINES
    )


def _is_large(old_stripped: str) -> bool:
    return (
        len(old_stripped) >= _LARGE_BYTES
        or _nonblank_count(old_stripped) >= _LARGE_LINES
    )


def _looks_like_placeholder(new_stripped: str) -> bool:
    if not new_stripped or len(new_stripped) > _PLACEHOLDER_MAX_BYTES:
        return False
    if _nonblank_count(new_stripped) > _PLACEHOLDER_MAX_LINES:
        return False
    low = new_stripped.lower()
    return any(marker in low for marker in _PLACEHOLDER_MARKERS)


def classify_destructive_write(old: str, new: str) -> str | None:
    """Return the sub-reason a write over ``old`` with ``new`` is destructive
    (``"placeholder"`` | ``"emptied"`` | ``"truncation"`` | ``"gutted"``), else
    ``None``. The caller raises/records :data:`BLOCKED_REASON` when this is set.
    """
    old_stripped = (old or "").strip()
    new_stripped = (new or "").strip()

    # A brand-new / previously-empty file can never be "destroyed", and a file
    # without real content isn't worth protecting.
    if not _is_substantial(old_stripped):
        return None

    # 1) A bare placeholder/sentinel written over a real file — the direct fix for
    #    the live incident. Fires for any substantial file (a padded sentinel is
    #    still tiny relative to the real body).
    if _looks_like_placeholder(new_stripped):
        return "placeholder"

    # 2) A substantial file blanked out entirely. Emptying a real file is a delete,
    #    never an edit.
    if not new_stripped:
        return "emptied"

    # The collapse rules below fire only for genuinely LARGE files — shrinking a
    # moderate file to a stub can be a legitimate refactor, so it is left to the
    # reviewer; gutting a codebase-scale module is the incident.
    if not _is_large(old_stripped):
        return None
    old_len = len(old_stripped)
    new_len = len(new_stripped)
    # 3) Collapsed to a handful of lines that are also a small fraction.
    if _nonblank_count(new_stripped) <= _STUB_MAX_LINES and new_len < (
        _STUB_MAX_FRACTION * old_len
    ):
        return "truncation"
    # 4) Gutted to a tiny fraction of the bytes regardless of line count.
    if new_len < _LARGE_SHRINK_FRACTION * old_len:
        return "gutted"
    return None


__all__ = ["BLOCKED_REASON", "classify_destructive_write"]
