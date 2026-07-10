from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_TAURI = {"x-errorta-origin": "tauri-ui"}


def _client(*, headers: dict[str, str] | None = None) -> TestClient:
    from errorta_app.routes import coding as coding_routes

    app = FastAPI()
    app.include_router(coding_routes.router)
    return TestClient(app, headers=headers)


def _workspace(project_id: str):
    from errorta_council.coding.ledger import LedgerStore
    from errorta_council.coding.workspace import CodingWorkspace

    store = LedgerStore(project_id)
    store.create_project(
        north_star="n",
        definition_of_done="d",
        target="new",
        repo_path=None,
    )
    ws = CodingWorkspace(project_id, store)
    ws.setup(target="new", repo_path=None)
    return store, ws


def _commit_bytes(ws, rel_path: str, body: bytes) -> None:
    from errorta_tools.runner.apply_workspace import _git

    target = ws.root() / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    _git(ws.root(), "add", "-A")
    _git(ws.root(), "commit", "-q", "--allow-empty", "-m", f"bytes:{rel_path}")


def test_safe_rel_pathspec_rejects_traversal_and_git_magic() -> None:
    from errorta_tools.runner.apply_workspace import (
        ApplyWorkspaceError,
        _safe_rel_pathspec,
    )

    for bad in ["../x", "a/../../b", "/etc/passwd", ":!magic", ":(top)x", ""]:
        with pytest.raises(ApplyWorkspaceError, match="apply_bad_pathspec"):
            _safe_rel_pathspec(bad)
    assert _safe_rel_pathspec("src/core.py") == "src/core.py"
    assert _safe_rel_pathspec("a/b/c.py") == "a/b/c.py"
    assert _safe_rel_pathspec("./src/core.py") == "src/core.py"


def test_read_master_file_returns_raw_bytes_and_does_not_checkout(
    tmp_errorta_home: Path,
) -> None:
    from errorta_tools.runner.apply_workspace import _git

    _store, ws = _workspace("file-read-helper")
    body = b"prefix\x00suffix\n"
    _commit_bytes(ws, "src/blob.bin", body)
    _git(ws.root(), "checkout", "-q", "-b", "scratch")
    before_branch = _git(ws.root(), "rev-parse", "--abbrev-ref", "HEAD")
    before_status = _git(ws.root(), "status", "--porcelain")

    got = ws.read_master_file("src/blob.bin")

    assert isinstance(got, bytes)
    assert got == body
    assert ws.read_master_file("src/missing.py") is None
    assert ws.read_master_file(".") is None
    assert _git(ws.root(), "rev-parse", "--abbrev-ref", "HEAD") == before_branch
    assert _git(ws.root(), "status", "--porcelain") == before_status


