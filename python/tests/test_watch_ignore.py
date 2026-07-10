"""Tests for errorta_watch.ignore (F005 foundation)."""
from __future__ import annotations

import os

import pytest

from errorta_watch.ignore import (
    DEFAULT_IGNORES,
    DEFAULT_SUPPORTED_EXTS,
    is_cloud_sync_path,
    is_ignored,
    is_supported,
)


def test_is_ignored_default_names() -> None:
    assert is_ignored(".DS_Store") is True
    assert is_ignored("node_modules") is True
    assert is_ignored("__pycache__") is True
    assert is_ignored(".git") is True


def test_is_ignored_regular_file_is_not_ignored() -> None:
    assert is_ignored("foo.txt") is False
    assert is_ignored("report.pdf") is False


def test_is_ignored_empty_string_is_ignored() -> None:
    assert is_ignored("") is True


def test_is_ignored_hidden_files_always_ignored() -> None:
    assert is_ignored(".env") is True
    assert is_ignored(".hidden") is True


def test_is_ignored_honors_extra_patterns() -> None:
    assert is_ignored("custom_dir", extra=("custom_dir",)) is True
    assert is_ignored("custom_dir") is False


def test_is_ignored_trailing_slash_directory_pattern() -> None:
    """Callers may pass trailing-slash patterns (gitignore style) as extras.

    The current implementation matches by exact segment; a trailing-slash
    pattern matches the directory name when the slash is stripped by the
    caller. This test pins the documented behavior so future refactors
    can opt in to richer matching without surprising consumers.
    """
    # Bare segment match (the canonical shape after slash stripping).
    assert is_ignored("build", extra=("build",)) is True
    # The trailing-slash literal is preserved verbatim if passed in.
    assert is_ignored("build/", extra=("build/",)) is True


def test_is_supported_known_extensions() -> None:
    assert is_supported("foo.pdf") is True
    assert is_supported("notes.md") is True
    assert is_supported("data.CSV") is True  # case-insensitive


def test_is_supported_rejects_unknown_extensions() -> None:
    assert is_supported("foo.exe") is False
    assert is_supported("archive.zip") is False
    assert is_supported("no_extension") is False


def test_is_supported_respects_type_filter() -> None:
    assert is_supported("foo.pdf", type_filter=["pdf"]) is True
    assert is_supported("foo.md", type_filter=["pdf"]) is False
    # Leading dot in the filter is tolerated.
    assert is_supported("foo.pdf", type_filter=[".pdf"]) is True


def test_is_cloud_sync_path_detects_icloud() -> None:
    p = os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs/notes")
    assert is_cloud_sync_path(p) == "Mobile Documents/com~apple~CloudDocs"


def test_is_cloud_sync_path_detects_dropbox_and_onedrive() -> None:
    assert is_cloud_sync_path("/Users/alice/Dropbox/work") == "Dropbox"
    assert is_cloud_sync_path("/Users/alice/OneDrive/Docs") == "OneDrive"


def test_is_cloud_sync_path_returns_none_for_local_path(tmp_errorta_home) -> None:
    local = str(tmp_errorta_home / "Documents" / "corpus")
    assert is_cloud_sync_path(local) is None


def test_default_constants_are_nonempty() -> None:
    assert ".DS_Store" in DEFAULT_IGNORES
    assert ".pdf" in DEFAULT_SUPPORTED_EXTS
