"""F105 Slice A — greenfield delivery-root model + create/accept validation."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

_TAURI = {"x-errorta-origin": "tauri-ui"}


def _client() -> TestClient:
    from errorta_app.routes import coding as coding_routes

    app = FastAPI()
    app.include_router(coding_routes.router)
    return TestClient(app, headers=_TAURI)


# --- unit: _validate_delivery_root ------------------------------------------


def test_blank_and_none_delivery_root_default(tmp_errorta_home, tmp_path) -> None:
    from errorta_app.routes.coding import _validate_delivery_root

    assert _validate_delivery_root(None) is None
    assert _validate_delivery_root("") is None
    assert _validate_delivery_root("   ") is None


def test_custom_root_is_normalized_and_returned(tmp_errorta_home, tmp_path) -> None:
    from errorta_app.routes.coding import _validate_delivery_root

    target = tmp_path / "Projects"
    target.mkdir()
    got = _validate_delivery_root(str(target))
    assert got == str(target.resolve())


def test_relative_path_rejected_via_nonexistence(tmp_errorta_home, tmp_path) -> None:
    from errorta_app.routes.coding import _validate_delivery_root

    # A non-existent / non-directory root is rejected.
    with pytest.raises(HTTPException) as exc:
        _validate_delivery_root(str(tmp_path / "does-not-exist"))
    assert exc.value.status_code == 422


def test_non_directory_root_rejected(tmp_errorta_home, tmp_path) -> None:
    from errorta_app.routes.coding import _validate_delivery_root

    f = tmp_path / "a-file.txt"
    f.write_text("x")
    with pytest.raises(HTTPException) as exc:
        _validate_delivery_root(str(f))
    assert exc.value.status_code == 422
    assert "not a directory" in str(exc.value.detail)


def test_filesystem_root_rejected(tmp_errorta_home) -> None:
    from errorta_app.routes.coding import _validate_delivery_root

    with pytest.raises(HTTPException) as exc:
        _validate_delivery_root("/")
    assert exc.value.status_code == 422


def test_home_directory_itself_rejected(tmp_errorta_home, monkeypatch, tmp_path) -> None:
    from errorta_app.routes import coding as coding_routes

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    with pytest.raises(HTTPException) as exc:
        coding_routes._validate_delivery_root(str(fake_home))
    assert exc.value.status_code == 422
    assert "home" in str(exc.value.detail)


@pytest.mark.parametrize("protected", ["/usr", "/etc", "/System", "/Library"])
def test_protected_posix_root_rejected(tmp_errorta_home, protected) -> None:
    from errorta_app.routes.coding import _validate_delivery_root

    # Only run for roots that (a) exist as a directory and (b) do not resolve
    # through a symlink to a non-denied location (e.g. macOS /var -> /private/var,
    # which intentionally is NOT blocked so the per-user temp dir stays usable).
    p = Path(protected)
    if not p.is_dir() or p.resolve() != p:
        pytest.skip(f"{protected} not a real protected dir on this host")
    with pytest.raises(HTTPException) as exc:
        _validate_delivery_root(protected)
    assert exc.value.status_code == 422


def test_errorta_home_and_under_it_rejected(tmp_errorta_home) -> None:
    from errorta_app.paths import errorta_home
    from errorta_app.routes.coding import _validate_delivery_root

    home = errorta_home()
    with pytest.raises(HTTPException) as exc:
        _validate_delivery_root(str(home))
    assert exc.value.status_code == 422

    under = home / "council"
    under.mkdir(parents=True, exist_ok=True)
    with pytest.raises(HTTPException) as exc2:
        _validate_delivery_root(str(under))
    assert exc2.value.status_code == 422


def test_hidden_home_dotdir_rejected(tmp_errorta_home, monkeypatch, tmp_path) -> None:
    from errorta_app.routes import coding as coding_routes

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    for hidden in (".ssh", ".config", ".errorta"):
        d = fake_home / hidden
        d.mkdir(exist_ok=True)
        with pytest.raises(HTTPException) as exc:
            coding_routes._validate_delivery_root(str(d))
        assert exc.value.status_code == 422, hidden


def test_normal_subdir_under_home_allowed(tmp_errorta_home, monkeypatch, tmp_path) -> None:
    from errorta_app.routes import coding as coding_routes

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    d = fake_home / "Projects"
    d.mkdir()
    assert coding_routes._validate_delivery_root(str(d)) == str(d.resolve())


# --- route: create stores the validated root + planned_delivery_dir ----------


def test_create_default_planned_dir(tmp_errorta_home, monkeypatch) -> None:
    monkeypatch.delenv("ERRORTA_DELIVERABLES_DIR", raising=False)
    c = _client()
    r = c.post("/coding/projects", json={"project_id": "pdefault",
               "north_star": "n", "definition_of_done": "d", "target": "new"})
    assert r.status_code == 200, r.text
    proj = r.json()["project"]
    assert proj["delivery_root"] is None
    planned = Path(proj["planned_delivery_dir"])
    assert planned.name == "pdefault"
    assert planned.parent.name == "Errorta Projects"


def test_create_with_custom_root_persists_normalized(tmp_errorta_home, tmp_path) -> None:
    root = tmp_path / "my projects"
    root.mkdir()
    c = _client()
    r = c.post("/coding/projects", json={"project_id": "pcustom",
               "north_star": "n", "definition_of_done": "d", "target": "new",
               "delivery_root": str(root)})
    assert r.status_code == 200, r.text
    proj = r.json()["project"]
    assert proj["delivery_root"] == str(root.resolve())
    assert proj["planned_delivery_dir"] == str((root / "pcustom").resolve())

    # And it round-trips on GET.
    got = c.get("/coding/projects/pcustom").json()["project"]
    assert got["delivery_root"] == str(root.resolve())


def test_create_protected_root_returns_422(tmp_errorta_home, tmp_path) -> None:
    f = tmp_path / "afile"
    f.write_text("x")
    c = _client()
    r = c.post("/coding/projects", json={"project_id": "pbad",
               "north_star": "n", "definition_of_done": "d", "target": "new",
               "delivery_root": str(f)})
    assert r.status_code == 422, r.text


def test_existing_target_forces_delivery_root_none(tmp_errorta_home, tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    other = tmp_path / "Projects"
    other.mkdir()
    c = _client()
    # Even if a delivery_root is supplied, an existing target stores None and has
    # no planned_delivery_dir.
    r = c.post("/coding/projects", json={"project_id": "pexisting",
               "north_star": "n", "definition_of_done": "d", "target": "existing",
               "repo_path": str(repo), "delivery_root": str(other)})
    assert r.status_code == 200, r.text
    proj = r.json()["project"]
    assert proj["delivery_root"] is None
    assert proj["planned_delivery_dir"] is None


def test_existing_repo_path_validation_unchanged(tmp_errorta_home, tmp_path) -> None:
    # A bad existing repo_path still 422s (no regression from F105).
    c = _client()
    r = c.post("/coding/projects", json={"project_id": "pnorepo",
               "north_star": "n", "definition_of_done": "d", "target": "existing",
               "repo_path": str(tmp_path / "nope")})
    assert r.status_code == 422, r.text
