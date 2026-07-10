"""F086 Slice A — unit tests for the archive path-safety primitive."""
from __future__ import annotations

from pathlib import Path

import pytest

from errorta_export.safe_path import (
    UnsafePathError,
    resolve_under_root,
    safe_segment,
)


REJECT_KEYS = [
    "",
    "/etc/hosts",
    "../../etc/hosts",
    "..\\..\\etc\\hosts",
    "C:\\Windows\\win.ini",
    "C:/Windows/win.ini",
    "\\\\server\\share\\x",
    "//server/share/x",
    "a/../b",
    "a//b",          # empty segment
    "a/b/",          # trailing slash -> empty last segment
    "ok/../../x",
    "Errorta/corpora/../files/x",
    "x\x00y",        # NUL
]


@pytest.mark.parametrize("key", REJECT_KEYS)
def test_resolve_under_root_rejects(tmp_path: Path, key: str) -> None:
    with pytest.raises(UnsafePathError):
        resolve_under_root(tmp_path, key)


def test_resolve_under_root_accepts_legit_nested(tmp_path: Path) -> None:
    got = resolve_under_root(tmp_path, "Errorta/corpora/demo/files/a.txt")
    assert got == (tmp_path / "Errorta/corpora/demo/files/a.txt").resolve()
    assert tmp_path.resolve() in got.parents


def test_resolve_under_root_symlinked_root_no_false_reject(tmp_path: Path) -> None:
    # A legitimately symlinked root (e.g. macOS /var -> /private/var) must not
    # cause a legit descendant to be rejected: resolve the root once, compare
    # realpaths.
    real_root = tmp_path / "real"
    real_root.mkdir()
    link_root = tmp_path / "link"
    link_root.symlink_to(real_root, target_is_directory=True)
    got = resolve_under_root(link_root, "sub/file.txt")
    assert got == (real_root / "sub/file.txt").resolve()


def test_resolve_under_root_blocks_symlink_escape(tmp_path: Path) -> None:
    # A symlink staged INSIDE the root that points outside must be caught by the
    # realpath containment check.
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "evil").symlink_to(outside, target_is_directory=True)
    with pytest.raises(UnsafePathError):
        resolve_under_root(root, "evil/secret.txt")


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\\b", "x\x00y"])
def test_safe_segment_rejects(name: str) -> None:
    with pytest.raises(UnsafePathError):
        safe_segment(name)


def test_safe_segment_accepts() -> None:
    assert safe_segment("demo-corpus") == "demo-corpus"


def test_unsafe_path_error_carries_key_only() -> None:
    try:
        resolve_under_root(Path("/tmp/root"), "/etc/passwd")
    except UnsafePathError as exc:
        assert exc.key == "/etc/passwd"
        assert exc.code == "unsafe_bundle_member"
    else:  # pragma: no cover
        pytest.fail("expected UnsafePathError")