def test_file_route_returns_utf8_master_content(tmp_errorta_home: Path) -> None:
    _store, ws = _workspace("file-route-ok")
    ws.write_file("src/core.py", "def add(a, b):\n    return a + b\n", task_id="t1")

    resp = _client(headers=_TAURI).get(
        "/coding/projects/file-route-ok/files",
        params={"path": "src/core.py"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["path"] == "src/core.py"
    assert body["content"] == "def add(a, b):\n    return a + b\n"
    assert body["encoding"] == "utf-8"
    assert body["bytes"] == len(body["content"].encode("utf-8"))
    assert body["truncated"] is False
    assert body["on_master"] is True


def test_file_route_rejects_traversal_and_pathspec_magic(
    tmp_errorta_home: Path,
) -> None:
    _store, ws = _workspace("file-route-bad-path")
    ws.write_file("src/core.py", "print('ok')\n", task_id="t1")
    client = _client(headers=_TAURI)

    for bad in ["../../etc/passwd", "/etc/passwd", ":!magic"]:
        resp = client.get(
            "/coding/projects/file-route-bad-path/files",
            params={"path": bad},
        )
        assert resp.status_code == 400, f"{bad} -> {resp.status_code}: {resp.text}"


def test_file_route_returns_not_on_master_for_unmerged_branch(
    tmp_errorta_home: Path,
) -> None:
    _store, ws = _workspace("file-route-unmerged")
    ws.start_task_branch("t1")
    ws.write_file("src/future.py", "print('future')\n", task_id="t1")

    resp = _client(headers=_TAURI).get(
        "/coding/projects/file-route-unmerged/files",
        params={"path": "src/future.py"},
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == {"reason": "not_on_master"}


def test_file_route_truncates_large_text(tmp_errorta_home: Path) -> None:
    _store, ws = _workspace("file-route-large")
    content = "x" * (256 * 1024 + 100)
    ws.write_file("src/large.txt", content, task_id="t1")

    resp = _client(headers=_TAURI).get(
        "/coding/projects/file-route-large/files",
        params={"path": "src/large.txt"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["encoding"] == "utf-8"
    assert body["truncated"] is True
    assert body["bytes"] == len(content)
    assert len(body["content"].encode("utf-8")) <= 256 * 1024


def test_file_route_marks_binary_without_content(tmp_errorta_home: Path) -> None:
    _store, ws = _workspace("file-route-binary")
    _commit_bytes(ws, "src/blob.bin", b"abc\x00def")

    resp = _client(headers=_TAURI).get(
        "/coding/projects/file-route-binary/files",
        params={"path": "src/blob.bin"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["encoding"] == "binary"
    assert body["content"] is None
    assert body["bytes"] == 7


def test_file_route_requires_tauri_origin(tmp_errorta_home: Path) -> None:
    _store, ws = _workspace("file-route-origin")
    ws.write_file("src/core.py", "print('ok')\n", task_id="t1")

    resp = _client().get(
        "/coding/projects/file-route-origin/files",
        params={"path": "src/core.py"},
    )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "origin_not_authorized"


def test_artifacts_include_on_master_without_requiring_worktree(
    tmp_errorta_home: Path,
) -> None:
    from errorta_council.coding.ledger import LedgerStore

    _store, ws = _workspace("file-route-artifacts")
    ws.write_file("src/core.py", "print('merged')\n", task_id="t1", summary="merged")
    ws.start_task_branch("t2")
    ws.write_file("src/future.py", "print('future')\n", task_id="t2", summary="future")

    got = _client(headers=_TAURI).get(
        "/coding/projects/file-route-artifacts/artifacts",
    )

    assert got.status_code == 200, got.text
    by_path = {item["path"]: item for item in got.json()["artifacts"]}
    assert by_path["src/core.py"]["on_master"] is True
    assert by_path["src/future.py"]["on_master"] is False

    no_ws = LedgerStore("file-route-no-worktree")
    no_ws.create_project(
        north_star="n",
        definition_of_done="d",
        target="new",
        repo_path=None,
    )
    no_ws.upsert_artifact(
        path="src/missing.py",
        status="created",
        last_task_id="t1",
        content_sha256="abc",
    )
    got_no_ws = _client(headers=_TAURI).get(
        "/coding/projects/file-route-no-worktree/artifacts",
    )
    assert got_no_ws.status_code == 200, got_no_ws.text
    assert got_no_ws.json()["artifacts"][0]["on_master"] is False


def test_file_route_is_absent_from_mobile_api(tmp_errorta_home: Path) -> None:
    from errorta_app.mobile_server import build_mobile_app

    client = TestClient(build_mobile_app(), headers=_TAURI)

    assert client.get("/coding/projects/file-route-ok/files?path=src/core.py").status_code == 404
    assert (
        client.get(
            "/mobile/v1/coding-projects/file-route-ok/files",
            params={"path": "src/core.py"},
        ).status_code
        == 404
    )


# --- F105 Slice C: GET content_sha256 + PUT file write ----------------------


def test_get_file_includes_content_sha256_for_utf8(tmp_errorta_home: Path) -> None:
    _store, ws = _workspace("sha-utf8")
    content = "def add(a, b):\n    return a + b\n"
    ws.write_file("src/core.py", content, task_id="t1")

    body = _client(headers=_TAURI).get(
        "/coding/projects/sha-utf8/files", params={"path": "src/core.py"},
    ).json()

    assert body["content_sha256"] == hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_get_file_omits_sha_for_binary(tmp_errorta_home: Path) -> None:
    _store, ws = _workspace("sha-binary")
    _commit_bytes(ws, "src/blob.bin", b"abc\x00def")

    body = _client(headers=_TAURI).get(
        "/coding/projects/sha-binary/files", params={"path": "src/blob.bin"},
    ).json()

    assert body["encoding"] == "binary"
    assert "content_sha256" not in body


def test_get_file_sha_is_over_full_blob_not_capped(tmp_errorta_home: Path) -> None:
    _store, ws = _workspace("sha-large")
    content = "x" * (256 * 1024 + 100)
    ws.write_file("src/large.txt", content, task_id="t1")

    body = _client(headers=_TAURI).get(
        "/coding/projects/sha-large/files", params={"path": "src/large.txt"},
    ).json()

    assert body["truncated"] is True
    # SHA is over the full blob, not just the 256 KiB displayed bytes.
    assert body["content_sha256"] == hashlib.sha256(content.encode("utf-8")).hexdigest()


def _get_sha(client, pid: str, path: str) -> str:
    return client.get(
        f"/coding/projects/{pid}/files", params={"path": path}).json()["content_sha256"]


def test_put_file_happy_path_updates_master_and_artifact(tmp_errorta_home: Path) -> None:
    from errorta_council.coding.ledger import LedgerStore

    store, ws = _workspace("put-ok")
    ws.write_file("src/core.py", "old\n", task_id="t1")
    client = _client(headers=_TAURI)
    sha = _get_sha(client, "put-ok", "src/core.py")

    new = "def add(a, b):\n    return a + b\n"
    resp = client.put(
        "/coding/projects/put-ok/files", params={"path": "src/core.py"},
        json={"content": new, "expected_sha256": sha},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["path"] == "src/core.py"
    assert body["on_master"] is True
    assert body["bytes"] == len(new.encode("utf-8"))
    assert body["content_sha256"] == hashlib.sha256(new.encode("utf-8")).hexdigest()
    assert len(body["head"]) >= 7

    # master now holds the new content (re-read confirms).
    again = client.get(
        "/coding/projects/put-ok/files", params={"path": "src/core.py"}).json()
    assert again["content"] == new
    assert again["content_sha256"] == body["content_sha256"]

    # artifact index points at the new hash.
    arts = {a["path"]: a for a in LedgerStore("put-ok").list_artifacts()}
    assert arts["src/core.py"]["content_sha256"] == body["content_sha256"]

    # a human_file_edit decision was recorded with top-level path/head/sha.
    decs = [d for d in LedgerStore("put-ok").list_decisions()
            if d.get("choice") == "human_file_edit"]
    assert len(decs) == 1
    assert decs[0]["path"] == "src/core.py"
    assert decs[0]["content_sha256"] == body["content_sha256"]
    assert decs[0]["head"] == body["head"]


def test_put_file_does_not_switch_working_tree_branch(tmp_errorta_home: Path) -> None:
    from errorta_tools.runner.apply_workspace import _git

    store, ws = _workspace("put-noswitch")
    ws.write_file("src/core.py", "old\n", task_id="t1")
    # Put the shared working tree on a task branch, as a live run would.
    ws.start_task_branch("t2")
    before_branch = _git(ws.root(), "rev-parse", "--abbrev-ref", "HEAD")

    client = _client(headers=_TAURI)
    sha = _get_sha(client, "put-noswitch", "src/core.py")
    resp = client.put(
        "/coding/projects/put-noswitch/files", params={"path": "src/core.py"},
        json={"content": "new\n", "expected_sha256": sha},
    )
    assert resp.status_code == 200, resp.text
    # the checkout is untouched (no branch switch during the master write).
    assert _git(ws.root(), "rev-parse", "--abbrev-ref", "HEAD") == before_branch


def test_put_file_stale_sha_returns_409(tmp_errorta_home: Path) -> None:
    _store, ws = _workspace("put-stale")
    ws.write_file("src/core.py", "current\n", task_id="t1")
    client = _client(headers=_TAURI)

    resp = client.put(
        "/coding/projects/put-stale/files", params={"path": "src/core.py"},
        json={"content": "new\n", "expected_sha256": "0" * 64},
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "stale_file"
    assert detail["content_sha256"] == hashlib.sha256(b"current\n").hexdigest()


def test_put_file_rejects_path_traversal_and_magic(tmp_errorta_home: Path) -> None:
    _store, ws = _workspace("put-badpath")
    ws.write_file("src/core.py", "x\n", task_id="t1")
    client = _client(headers=_TAURI)

    for bad in ["../../etc/passwd", "/etc/passwd", ":!magic"]:
        resp = client.put(
            "/coding/projects/put-badpath/files", params={"path": bad},
            json={"content": "x\n", "expected_sha256": "0" * 64},
        )
        assert resp.status_code == 400, f"{bad} -> {resp.status_code}"


def test_put_file_absent_on_master_404(tmp_errorta_home: Path) -> None:
    _store, ws = _workspace("put-absent")
    ws.write_file("src/core.py", "x\n", task_id="t1")
    client = _client(headers=_TAURI)
    resp = client.put(
        "/coding/projects/put-absent/files", params={"path": "src/missing.py"},
        json={"content": "x\n", "expected_sha256": "0" * 64},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == {"reason": "not_on_master"}


def test_put_file_rejects_binary_current(tmp_errorta_home: Path) -> None:
    _store, ws = _workspace("put-bincur")
    _commit_bytes(ws, "src/blob.bin", b"abc\x00def")
    client = _client(headers=_TAURI)
    resp = client.put(
        "/coding/projects/put-bincur/files", params={"path": "src/blob.bin"},
        json={"content": "text\n", "expected_sha256": "0" * 64},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "binary_file"


def test_put_file_rejects_current_too_large(tmp_errorta_home: Path) -> None:
    _store, ws = _workspace("put-curlarge")
    big = "x" * (256 * 1024 + 10)
    ws.write_file("src/large.txt", big, task_id="t1")
    client = _client(headers=_TAURI)
    resp = client.put(
        "/coding/projects/put-curlarge/files", params={"path": "src/large.txt"},
        json={"content": "small\n", "expected_sha256": "0" * 64},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "file_too_large"


def test_put_file_rejects_new_content_too_large(tmp_errorta_home: Path) -> None:
    _store, ws = _workspace("put-newlarge")
    ws.write_file("src/core.py", "small\n", task_id="t1")
    client = _client(headers=_TAURI)
    sha = _get_sha(client, "put-newlarge", "src/core.py")
    resp = client.put(
        "/coding/projects/put-newlarge/files", params={"path": "src/core.py"},
        json={"content": "x" * (256 * 1024 + 1), "expected_sha256": sha},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "content_too_large"


def test_put_file_rejects_nul_in_new_content(tmp_errorta_home: Path) -> None:
    _store, ws = _workspace("put-nul")
    ws.write_file("src/core.py", "ok\n", task_id="t1")
    client = _client(headers=_TAURI)
    sha = _get_sha(client, "put-nul", "src/core.py")
    resp = client.put(
        "/coding/projects/put-nul/files", params={"path": "src/core.py"},
        json={"content": "bad\x00bytes", "expected_sha256": sha},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason"] == "invalid_content"


def test_put_file_requires_tauri_origin(tmp_errorta_home: Path) -> None:
    _store, ws = _workspace("put-origin")
    ws.write_file("src/core.py", "ok\n", task_id="t1")
    resp = _client().put(
        "/coding/projects/put-origin/files", params={"path": "src/core.py"},
        json={"content": "new\n", "expected_sha256": "0" * 64},
    )
    assert resp.status_code == 403


def test_put_file_rejected_while_run_active(tmp_errorta_home: Path, monkeypatch) -> None:
    from errorta_app.routes import coding as coding_routes

    store, ws = _workspace("put-running")
    ws.write_file("src/core.py", "ok\n", task_id="t1")
    client = _client(headers=_TAURI)
    sha = _get_sha(client, "put-running", "src/core.py")

    # Force the authoritative run-active state the route consults.
    monkeypatch.setattr(coding_routes, "_reconcile_run_state",
                        lambda pid, store: {"status": "running"})
    monkeypatch.setattr(coding_routes, "_thread_alive", lambda pid: True)

    resp = client.put(
        "/coding/projects/put-running/files", params={"path": "src/core.py"},
        json={"content": "new\n", "expected_sha256": sha},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["reason"] == "run_active"


def test_put_file_is_absent_from_mobile_api(tmp_errorta_home: Path) -> None:
    from errorta_app.mobile_server import build_mobile_app

    client = TestClient(build_mobile_app(), headers=_TAURI)
    # No PUT source-file route on the mobile surface (F090 source exclusion).
    assert client.put(
        "/coding/projects/put-ok/files", params={"path": "src/core.py"},
        json={"content": "x\n", "expected_sha256": "0" * 64},
    ).status_code == 404
    assert client.put(
        "/mobile/v1/coding-projects/put-ok/files", params={"path": "src/core.py"},
        json={"content": "x\n", "expected_sha256": "0" * 64},
    ).status_code == 404
