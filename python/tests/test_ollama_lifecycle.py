"""Tests for errorta_ollama.lifecycle."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from errorta_ollama import lifecycle, settings as settings_module
from errorta_ollama.detect import DetectionResult


def _write_settings(
    tmp_errorta_home: Path,
    *,
    managed_by_errorta: bool = True,
    expect_running: bool = True,
    storage_path: str | None = None,
) -> None:
    """Persist a settings.json so lifecycle.load() picks up the test values."""
    s = settings_module.OllamaSettings(
        managed_by_errorta=managed_by_errorta,
        expect_running=expect_running,
        storage_path=storage_path,
    )
    # settings module reads HOME via Path.home(); tmp_errorta_home fixture
    # already set HOME to tmp_path, so save() lands at tmp_path/.errorta/ollama.json.
    settings_module.save(s)


def test_restart_noop_when_not_managed(
    tmp_errorta_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_subprocess_popen: MagicMock,
) -> None:
    _write_settings(tmp_errorta_home, managed_by_errorta=False)

    probe_mock = MagicMock(return_value=DetectionResult(reachable=False, host="x"))
    monkeypatch.setattr(lifecycle.detect, "probe", probe_mock)

    result = lifecycle.restart_if_managed_and_down()

    assert result.attempted is False
    assert result.succeeded is False
    assert "not managed" in result.reason.lower()
    mock_subprocess_popen.assert_not_called()
    probe_mock.assert_not_called()


def test_restart_noop_when_expect_running_false(
    tmp_errorta_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_subprocess_popen: MagicMock,
) -> None:
    _write_settings(tmp_errorta_home, managed_by_errorta=True, expect_running=False)

    monkeypatch.setattr(
        lifecycle.detect, "probe",
        MagicMock(return_value=DetectionResult(reachable=False, host="x")),
    )

    result = lifecycle.restart_if_managed_and_down()

    assert result.attempted is False
    assert result.succeeded is False
    mock_subprocess_popen.assert_not_called()


def test_restart_noop_when_already_reachable(
    tmp_errorta_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_subprocess_popen: MagicMock,
) -> None:
    _write_settings(tmp_errorta_home)

    monkeypatch.setattr(
        lifecycle.detect, "probe",
        MagicMock(return_value=DetectionResult(reachable=True, host="x")),
    )

    result = lifecycle.restart_if_managed_and_down()

    assert result.attempted is False
    assert result.succeeded is True
    assert "already" in result.reason.lower()
    mock_subprocess_popen.assert_not_called()


def test_restart_spawns_when_managed_and_down(
    tmp_errorta_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_subprocess_popen: MagicMock,
) -> None:
    _write_settings(tmp_errorta_home)

    monkeypatch.setattr(
        lifecycle.detect, "probe",
        MagicMock(return_value=DetectionResult(reachable=False, host="x")),
    )
    monkeypatch.setattr(lifecycle.detect, "wait_until_ready", MagicMock(return_value=True))
    # Force a deterministic platform branch.
    monkeypatch.setattr(lifecycle.sys, "platform", "darwin")

    result = lifecycle.restart_if_managed_and_down()

    assert result.attempted is True
    assert result.succeeded is True
    mock_subprocess_popen.assert_called_once()
    args, _kwargs = mock_subprocess_popen.call_args
    assert args[0] == ["open", "-a", "Ollama"]


def test_restart_started_but_not_ready_returns_failure(
    tmp_errorta_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_subprocess_popen: MagicMock,
) -> None:
    _write_settings(tmp_errorta_home)

    monkeypatch.setattr(
        lifecycle.detect, "probe",
        MagicMock(return_value=DetectionResult(reachable=False, host="x")),
    )
    monkeypatch.setattr(lifecycle.detect, "wait_until_ready", MagicMock(return_value=False))
    monkeypatch.setattr(lifecycle.sys, "platform", "darwin")

    result = lifecycle.restart_if_managed_and_down()

    assert result.attempted is True
    assert result.succeeded is False
    assert "did not respond" in result.reason.lower()


def test_restart_surfaces_spawn_error_as_failure(
    tmp_errorta_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_settings(tmp_errorta_home)

    monkeypatch.setattr(
        lifecycle.detect, "probe",
        MagicMock(return_value=DetectionResult(reachable=False, host="x")),
    )
    wait_mock = MagicMock(return_value=True)
    monkeypatch.setattr(lifecycle.detect, "wait_until_ready", wait_mock)
    monkeypatch.setattr(lifecycle.sys, "platform", "darwin")

    def boom(*_a, **_kw):
        raise OSError("no binary")

    monkeypatch.setattr(lifecycle.subprocess, "Popen", boom)

    result = lifecycle.restart_if_managed_and_down()

    assert result.attempted is True
    assert result.succeeded is False
    assert "failed to spawn" in result.reason.lower()
    wait_mock.assert_not_called()


def test_platform_start_darwin_uses_open(
    monkeypatch: pytest.MonkeyPatch, mock_subprocess_popen: MagicMock
) -> None:
    monkeypatch.setattr(lifecycle.sys, "platform", "darwin")
    assert lifecycle._platform_start(storage_path="/tmp/models") is True
    mock_subprocess_popen.assert_called_once()
    args, kwargs = mock_subprocess_popen.call_args
    assert args[0] == ["open", "-a", "Ollama"]
    assert kwargs["env"]["OLLAMA_MODELS"] == "/tmp/models"


def test_platform_start_linux_falls_back_to_ollama_serve(
    monkeypatch: pytest.MonkeyPatch, mock_subprocess_popen: MagicMock
) -> None:
    monkeypatch.setattr(lifecycle.sys, "platform", "linux")

    def systemctl_fails(*_a, **_kw):
        raise FileNotFoundError("no systemctl")

    monkeypatch.setattr(lifecycle.subprocess, "run", systemctl_fails)

    assert lifecycle._platform_start() is True
    mock_subprocess_popen.assert_called_once()
    args, _kwargs = mock_subprocess_popen.call_args
    assert args[0] == ["ollama", "serve"]


def test_platform_start_linux_prefers_systemctl(
    monkeypatch: pytest.MonkeyPatch, mock_subprocess_popen: MagicMock
) -> None:
    monkeypatch.setattr(lifecycle.sys, "platform", "linux")

    run_mock = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(lifecycle.subprocess, "run", run_mock)

    assert lifecycle._platform_start() is True
    run_mock.assert_called_once()
    assert run_mock.call_args[0][0][:3] == ["systemctl", "--user", "start"]
    # No fall-through to Popen when systemctl succeeded.
    mock_subprocess_popen.assert_not_called()


def test_platform_start_windows_uses_cmd_start(
    monkeypatch: pytest.MonkeyPatch, mock_subprocess_popen: MagicMock
) -> None:
    monkeypatch.setattr(lifecycle.sys, "platform", "win32")
    # Stub the Windows exe resolution so the real shutil.which (which touches
    # _winapi, absent on the macOS test host) isn't invoked.
    monkeypatch.setattr(lifecycle.shutil, "which", lambda name, **k: "ollama.exe")
    assert lifecycle._platform_start() is True
    mock_subprocess_popen.assert_called_once()
    args, _kwargs = mock_subprocess_popen.call_args
    assert args[0][0] == "cmd"
    assert "ollama.exe" in args[0]


def test_platform_start_unknown_platform_returns_false(
    monkeypatch: pytest.MonkeyPatch, mock_subprocess_popen: MagicMock
) -> None:
    monkeypatch.setattr(lifecycle.sys, "platform", "sunos5")
    assert lifecycle._platform_start() is False
    mock_subprocess_popen.assert_not_called()
