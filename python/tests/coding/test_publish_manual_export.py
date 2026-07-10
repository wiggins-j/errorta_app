from __future__ import annotations

import zipfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

_TAURI = {"x-errorta-origin": "tauri-ui"}


def _client(*, headers: dict[str, str] | None = None) -> TestClient:
    from errorta_app.routes import coding as coding_routes

    app = FastAPI()
    app.include_router(coding_routes.router)
    return TestClient(app, headers=headers)


def _project(project_id: str, *, delivery_root: Path | None = None):
    """Create a greenfield project + worktree with a couple of tracked files."""
    from errorta_council.coding.ledger import LedgerStore
    from errorta_council.coding.workspace import CodingWorkspace

    store = LedgerStore(project_id)
    store.create_project(
        north_star="n",
        definition_of_done="d",
        target="new",
        repo_path=None,
        delivery_root=str(delivery_root) if delivery_root else None,
    )
    ws = CodingWorkspace(project_id, store)
    ws.setup(target="new", repo_path=None)
    ws.write_file("src/app.py", "print('hello')\n", task_id="t1")
    ws.write_file("README.md", "# demo\n", task_id="t1")
    return store, ws


# --- direct egress (build_zip_export / build_patch) ----------------------- #


def test_build_zip_export_contains_tracked_files_and_no_git(
    tmp_errorta_home: Path, tmp_path: Path,
) -> None:
    from errorta_tools.runner.publish import build_zip_export

    _store, ws = _project("zip-egress")
    dest = tmp_path / "out.zip"
    built = build_zip_export(ws.root(), dest, ref="master")
    assert built == dest and dest.is_file()

    with zipfile.ZipFile(dest) as zf:
        names = set(zf.namelist())
    assert "src/app.py" in names
    assert "README.md" in names
    # No .git directory entries leak into the deliverable.
    assert not any(n == ".git" or n.startswith(".git/") for n in names)


def test_build_patch_matches_workspace_diff(tmp_errorta_home: Path) -> None:
    from errorta_tools.runner.publish import build_patch

    _store, ws = _project("patch-egress")
    patch = build_patch(ws.root(), ref="master")
    # The cumulative diff vs the empty baseline includes the added files.
    assert "src/app.py" in patch
    assert "README.md" in patch
    assert "+print('hello')" in patch
    # And it equals the workspace's own cumulative diff.
    assert patch == ws._ws.cumulative_diff()


# --- routes --------------------------------------------------------------- #


def test_manual_export_zip_route_records_redacted_event(
    tmp_errorta_home: Path, tmp_path: Path,
) -> None:
    delivery = tmp_path / "delivered"
    delivery.mkdir()
    _project("manual-zip", delivery_root=delivery)

    resp = _client(headers=_TAURI).post(
        "/coding/projects/manual-zip/publish/manual-export",
        json={"kind": "zip"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "zip"
    assert Path(body["path"]).is_file()
    with zipfile.ZipFile(body["path"]) as zf:
        assert "src/app.py" in zf.namelist()

    # The event was recorded.
    events = _client(headers=_TAURI).get(
        "/coding/projects/manual-zip/publish/events").json()["events"]
    assert any(e["kind"] == "manual_export" and e["state"] == "committed"
               for e in events)
    # A target exists.
    targets = _client(headers=_TAURI).get(
        "/coding/projects/manual-zip/publish/targets").json()["targets"]
    assert any(t["kind"] == "manual_export" for t in targets)


def test_manual_export_patch_and_git_apply(
    tmp_errorta_home: Path, tmp_path: Path,
) -> None:
    delivery = tmp_path / "d2"
    delivery.mkdir()
    _project("manual-patch", delivery_root=delivery)
    c = _client(headers=_TAURI)

    patch_resp = c.post(
        "/coding/projects/manual-patch/publish/manual-export",
        json={"kind": "patch"})
    assert patch_resp.status_code == 200, patch_resp.text
    assert "src/app.py" in patch_resp.json()["patch"]

    ga_resp = c.post(
        "/coding/projects/manual-patch/publish/manual-export",
        json={"kind": "git_apply"})
    assert ga_resp.status_code == 200, ga_resp.text
    ga = ga_resp.json()
    assert ga["command"].startswith("git apply ")
    assert Path(ga["path"]).is_file()


def test_manual_export_open_folder(
    tmp_errorta_home: Path, tmp_path: Path,
) -> None:
    delivery = tmp_path / "d3"
    delivery.mkdir()
    _project("manual-folder", delivery_root=delivery)

    resp = _client(headers=_TAURI).post(
        "/coding/projects/manual-folder/publish/manual-export",
        json={"kind": "open_folder"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    delivered = Path(body["path"])
    assert (delivered / "src" / "app.py").is_file()
    assert body["run_hint"]


def test_manual_export_requires_tauri_origin(
    tmp_errorta_home: Path, tmp_path: Path,
) -> None:
    delivery = tmp_path / "d4"
    delivery.mkdir()
    _project("manual-origin", delivery_root=delivery)

    resp = _client().post(  # no x-errorta-origin header
        "/coding/projects/manual-origin/publish/manual-export",
        json={"kind": "zip"})
    assert resp.status_code == 403


def test_publish_read_routes_require_tauri_origin(
    tmp_errorta_home: Path, tmp_path: Path,
) -> None:
    delivery = tmp_path / "d4b"
    delivery.mkdir()
    _project("manual-read-origin", delivery_root=delivery)
    c = _client()  # no x-errorta-origin header

    assert c.get("/coding/projects/manual-read-origin/publish/events").status_code == 403
    assert c.get("/coding/projects/manual-read-origin/publish/targets").status_code == 403


def test_manual_export_unknown_kind_is_400(
    tmp_errorta_home: Path, tmp_path: Path,
) -> None:
    delivery = tmp_path / "d5"
    delivery.mkdir()
    _project("manual-bad", delivery_root=delivery)

    resp = _client(headers=_TAURI).post(
        "/coding/projects/manual-bad/publish/manual-export",
        json={"kind": "deploy"})
    assert resp.status_code == 400


def test_no_mobile_publish_route_exists() -> None:
    """Publishing is a desktop-only Tauri surface; no /mobile/v1 publish route."""
    from errorta_app.routes import coding as coding_routes

    paths = [getattr(r, "path", "") for r in coding_routes.router.routes]
    assert not any("/mobile/v1" in p for p in paths)
    assert not any("mobile" in p and "publish" in p for p in paths)
