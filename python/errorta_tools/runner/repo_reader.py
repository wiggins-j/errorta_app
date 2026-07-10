"""F135 — bounded, skip-set-honoring read of a real repo for the orientation scan.

The orientation scan (North Star inference) needs to read a user's repo WITHOUT
copying it into an ApplyWorkspace first. It reads the real ``repo_path`` directly
and returns a small, capped text blob plus the list of files it read (so the
proposal can cite its sources).

Two hard rules (F135 Review #10):
  * honor the same skip-set the apply-workspace copy uses (``.git``, ``node_modules``,
    ``.env`` + variants, key/secret suffixes, ...) so secrets never reach the model;
  * never follow a symlink out of the tree.

This lives in ``errorta_tools`` (the sanctioned filesystem-read boundary); the
council-side scan calls into here.
"""
from __future__ import annotations

import os
from pathlib import Path

from .apply_workspace import (
    _ENV_EXAMPLE_NAMES,
    _SECRET_SUFFIXES,
    _SKIPPED_DIR_NAMES,
    _SKIPPED_FILE_NAMES,
)

# README + common dependency/manifest files, read first (highest signal for "what
# is this project"). Matched case-insensitively by exact name.
_PRIORITY_NAMES = (
    "readme.md", "readme.rst", "readme.txt", "readme",
    "pyproject.toml", "package.json", "go.mod", "cargo.toml", "pom.xml",
    "build.gradle", "gemfile", "composer.json", "requirements.txt",
    "setup.py", "setup.cfg", "makefile", "dockerfile",
)

_DEFAULT_TOTAL_CAP = 24_000      # ~6k tokens of context for the scan
_PER_FILE_CAP = 6_000            # don't let one file dominate
_MAX_FILES = 40

# F135 hardening: repo_reader's blob is sent to a MODEL PROVIDER (unlike the
# apply-workspace copy, whose content stays local). So it applies extra secret
# guards ON TOP of the shared skip-set, scoped to this egress path (the shared
# apply_workspace set is intentionally left unchanged):
#   * key/keystore suffixes the shared set misses;
#   * a content check that drops any file carrying a private-key PEM block —
#     catches credential files with non-standard names (e.g. a GCP
#     service-account JSON named `credentials.json`) that no name list would.
_EXTRA_SECRET_SUFFIXES = (".p8", ".jks", ".keystore", ".pkcs12")
_PRIVATE_KEY_MARKERS = (
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN EC PRIVATE KEY-----",
    "-----BEGIN DSA PRIVATE KEY-----",
    "-----BEGIN PGP PRIVATE KEY BLOCK-----",
)


def _has_private_key(text: str) -> bool:
    return any(marker in text for marker in _PRIVATE_KEY_MARKERS)


def _is_secret_file(name: str) -> bool:
    lower = name.lower()
    if lower in _SKIPPED_FILE_NAMES:
        return True
    # .env, .env.local, ... are secret; .env.example/.sample/.template are safe.
    if lower == ".env" or (lower.startswith(".env") and lower not in _ENV_EXAMPLE_NAMES):
        return True
    return any(lower.endswith(suf) for suf in _SECRET_SUFFIXES) or any(
        lower.endswith(suf) for suf in _EXTRA_SECRET_SUFFIXES)


def _iter_files(root: Path):
    """Walk ``root`` yielding (rel_path, abs_path), skipping skipped dirs, secret
    files, and symlinks (dirs or files). Deterministic order (sorted)."""
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # prune skipped + symlinked dirs in place so os.walk doesn't descend
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in _SKIPPED_DIR_NAMES
            and not os.path.islink(os.path.join(dirpath, d))
        )
        for name in sorted(filenames):
            abs_path = Path(dirpath) / name
            if abs_path.is_symlink():
                continue
            if _is_secret_file(name):
                continue
            try:
                if not abs_path.resolve().is_relative_to(root):
                    continue  # symlink-escape defense-in-depth
            except (OSError, ValueError):
                continue
            yield abs_path.relative_to(root), abs_path


def _read_text(path: Path, cap: int) -> str | None:
    try:
        data = path.read_bytes()[: cap + 1]
    except OSError:
        return None
    if b"\x00" in data:
        return None  # binary
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text[:cap]


def read_bounded(repo_path: str | Path, *, total_cap: int = _DEFAULT_TOTAL_CAP,
                 per_file_cap: int = _PER_FILE_CAP,
                 max_files: int = _MAX_FILES) -> dict:
    """Return ``{blob, files, has_readme, empty}``.

    ``blob`` is a capped, source-labeled text digest (README + manifests first,
    then other text files); ``files`` is the list of relative paths read;
    ``has_readme`` flags whether a README was found; ``empty`` is True when there
    was no readable text at all (an empty/binary-only repo)."""
    root = Path(repo_path).expanduser()
    if not root.is_dir():
        return {"blob": "", "files": [], "has_readme": False, "empty": True}

    all_files = list(_iter_files(root))
    # priority files first (README, manifests), then the rest, both sorted.
    def _rank(item):
        rel, _ = item
        low = rel.name.lower()
        return (0, _PRIORITY_NAMES.index(low)) if low in _PRIORITY_NAMES else (1, 0)
    ordered = sorted(all_files, key=lambda it: (_rank(it), str(it[0])))

    parts: list[str] = []
    read_files: list[str] = []
    has_readme = False
    used = 0
    for rel, abs_path in ordered:
        if len(read_files) >= max_files or used >= total_cap:
            break
        remaining = total_cap - used
        text = _read_text(abs_path, min(per_file_cap, remaining))
        if not text or not text.strip():
            continue
        if _has_private_key(text):
            continue  # F135: never send a file carrying a private key to the model
        rel_str = rel.as_posix()
        if rel.name.lower().startswith("readme"):
            has_readme = True
        chunk = f"===== {rel_str} =====\n{text}\n"
        parts.append(chunk)
        used += len(chunk)
        read_files.append(rel_str)

    blob = "".join(parts)
    return {
        "blob": blob,
        "files": read_files,
        "has_readme": has_readme,
        "empty": not read_files,
    }
