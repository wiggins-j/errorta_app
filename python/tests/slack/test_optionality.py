"""Task 10 — the real optionality guarantee.

Three properties, each load-bearing on its own:

(a) with the bridge disabled (the default), ``slack_lifecycle.sync()``
    starts nothing and reports why.
(b) with ``slack-sdk`` unimportable (simulating an install that never
    pulled the optional extra), ``sync()`` degrades cleanly instead of
    raising -- even when the operator has otherwise enabled the bridge.
(c) importing ``errorta_app.server`` -- the actual sidecar entrypoint --
    never drags ``errorta_slack`` into ``sys.modules``, in a FRESH
    interpreter. This is deliberately a subprocess check (not an
    in-process ``sys.modules`` assertion): within this same pytest
    process, `errorta_slack.*` is already imported many times over by
    sibling test modules in this folder, which would make an in-process
    assertion trivially and misleadingly pass regardless of whether
    ``server.py`` itself ever imports it. Task 6's per-module
    "does_not_import_slack_sdk" tests had exactly this weakness (see
    progress.md Task 6 carry-note) -- this test is the actual guarantee.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_errorta_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


# --- (a) disabled -----------------------------------------------------------


def test_sync_disabled_starts_nothing() -> None:
    from errorta_slack import config as slack_config

    slack_config.save({"enabled": False})

    from errorta_app import slack_lifecycle

    result = slack_lifecycle.sync()

    assert result == {"running": False, "reason": "disabled"}


# --- (b) slack-sdk unimportable ---------------------------------------------


class _BlockSlackSdk:
    """A ``sys.meta_path`` finder that makes ``import slack_sdk`` fail,
    simulating a sidecar build that never installed the optional extra --
    without needing to actually uninstall the package from this dev venv."""

    def find_spec(self, name, path=None, target=None):
        if name == "slack_sdk" or name.startswith("slack_sdk."):
            raise ImportError(f"blocked for test: {name}")
        return None


def test_sync_reports_sdk_missing_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    from errorta_slack import config as slack_config

    slack_config.save({"enabled": True})

    for name in list(sys.modules):
        if name == "slack_sdk" or name.startswith("slack_sdk."):
            monkeypatch.delitem(sys.modules, name, raising=False)

    from errorta_app import slack_lifecycle

    blocker = _BlockSlackSdk()
    sys.meta_path.insert(0, blocker)
    try:
        result = slack_lifecycle.sync()
    finally:
        sys.meta_path.remove(blocker)
        slack_lifecycle.stop()

    assert result == {"running": False, "reason": "sdk_missing"}


def test_sync_reports_no_tokens_when_sdk_present_but_untokened() -> None:
    from errorta_slack import config as slack_config

    slack_config.save({"enabled": True})

    from errorta_app import slack_lifecycle

    result = slack_lifecycle.sync()

    assert result == {"running": False, "reason": "no_tokens"}
    slack_lifecycle.stop()


# --- (c) server boot never imports errorta_slack, in a fresh interpreter ---


def test_server_import_does_not_pull_in_slack_bridge(tmp_path: Path) -> None:
    snippet = (
        "import sys\n"
        "import errorta_app.server\n"
        "assert 'errorta_slack' not in sys.modules, "
        "'errorta_app.server import pulled in errorta_slack at module load'\n"
        "print('OK')\n"
    )
    env = {**__import__("os").environ, "ERRORTA_HOME": str(tmp_path)}
    proc = subprocess.run(
        [sys.executable, "-c", snippet],
        cwd=str(Path(__file__).resolve().parents[2]),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"subprocess failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert proc.stdout.strip() == "OK"
