"""ERRORTA_HOME resolution mirrors paths.py; cwd↔project resolution.

The resolution order asserted here is deliberately identical to
``errorta_app.paths.errorta_home`` (``$ERRORTA_HOME`` > legacy > ``~/.errorta``);
it is re-implemented in ``config`` rather than imported so the CLI stays
client-only (golden invariant #1).
"""
from __future__ import annotations

import json
from pathlib import Path

from errorta_cli import config


def _clear_home_env(monkeypatch) -> None:
    for name in ("ERRORTA_HOME", "ERRORTA_STATE_DIR", "ERRORTA_DATA_DIR"):
        monkeypatch.delenv(name, raising=False)


def test_canonical_env_wins(monkeypatch, tmp_path) -> None:
    _clear_home_env(monkeypatch)
    target = tmp_path / "canonical"
    monkeypatch.setenv("ERRORTA_HOME", str(target))
    assert config.resolve_home() == target
    assert target.is_dir()


def test_override_beats_env(monkeypatch, tmp_path) -> None:
    _clear_home_env(monkeypatch)
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path / "env"))
    override = tmp_path / "override"
    assert config.resolve_home(str(override)) == override


def test_legacy_env_used_when_canonical_absent(monkeypatch, tmp_path) -> None:
    _clear_home_env(monkeypatch)
    legacy = tmp_path / "legacy-state"
    monkeypatch.setenv("ERRORTA_STATE_DIR", str(legacy))
    assert config.resolve_home() == legacy


def test_canonical_beats_legacy(monkeypatch, tmp_path) -> None:
    _clear_home_env(monkeypatch)
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path / "canon"))
    monkeypatch.setenv("ERRORTA_STATE_DIR", str(tmp_path / "legacy"))
    assert config.resolve_home() == tmp_path / "canon"


def test_default_home_is_dot_errorta(monkeypatch, tmp_path) -> None:
    _clear_home_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert config.resolve_home() == tmp_path / ".errorta"


def test_pointer_read_and_write(tmp_path) -> None:
    config.write_pointer(tmp_path, "proj-abc")
    assert config.read_pointer(tmp_path) == "proj-abc"


def test_pointer_found_in_ancestor(tmp_path) -> None:
    config.write_pointer(tmp_path, "proj-root")
    child = tmp_path / "a" / "b"
    child.mkdir(parents=True)
    assert config.read_pointer(child) == "proj-root"


def test_pointer_accepts_bare_line(tmp_path) -> None:
    (tmp_path / config.POINTER_FILENAME).write_text("proj-bare\n", "utf-8")
    assert config.read_pointer(tmp_path) == "proj-bare"


def _make_project(home: Path, project_id: str, **fields) -> Path:
    pdir = config.coding_projects_dir(home) / project_id
    pdir.mkdir(parents=True)
    (pdir / "project.json").write_text(
        json.dumps({"id": project_id, **fields}), "utf-8"
    )
    return pdir


def test_resolve_project_prefers_pointer(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    _make_project(home, "by-path", repo_path=str(work))
    config.write_pointer(work, "by-pointer")
    assert config.resolve_project_id(home, work) == "by-pointer"


def test_resolve_project_falls_back_to_repo_path(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _make_project(home, "matched", repo_path=str(repo))
    assert config.resolve_project_id(home, repo / "src") == "matched"


def test_resolve_project_matches_delivery_root(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    delivery = tmp_path / "deliver"
    delivery.mkdir()
    _make_project(home, "delivered", delivery_root=str(delivery))
    assert config.resolve_project_id(home, delivery) == "delivered"


def test_resolve_project_none_when_unbound(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _make_project(home, "somewhere", repo_path=str(tmp_path / "other"))
    assert config.resolve_project_id(home, elsewhere) is None
