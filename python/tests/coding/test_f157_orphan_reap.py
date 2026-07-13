"""F157 — persisted-session orphan reaping.

`_LIVE` only tracks processes THIS sidecar spawned, and the graceful-shutdown
`teardown_all` never runs on a crash/SIGKILL — so a managed-local dev server can
outlive its sidecar with no reaper. The pgid is persisted per session, so we reap
by pgid from the store. These tests spawn REAL detached process groups (a plain
`python -c "sleep"`, own session via start_new_session) and assert the reaper
kills ONLY groups it can positively identify as ours (cwd inside the project's
workspace) — the PID-reuse guard is the safety-critical case.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from errorta_council.coding import runtime_process as rp
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runtime import (
    RuntimeProfileStore,
    RuntimeSession,
)
from errorta_council.coding.workspace import CodingWorkspace

_SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


@pytest.fixture
def _spawned():
    """Track spawned detached groups and hard-kill any survivor after the test so
    a test process is never leaked even if an assertion fails mid-way."""
    procs: list[subprocess.Popen] = []

    def spawn(cwd: Path) -> tuple[subprocess.Popen, int]:
        proc = subprocess.Popen(
            _SLEEPER, cwd=str(cwd),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,  # own session+group => pgid == proc.pid
        )
        procs.append(proc)
        # Give the child a beat to chdir so psutil.cwd() is settled.
        time.sleep(0.2)
        return proc, os.getpgid(proc.pid)

    yield spawn

    for proc in procs:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            pass


def _project(pid: str) -> tuple[RuntimeProfileStore, Path]:
    """A project with a real, created apply-workspace; returns (rstore, root)."""
    store = LedgerStore(pid)
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    ws = CodingWorkspace(pid, store)
    ws.setup(target="new", repo_path=None)
    rstore = RuntimeProfileStore.for_ledger(store)
    root = rp._project_workspace_root(pid)
    assert root is not None and root.exists(), root
    return rstore, root


def _record_session(rstore: RuntimeProfileStore, *, pgid: int | None,
                    state: str = "running", sid: str | None = None) -> str:
    sid = sid or rstore.new_session_id()
    rstore.append_session(RuntimeSession(
        session_id=sid, profile_id="default", state=state, pgid=pgid,
        started_at="t0"))
    return sid


# --- low-level primitives -----------------------------------------------------

def test_pgid_alive_and_kill(_spawned) -> None:
    proc, pgid = _spawned(Path.cwd())
    assert rp._pgid_alive(pgid) is True
    assert rp._kill_pgid(pgid, grace=1.0) is True
    assert rp._pgid_alive(pgid) is False


def test_pgid_alive_false_for_dead(_spawned) -> None:
    proc, pgid = _spawned(Path.cwd())
    os.killpg(pgid, 9)
    proc.wait(timeout=2)
    time.sleep(0.1)
    assert rp._pgid_alive(pgid) is False


# --- ownership guard (the safety-critical gate) -------------------------------

def test_ownership_guard_confirms_cwd_inside_workspace(tmp_errorta_home: Path,
                                                       _spawned) -> None:
    _rstore, root = _project("f157-own-in")
    _proc, pgid = _spawned(root)
    assert rp._pgid_is_ours(pgid, workspace_root=root) is True


def test_ownership_guard_spares_foreign_pgid(tmp_errorta_home: Path,
                                             _spawned, tmp_path: Path) -> None:
    # A live process whose cwd is OUTSIDE the workspace must NOT be confirmed ours
    # (the PID-reuse case: after a reboot a stored pgid can belong to a stranger).
    _rstore, root = _project("f157-own-out")
    foreign_cwd = tmp_path / "not-the-workspace"
    foreign_cwd.mkdir()
    _proc, pgid = _spawned(foreign_cwd)
    assert rp._pgid_is_ours(pgid, workspace_root=root) is False


def test_ownership_guard_false_for_dead_pgid(tmp_errorta_home: Path,
                                             _spawned) -> None:
    _rstore, root = _project("f157-own-dead")
    proc, pgid = _spawned(root)
    os.killpg(pgid, 9)
    proc.wait(timeout=2)
    time.sleep(0.1)
    assert rp._pgid_is_ours(pgid, workspace_root=root) is False


# --- reap_persisted_sessions --------------------------------------------------

def test_reap_kills_confirmed_orphan(tmp_errorta_home: Path, _spawned) -> None:
    rstore, root = _project("f157-reap-kill")
    proc, pgid = _spawned(root)
    sid = _record_session(rstore, pgid=pgid, state="running")

    killed = rp.reap_persisted_sessions(rstore, project_id="f157-reap-kill",
                                        grace=1.0)
    assert killed == 1
    assert rp._pgid_alive(pgid) is False
    sess = rstore.get_session(sid)
    assert sess.state == "stopped" and sess.error == "reaped_orphan"


def test_reap_spares_foreign_and_records_orphan_gone(
        tmp_errorta_home: Path, _spawned, tmp_path: Path) -> None:
    # SAFETY: a foreign live process (cwd outside the workspace) is NOT killed;
    # the phantom session is still cleared so the store stops advertising it.
    rstore, root = _project("f157-reap-foreign")
    foreign_cwd = tmp_path / "elsewhere"
    foreign_cwd.mkdir()
    proc, pgid = _spawned(foreign_cwd)
    sid = _record_session(rstore, pgid=pgid, state="running")

    killed = rp.reap_persisted_sessions(rstore, project_id="f157-reap-foreign")
    assert killed == 0
    assert rp._pgid_alive(pgid) is True, "a foreign process must never be killed"
    sess = rstore.get_session(sid)
    assert sess.state == "stopped" and sess.error == "orphan_gone"


def test_reap_skips_terminal_and_null_pgid(tmp_errorta_home: Path,
                                            _spawned) -> None:
    rstore, root = _project("f157-reap-skip")
    # A live child recorded as already-terminal must be left untouched.
    proc_term, pgid_term = _spawned(root)
    _record_session(rstore, pgid=pgid_term, state="stopped", sid="s-term")
    # A session with no pgid is skipped.
    _record_session(rstore, pgid=None, state="running", sid="s-null")

    killed = rp.reap_persisted_sessions(rstore, project_id="f157-reap-skip")
    assert killed == 0
    assert rp._pgid_alive(pgid_term) is True  # terminal session -> not reaped


def test_reap_records_orphan_gone_for_dead_pgid(tmp_errorta_home: Path,
                                                _spawned) -> None:
    rstore, root = _project("f157-reap-dead")
    proc, pgid = _spawned(root)
    os.killpg(pgid, 9)
    proc.wait(timeout=2)
    time.sleep(0.1)
    sid = _record_session(rstore, pgid=pgid, state="running")

    killed = rp.reap_persisted_sessions(rstore, project_id="f157-reap-dead")
    assert killed == 0
    assert rstore.get_session(sid).error == "orphan_gone"


# --- reap_all_persisted_orphans (boot sweep) ----------------------------------

def test_reap_all_iterates_projects(tmp_errorta_home: Path, _spawned) -> None:
    r1, root1 = _project("f157-all-a")
    r2, root2 = _project("f157-all-b")
    _p1, pg1 = _spawned(root1)
    _p2, pg2 = _spawned(root2)
    _record_session(r1, pgid=pg1, state="running")
    _record_session(r2, pgid=pg2, state="running")

    total = rp.reap_all_persisted_orphans()
    assert total >= 2
    assert rp._pgid_alive(pg1) is False
    assert rp._pgid_alive(pg2) is False


# --- resilient_rmtree (G2: delete tolerates a briefly-open tree) --------------

def test_resilient_rmtree_removes_populated_tree(tmp_path: Path) -> None:
    from errorta_tools.runner.apply_workspace import resilient_rmtree
    root = tmp_path / "tree"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "f.txt").write_text("x")
    resilient_rmtree(root)
    assert not root.exists()


def test_resilient_rmtree_handles_readonly_entry(tmp_path: Path) -> None:
    # A read-only file/dir (chmod 0) must not defeat the removal — the onerror
    # chmod-and-retry path handles it.
    from errorta_tools.runner.apply_workspace import resilient_rmtree
    root = tmp_path / "ro"
    root.mkdir()
    f = root / "locked.txt"
    f.write_text("x")
    os.chmod(f, 0)
    resilient_rmtree(root)
    assert not root.exists()


def test_resilient_rmtree_noop_on_missing(tmp_path: Path) -> None:
    from errorta_tools.runner.apply_workspace import resilient_rmtree
    resilient_rmtree(tmp_path / "does-not-exist")  # must not raise


# --- boot-reap integration (the sidecar startup actually invokes the sweep) ---

def test_sidecar_boot_reaps_prior_orphan(tmp_errorta_home: Path, _spawned) -> None:
    # Simulate a crash: a project has a running server persisted with a live pgid,
    # but _LIVE is empty (this is a fresh process). Booting the sidecar (entering
    # the lifespan) must reap it — the escape hatch teardown_all can't cover.
    from fastapi.testclient import TestClient

    rstore, root = _project("f157-boot")
    proc, pgid = _spawned(root)
    sid = _record_session(rstore, pgid=pgid, state="running")
    assert not rp._LIVE, "precondition: no in-memory tracking (crashed prior process)"

    from errorta_app.server import app
    with TestClient(app, headers={"x-errorta-origin": "tauri-ui"}):
        pass  # entering/exiting the context runs startup (then shutdown)

    assert rp._pgid_alive(pgid) is False, "boot reap must kill the orphan"
    assert rstore.get_session(sid).error == "reaped_orphan"
