"""Spec 22 — diagnosable failures, sidecar side.

* Item 1 — the redacting, byte-capped ``logs/sidecar.log`` handler. Secrets never
  reach the log; the sink caps its own growth; the CLI-spawned case detaches the
  console handlers so nothing is written twice (and never unredacted).
* Item 2 — an unhandled route exception mints ``e-<12 hex>``, logs the traceback
  under it, and returns it in the body. Deliberate ``HTTPException``s are
  untouched.
* Item 3 — a failed ``POST /coding/projects`` leaves no project directory, and
  never removes a pre-existing one.
* Item 4 — ``DELETE`` removes every project-id-keyed location, sweeps a directory
  that has lost its ``project.json``, and refuses under a live run.
"""
from __future__ import annotations

import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from errorta_app import sidecar_log
from errorta_app.server import app

_ID_RE = re.compile(r"^e-[0-9a-f]{12}$")


def _client(_home: Path) -> TestClient:
    return TestClient(app, headers={"x-errorta-origin": "tauri-ui"})


def _new_project(client: TestClient, project_id: str, **extra):
    body = {"project_id": project_id, "north_star": "n",
            "definition_of_done": "d", "target": "new", **extra}
    return client.post("/coding/projects", json=body)


# --------------------------------------------------------------------------- #
# Item 1 — the redacting, capped file sink.
# --------------------------------------------------------------------------- #

def _isolated_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    return logger


def test_secrets_never_reach_the_log(tmp_path: Path) -> None:
    """The acceptance test, not an assumption.

    ``ERRORTA_LOG_FILE`` writes UNREDACTED by its own admission, which is why it
    cannot be turned on by default. This sink runs the same pipeline
    ``/diagnostics/log-tail`` applies, so it can.
    """
    token = "sk-ant-" + "A1b2C3d4E5f6G7h8I9j0K1l2"
    path = tmp_path / "sidecar.log"
    logger = _isolated_logger("errorta.test.spec22.redact")
    handler, detached = sidecar_log.install([logger], path)
    try:
        logger.warning("provider handshake failed with key %s", token)
    finally:
        sidecar_log.uninstall([logger], handler, detached)

    text = path.read_text("utf-8")
    assert token not in text
    assert "<token-redacted>" in text


def test_the_sink_caps_its_own_growth(tmp_path: Path) -> None:
    """A truncating in-process rotation is rejected (the CLI's fd owns the inode),
    so the honest option is: cross the budget, warn once, stop writing."""
    path = tmp_path / "sidecar.log"
    logger = _isolated_logger("errorta.test.spec22.cap")
    handler, detached = sidecar_log.install([logger], path, max_bytes=400)
    try:
        for i in range(200):
            logger.info("filler line %03d %s", i, "y" * 40)
    finally:
        sidecar_log.uninstall([logger], handler, detached)

    assert handler.capped is True
    assert path.stat().st_size < 400 + len(sidecar_log._CAPPED_NOTICE) + 200
    assert "budget reached" in path.read_text("utf-8")


def test_cli_spawn_detaches_console_handlers_so_nothing_is_written_twice(
    tmp_path: Path,
) -> None:
    """In the CLI case stdout/stderr ARE this file. A surviving console handler
    would write every line twice — and the stderr copy UNREDACTED."""
    logger = _isolated_logger("errorta.test.spec22.detach")
    console = logging.StreamHandler(sys.stderr)
    logger.addHandler(console)
    other = logging.FileHandler(tmp_path / "unrelated.log", encoding="utf-8")
    logger.addHandler(other)

    handler, detached = sidecar_log.install(
        [logger], tmp_path / "sidecar.log", detach_console=True)
    try:
        assert console not in logger.handlers          # detached
        assert other in logger.handlers                # a FileHandler is not console
        assert [h for _, h in detached] == [console]
    finally:
        sidecar_log.uninstall([logger], handler, detached)
        logger.removeHandler(other)
        other.close()

    assert console in logger.handlers                  # restored on shutdown


