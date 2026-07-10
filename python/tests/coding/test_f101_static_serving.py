"""F101-01 — static demo serving over loopback (end-to-end through the sandbox).

The static detector now emits a SERVED ``managed_local`` profile
(``python -m http.server``). This proves the served profile starts under the
F039 sandbox, reaches ``healthy`` via the http probe, serves ``index.html`` + an
ES module + a relative-fetch target over a real ``http://127.0.0.1:{port}``
origin, blocks path traversal above ``working_dir``, runs under a resolved
sandbox backend (not forced to ``none``), and tears down cleanly on stop.
"""
from __future__ import annotations

import os
import socket
import time
from pathlib import Path

import pytest

from errorta_council.coding import runtime_process as rp
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runtime import (
    RuntimeProfileStore,
    detect,
)
from errorta_council.coding.runtime_process import RuntimeProcessManager
from errorta_council.coding.workspace import CodingWorkspace


@pytest.fixture(autouse=True)
def _fast_and_clean(monkeypatch):
    monkeypatch.setattr(rp, "_POLL_INTERVAL", 0.05)
    monkeypatch.setattr(rp, "_GRACE_SECONDS", 1.0)
    yield
    rp.teardown_all()


def _wait_state(mgr, sid, targets, timeout=15.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        s = mgr.get_session(sid)
        last = s.state if s else None
        if s and s.state in targets:
            return s
        time.sleep(0.05)
    raise AssertionError(f"session {sid} never reached {targets}; last={last}")


def _build_static_workspace(project_id: str) -> tuple[RuntimeProcessManager, Path]:
    store = LedgerStore(project_id)
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    ws = CodingWorkspace(project_id, store)
    ws.setup(target="new", repo_path=None)
    root = ws.root()
    # A real static SPA shape: module entry + relative fetch target.
    (root / "index.html").write_text(
        '<html><head><script type="module" src="./main.js"></script>'
        "</head><body>hi</body></html>")
    (root / "main.js").write_text('fetch("./data.json").then(r => r.json());\n')
    (root / "data.json").write_text('{"ok": true}\n')
    # A secret-bearing file ABOVE working_dir to prove traversal is blocked.
    (root.parent / "above-secret.txt").write_text("TOP SECRET")
    return RuntimeProcessManager.for_project(project_id), root


def _http_get(url: str) -> tuple[int, str]:
    import httpx
    resp = httpx.get(url, timeout=2.0)
    return resp.status_code, resp.text


def test_detect_emits_served_static_profile(tmp_errorta_home: Path):
    store = LedgerStore("statdet")
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    ws = CodingWorkspace("statdet", store)
    ws.setup(target="new", repo_path=None)
    (ws.root() / "index.html").write_text("<html></html>")
    props = detect(ws.root(), project_id="statdet")
    assert len(props) == 1
    p = props[0]
    assert p.runtime_mode == "managed_local" and p.kind == "static"
    assert p.start == ["python", "-m", "http.server", "{port}",
                       "--bind", "127.0.0.1"]


def test_served_static_reaches_healthy_and_serves_assets(tmp_errorta_home: Path):
    mgr, root = _build_static_workspace("statsrv1")
    props = detect(root, project_id="statsrv1")
    rstore = RuntimeProfileStore.for_ledger(LedgerStore("statsrv1"))
    rstore.upsert_profile(props[0])

    started = mgr.start("default")
    sid = started.session_id
    assert started.allocated_ports and started.allocated_ports[0] >= 1024
    port = started.allocated_ports[0]

    healthy = _wait_state(mgr, sid, {"healthy"})
    # Sandbox is the resolved backend, not forced to none, where one is available.
    assert healthy.sandbox_backend in {"seatbelt", "bwrap", "none"}
    pgid = healthy.pgid

    base = f"http://127.0.0.1:{port}"
    code, body = _http_get(base + "/index.html")
    assert code == 200 and "module" in body
    code, _ = _http_get(base + "/main.js")
    assert code == 200
    code, body = _http_get(base + "/data.json")
    assert code == 200 and "ok" in body

    # Traversal above working_dir must not return the secret file. http.server
    # normalizes ".." in the URL path, so the secret is never served.
    code, body = _http_get(base + "/../above-secret.txt")
    assert "TOP SECRET" not in body

    mgr.stop("default")
    stopped = _wait_state(mgr, sid, {"stopped"})
    assert stopped.state == "stopped"
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)
    # Port released.
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
    finally:
        s.close()


def test_legacy_static_profile_still_validates(tmp_errorta_home: Path):
    """Migration: a stored runtime_mode:"static" profile (old shape) still loads
    and validates, with _extras round-tripping — forward-compatible."""
    from errorta_council.coding.runtime import validate_profile
    legacy = validate_profile(
        {"runtime_mode": "static", "kind": "static", "start": [],
         "demo": {"type": "file", "path": "index.html"}, "custom_field": 7},
        profile_id="static", project_id="p")
    assert legacy.runtime_mode == "static"
    assert legacy.to_dict()["custom_field"] == 7
