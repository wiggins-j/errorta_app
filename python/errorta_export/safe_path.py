"""F086 Slice A — path-safety primitive for untrusted archive manifest keys.

A bundle manifest (export or brief) is attacker-controlled: a crafted bundle can
ship a manifest whose file key / corpus name / brief id is an absolute path or
contains ``..`` segments. Any such value joined to a filesystem root without
validation lets the bundle read or write OUTSIDE the staging/target root (path
traversal) — and the hash of an out-of-tree file leaking back in an error is an
information-disclosure oracle.

Every manifest-supplied key/name that is joined to a root must pass through here
first. Fail-closed: reject before touching the filesystem, then realpath-assert
containment as defense-in-depth against symlinks staged inside the archive.
"""
from __future__ import annotations

import re
from pathlib import Path

_WIN_DRIVE = re.compile(r"^[A-Za-z]:")


class UnsafePathError(ValueError):
    """A manifest-supplied key/name is unsafe to join to a filesystem root.

    The string form carries the offending KEY only — never a resolved absolute
    path or a file hash, both of which would be an information-disclosure
    oracle if surfaced in an HTTP error body.
    """

    code = "unsafe_bundle_member"

    def __init__(self, key: str, reason: str) -> None:
        super().__init__(f"unsafe bundle member ({reason}): {key!r}")
        self.key = key
        self.reason = reason


def _validate_relative_key(key: str) -> None:
    """Raise UnsafePathError unless ``key`` is a safe relative, ``/``-joined path.

    Backslashes are normalized to ``/`` first — Windows-exported bundles use
    them and the importer already does ``rel.replace('\\\\', '/')`` elsewhere —
    so a Windows-style traversal collapses to the same ``..``-segment rejection
    rather than being accepted as a literal filename.
    """
    if not isinstance(key, str) or key == "":
        raise UnsafePathError(str(key), "empty")
    if "\x00" in key:
        raise UnsafePathError(key, "NUL byte")
    norm = key.replace("\\", "/")
    if norm.startswith("/"):
        raise UnsafePathError(key, "absolute or UNC")
    if _WIN_DRIVE.match(norm):
        raise UnsafePathError(key, "windows drive")
    for seg in norm.split("/"):
        if seg in ("", ".", ".."):
            # empty rejects 'a//b' and trailing '/'; '.'/'..' reject traversal.
            raise UnsafePathError(key, "unsafe segment")


def resolve_under_root(root: Path, key: str) -> Path:
    """Join ``key`` under ``root`` safely; return the resolved absolute path.

    Raises :class:`UnsafePathError` if ``key`` is absolute/UNC/drive-qualified,
    contains a ``..``/``.``/empty segment or a NUL, or if the resolved path
    escapes ``root`` (symlink defense). ``root`` is resolved once so a
    legitimately symlinked home (e.g. ``/var`` -> ``/private/var`` on macOS)
    does not false-reject its own descendants.
    """
    _validate_relative_key(key)
    root_r = root.resolve()
    cand_r = (root / key.replace("\\", "/")).resolve()
    if cand_r != root_r and root_r not in cand_r.parents:
        raise UnsafePathError(key, "escapes root")
    return cand_r


def safe_segment(name: str) -> str:
    """Return ``name`` if it is a single safe path component, else raise.

    For names used directly as ONE directory/file component (corpus name, brief
    id, filename) rather than a multi-segment relative key. Rejects empty,
    ``.``/``..``, and any value containing a separator or NUL.
    """
    if not isinstance(name, str) or name in ("", ".", ".."):
        raise UnsafePathError(str(name), "unsafe segment")
    if "/" in name or "\\" in name or "\x00" in name:
        raise UnsafePathError(name, "unsafe segment")
    return name