def test_the_sink_stays_attached_when_console_detach_is_off(tmp_path: Path) -> None:
    """An adopted (desktop-spawned) sidecar keeps its stderr — its fds belong to
    whoever spawned it and cannot be re-pointed."""
    logger = _isolated_logger("errorta.test.spec22.adopted")
    console = logging.StreamHandler(sys.stderr)
    logger.addHandler(console)
    handler, detached = sidecar_log.install([logger], tmp_path / "sidecar.log")
    try:
        assert detached == []
        assert console in logger.handlers
    finally:
        sidecar_log.uninstall([logger], handler, detached)
        logger.removeHandler(console)


def test_logs_dir_honours_the_env_var_the_desktop_app_uses(
    monkeypatch, tmp_path: Path
) -> None:
    """``shell_cmds_impl.rs::logs_folder`` honours ERRORTA_LOGS_DIR; if Python
    resolved differently the app's "open logs folder" button would point
    somewhere nothing writes (which is exactly what it did)."""
    from errorta_app import paths

    monkeypatch.setenv("ERRORTA_LOGS_DIR", str(tmp_path / "elsewhere"))
    assert paths.sidecar_log_path() == tmp_path / "elsewhere" / "sidecar.log"
    monkeypatch.delenv("ERRORTA_LOGS_DIR")
    assert paths.sidecar_log_path().name == "sidecar.log"
    assert paths.sidecar_log_path().parent.is_dir()


# --------------------------------------------------------------------------- #
# Item 2 — a correlation id on every unhandled route exception.
# --------------------------------------------------------------------------- #

@pytest.fixture
def boom_route():
    """Mount a route that raises, then take it back off the shared app."""
    path = "/__spec22_boom__"

    def _boom() -> dict:
        return {"x": 1 / 0}

    app.add_api_route(path, _boom, methods=["GET"])
    app.openapi_schema = None
    try:
        yield path
    finally:
        app.router.routes = [
            r for r in app.router.routes if getattr(r, "path", None) != path
        ]
        app.openapi_schema = None


def test_unhandled_exception_returns_a_correlation_id(boom_route, caplog) -> None:
    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR, logger="errorta.error"):
        resp = client.get(boom_route)

    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert detail["code"] == "internal_error"
    err_id = detail["error_id"]
    assert _ID_RE.match(err_id), err_id
    assert err_id in detail["hint"] and "sidecar.log" in detail["hint"]

    # The id resolves to a line in the log, immediately above the traceback —
    # `grep <id>` must be a complete debugging first step, or the id is a
    # diagnostic liability rather than a diagnostic (Item 5).
    captured = "\n".join(r.getMessage() for r in caplog.records)
    assert err_id in captured
    assert any("Traceback" in (r.exc_text or "") for r in caplog.records
               if r.exc_info or r.exc_text) or any(r.exc_info for r in caplog.records)


def test_each_failure_gets_its_own_id(boom_route) -> None:
    client = TestClient(app, raise_server_exceptions=False)
    first = client.get(boom_route).json()["detail"]["error_id"]
    second = client.get(boom_route).json()["detail"]["error_id"]
    assert first != second


def test_deliberate_http_exceptions_are_unaffected(tmp_errorta_home: Path) -> None:
    """The no-false-positives lock: hundreds of call sites in routes/coding.py
    raise HTTPException on purpose; Starlette dispatches those to its own handler
    and they never reach ours."""
    c = _client(tmp_errorta_home)
    resp = c.get("/coding/projects/nope")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "project not found"}


# --------------------------------------------------------------------------- #
# Item 3 — a failed create leaves nothing behind.
# --------------------------------------------------------------------------- #

def _ledger_dir(project_id: str) -> Path:
    from errorta_council.coding.ledger import LedgerStore

    return LedgerStore(project_id).dir


