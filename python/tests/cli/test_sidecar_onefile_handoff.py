"""The detached sidecar must never inherit PyInstaller's onefile hand-off env.

Root cause of the long-unexplained CLI-spawned-sidecar ``500`` on ``errorta new``
(the one SPEC-22 was written to make diagnosable, and which its log did in fact
crack). PyInstaller 6 sets ``_PYI_PARENT_PROCESS_LEVEL`` /
``_PYI_APPLICATION_HOME_DIR`` when a frozen process spawns another copy of
itself; the child then REUSES the parent's ``_MEI…`` extraction instead of
unpacking its own. Correct for a child the parent waits on — wrong for ours,
which is ``start_new_session=True`` and outlives the CLI. The parent exits, its
cleanup removes the shared directory, and the sidecar serves on from a temp dir
that no longer exists.

The failure is invisible until it isn't: everything already in ``sys.modules``
keeps working (``status`` / ``projects`` / ``delete`` are all fine), but the first
module imported AFTERWARDS can never load. ``routes/coding.py::_project_out``
lazy-imports ``errorta_project_grounding`` → ``sqlite3`` → ``_sqlite3`` in a
function body, so ``errorta new`` raised ``ModuleNotFoundError: No module named
'_sqlite3'`` from a binary that demonstrably bundles it.

Why this test and not an integration one: the bug only reproduces when the
sidecar was spawned by an EARLIER command (dir already reaped). Spawn and create
in one invocation and it passes every time — which is exactly what a person
debugging it would try, and why it survived three sessions. The env is the
invariant that is actually cheap to lock.
"""
from __future__ import annotations

from pathlib import Path

from errorta_cli import sidecar


class _FakeProc:
    def __init__(self, pid: int = 4321) -> None:
        self.pid = pid
        self.returncode = None

    def poll(self):
        return None


def _spawn_capturing_env(monkeypatch, tmp_path: Path, launched: dict):
    def fake_launch(argv, env, **_kw):
        launched["env"] = env
        launched["port"] = int(env["ERRORTA_SIDECAR_PORT"])
        return _FakeProc()

    monkeypatch.setattr(sidecar, "_launch", fake_launch)
    monkeypatch.setattr(
        sidecar, "probe_healthz",
        lambda port, **k: ({"build": {"commit": "abc"}}
                           if port == launched.get("port") else None))
    monkeypatch.setattr(sidecar, "_scan_errorta_processes", lambda **k: [])
    return sidecar.resolve(tmp_path, our_commit="abc")


def test_spawn_strips_pyinstaller_onefile_handoff_vars(
    monkeypatch, tmp_path: Path,
) -> None:
    """A frozen parent's hand-off vars must not reach the detached sidecar."""
    # Simulate running inside a PyInstaller onefile parent.
    monkeypatch.setenv("_PYI_PARENT_PROCESS_LEVEL", "1")
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", "/var/folders/x/T/_MEIdoomed")
    monkeypatch.setenv("_PYI_ARCHIVE_FILE", "/opt/homebrew/bin/errorta")
    monkeypatch.setenv("_MEIPASS2", "/var/folders/x/T/_MEIlegacy")
    # A normal variable must still be inherited.
    monkeypatch.setenv("ERRORTA_CANARY", "keep-me")

    launched: dict = {}
    _spawn_capturing_env(monkeypatch, tmp_path, launched)
    env = launched["env"]

    for var in sidecar._PYI_ONEFILE_HANDOFF_VARS:
        assert var not in env, (
            f"{var} leaked into the sidecar env — the detached sidecar will "
            f"reuse the parent's _MEI dir and lose it when the CLI exits, "
            f"breaking every later lazy import (e.g. `errorta new`)")

    # The strip must be surgical, not a wholesale env reset.
    assert env["ERRORTA_CANARY"] == "keep-me"
    assert env["ERRORTA_HOME"] == str(tmp_path)
    assert env["ERRORTA_SIDECAR_PORT"]


def test_handoff_var_list_covers_pyinstaller_6_and_legacy() -> None:
    """Lock the names. PyInstaller renamed these at 6.0; a rename that lands
    unnoticed silently restores the bug, so assert both spellings are covered."""
    assert "_PYI_PARENT_PROCESS_LEVEL" in sidecar._PYI_ONEFILE_HANDOFF_VARS
    assert "_PYI_APPLICATION_HOME_DIR" in sidecar._PYI_ONEFILE_HANDOFF_VARS
    assert "_MEIPASS2" in sidecar._PYI_ONEFILE_HANDOFF_VARS  # PyInstaller < 6


def test_spawn_env_is_unchanged_when_not_frozen(monkeypatch, tmp_path: Path) -> None:
    """The strip is a no-op for a dev (non-frozen) parent: nothing sets the
    hand-off vars, so today's dev trace is byte-identical."""
    for var in sidecar._PYI_ONEFILE_HANDOFF_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ERRORTA_CANARY", "keep-me")

    launched: dict = {}
    _spawn_capturing_env(monkeypatch, tmp_path, launched)

    assert launched["env"]["ERRORTA_CANARY"] == "keep-me"
    assert not any(v in launched["env"]
                   for v in sidecar._PYI_ONEFILE_HANDOFF_VARS)
