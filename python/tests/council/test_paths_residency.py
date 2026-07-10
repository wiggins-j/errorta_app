"""Invariant 8 — all council paths resolve under errorta_home()."""
from __future__ import annotations

from pathlib import Path

import pytest

from errorta_council import paths as council_paths


def test_council_root_under_tmp_home(tmp_errorta_home: Path) -> None:
    root = council_paths.council_root()
    assert root == tmp_errorta_home / ".errorta" / "council"
    assert root.is_dir()


def test_rooms_dir_under_tmp_home(tmp_errorta_home: Path) -> None:
    rooms = council_paths.rooms_dir()
    assert rooms == tmp_errorta_home / ".errorta" / "council" / "rooms"
    assert rooms.is_dir()


def test_runs_dir_under_tmp_home(tmp_errorta_home: Path) -> None:
    runs = council_paths.runs_dir()
    assert runs == tmp_errorta_home / ".errorta" / "council" / "runs"
    assert runs.is_dir()


def test_deleted_rooms_dir_under_tmp_home(tmp_errorta_home: Path) -> None:
    deleted = council_paths.deleted_rooms_dir()
    assert deleted == tmp_errorta_home / ".errorta" / "council" / "rooms" / "deleted"
    assert deleted.is_dir()


def test_remote_active_root_via_errorta_home_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 'remote active root' simulation proves no hardcoded ~/.errorta."""
    remote = tmp_path / "remote-active-root"
    remote.mkdir()
    monkeypatch.setenv("ERRORTA_HOME", str(remote))
    root = council_paths.council_root()
    assert root == remote / "council"
    assert root.is_dir()