def test_failed_create_leaves_no_project_directory(
    monkeypatch, tmp_errorta_home: Path
) -> None:
    from errorta_app.routes import coding as coding_routes

    def _boom(*_a, **_k):
        raise RuntimeError("bootstrap exploded")

    monkeypatch.setattr(coding_routes, "_apply_grounding_payload", _boom)
    c = TestClient(app, headers={"x-errorta-origin": "tauri-ui"},
                   raise_server_exceptions=False)
    resp = _new_project(c, "resid", grounding={"mode": "build_from_repo"})

    assert resp.status_code >= 400
    assert not _ledger_dir("resid").exists()


def test_a_retry_after_a_failed_create_behaves_like_a_first_attempt(
    monkeypatch, tmp_errorta_home: Path
) -> None:
    from errorta_app.routes import coding as coding_routes

    calls = {"n": 0}
    real = coding_routes._apply_grounding_payload

    def _flaky(store, payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("bootstrap exploded")
        return real(store, payload)

    monkeypatch.setattr(coding_routes, "_apply_grounding_payload", _flaky)
    c = TestClient(app, headers={"x-errorta-origin": "tauri-ui"},
                   raise_server_exceptions=False)
    assert _new_project(c, "retry", grounding={"mode": "none"}).status_code >= 400
    assert not _ledger_dir("retry").exists()

    assert _new_project(c, "retry", grounding={"mode": "none"}).status_code == 200
    assert (_ledger_dir("retry") / "project.json").is_file()


def test_a_duplicate_create_never_overwrites_a_pre_existing_project(
    tmp_errorta_home: Path,
) -> None:
    c = TestClient(app, headers={"x-errorta-origin": "tauri-ui"},
                   raise_server_exceptions=False)
    assert _new_project(c, "winner").status_code == 200
    marker = _ledger_dir("winner") / "project.json"
    original = marker.read_bytes()
    assert marker.is_file()

    duplicate = _new_project(c, "winner", grounding={"mode": "none"},
                             north_star="replacement")
    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": "project already exists"}

    assert _ledger_dir("winner").is_dir()
    assert marker.read_bytes() == original
    assert _client(tmp_errorta_home).get("/coding/projects/winner").status_code == 200


def test_concurrent_create_loser_cannot_delete_the_winner(
    monkeypatch, tmp_errorta_home: Path,
) -> None:
    """The directory claim, not timing luck, owns compensating cleanup."""
    from errorta_app.routes import coding as coding_routes

    entered_grounding = Event()
    release_grounding = Event()
    real_apply = coding_routes._apply_grounding_payload

    def _paused_apply(store, payload):
        entered_grounding.set()
        assert release_grounding.wait(timeout=10)
        return real_apply(store, payload)

    monkeypatch.setattr(coding_routes, "_apply_grounding_payload", _paused_apply)

    def _winner_request():
        client = TestClient(app, headers={"x-errorta-origin": "tauri-ui"},
                            raise_server_exceptions=False)
        return _new_project(client, "raced", grounding={"mode": "none"})

    with ThreadPoolExecutor(max_workers=1) as pool:
        winner = pool.submit(_winner_request)
        assert entered_grounding.wait(timeout=10)
        try:
            loser = _new_project(
                TestClient(app, headers={"x-errorta-origin": "tauri-ui"},
                           raise_server_exceptions=False),
                "raced",
                grounding={"mode": "none"},
            )
            assert loser.status_code == 409
        finally:
            release_grounding.set()
        assert winner.result(timeout=10).status_code == 200

    marker = _ledger_dir("raced") / "project.json"
    assert marker.is_file()
    assert _client(tmp_errorta_home).get("/coding/projects/raced").status_code == 200


# --------------------------------------------------------------------------- #
# Item 4 — delete removes every piece of per-project state.
# --------------------------------------------------------------------------- #

def test_delete_removes_the_ledger_and_the_apply_workspace(
    tmp_errorta_home: Path,
) -> None:
    from errorta_app.paths import errorta_home

    c = _client(tmp_errorta_home)
    assert _new_project(c, "gone").status_code == 200
    ws_root = errorta_home() / "council" / "apply-workspaces"
    ws_root.mkdir(parents=True, exist_ok=True)
    (ws_root / "coding-gone.source.json").write_text("{}", encoding="utf-8")

    assert c.delete("/coding/projects/gone").status_code == 200

    assert not _ledger_dir("gone").exists()
    assert not list(ws_root.glob("coding-gone*"))


def test_a_directory_with_no_project_json_is_swept_not_404ed(
    tmp_errorta_home: Path,
) -> None:
    """The stranded-directory lock — the observed `pocketboard2` shape.

    Both GET and DELETE used to 404 on a missing project.json, so a tree that
    lost it part-way through a failed rmtree was unreachable through the API
    forever. Sweeping it is the only change that makes it reachable.
    """
    stranded = _ledger_dir("stranded")
    stranded.mkdir(parents=True, exist_ok=True)
    (stranded / "run_config.json").write_text("{}", encoding="utf-8")
    (stranded / "run_state.json").write_text("{}", encoding="utf-8")

    c = _client(tmp_errorta_home)
    assert c.get("/coding/projects/stranded").status_code == 404   # unchanged
    resp = c.delete("/coding/projects/stranded")

    assert resp.status_code == 200
    assert resp.json()["deleted"] is True and resp.json()["swept"] is True
    assert not stranded.exists()


def test_a_second_delete_reports_not_found_cleanly(tmp_errorta_home: Path) -> None:
    c = _client(tmp_errorta_home)
    assert _new_project(c, "twice").status_code == 200
    assert c.delete("/coding/projects/twice").status_code == 200
    again = c.delete("/coding/projects/twice")
    assert again.status_code == 404
    assert again.json() == {"detail": "project not found"}


def test_the_residue_sweep_never_runs_under_a_live_run(
    monkeypatch, tmp_errorta_home: Path
) -> None:
    """The liveness check must run BEFORE the sweep, or a sweep could delete
    under a running run."""
    from errorta_app.routes import coding as coding_routes

    stranded = _ledger_dir("livewire")
    stranded.mkdir(parents=True, exist_ok=True)
    (stranded / "run_config.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(coding_routes, "_thread_alive", lambda _pid: True)
    c = _client(tmp_errorta_home)
    resp = c.delete("/coding/projects/livewire")

    assert resp.status_code == 409
    assert stranded.is_dir()


def test_delete_drops_project_json_before_the_tree(
    monkeypatch, tmp_errorta_home: Path
) -> None:
    """A partially-failed rmtree must leave a *sweepable* directory, not a
    stranded one — so the manifest goes first."""

    c = _client(tmp_errorta_home)
    assert _new_project(c, "partial").status_code == 200
    project_dir = _ledger_dir("partial")

    from errorta_tools.runner import apply_workspace

    seen: dict[str, bool] = {}
    state = {"explode": True}
    real_rmtree = apply_workspace.resilient_rmtree

    def _maybe_exploding(path, **kw):
        if state["explode"] and Path(path) == project_dir:
            seen["project_json_present"] = (project_dir / "project.json").exists()
            raise OSError("device busy")
        return real_rmtree(path, **kw)

    monkeypatch.setattr(apply_workspace, "resilient_rmtree", _maybe_exploding)
    failing = TestClient(app, headers={"x-errorta-origin": "tauri-ui"},
                         raise_server_exceptions=False)
    assert failing.delete("/coding/projects/partial").status_code == 500

    assert seen["project_json_present"] is False
    # …and the residue is now reachable: an ordinary DELETE sweeps it rather than
    # 404-ing forever (which is what stranded `pocketboard2` / `punprod`).
    state["explode"] = False
    assert c.delete("/coding/projects/partial").status_code == 200
    assert not project_dir.exists()
