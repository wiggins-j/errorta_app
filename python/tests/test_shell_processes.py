"""Tests for errorta_shell.processes."""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from errorta_shell import processes


@pytest.fixture(autouse=True)
def _clear_registry() -> None:
    """Empty the module-level extra-PID registry between tests."""
    processes._extra_pids.clear()
    yield
    processes._extra_pids.clear()


def test_register_and_unregister_managed_pid() -> None:
    processes.register_managed_pid(42424)
    assert 42424 in processes._extra_pids
    processes.unregister_managed_pid(42424)
    assert 42424 not in processes._extra_pids


def test_register_rejects_zero_and_negative() -> None:
    processes.register_managed_pid(0)
    processes.register_managed_pid(-1)
    assert processes._extra_pids == set()


def test_register_is_idempotent() -> None:
    processes.register_managed_pid(99)
    processes.register_managed_pid(99)
    assert processes._extra_pids == {99}


def test_list_managed_includes_sidecar() -> None:
    infos = processes.list_managed()
    assert any(i.pid == os.getpid() and i.role == "sidecar" for i in infos)


def _install_fake_psutil(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace `processes.psutil` with a fully fake module so callers do
    NOT fall through to the real psutil. Real exception classes are
    preserved so `except` clauses in the SUT still match."""
    import psutil as real_psutil

    fake = MagicMock()
    fake.NoSuchProcess = real_psutil.NoSuchProcess
    fake.AccessDenied = real_psutil.AccessDenied
    fake.Error = real_psutil.Error
    fake.alive_pids = {os.getpid()}
    fake.process_names: dict[int, str] = {}

    def _make_proc(pid: int):
        if pid not in fake.alive_pids:
            raise real_psutil.NoSuchProcess(pid)
        proc = MagicMock()
        proc.pid = pid
        proc.name.return_value = fake.process_names.get(pid, f"proc-{pid}")
        proc.status.return_value = "running"
        proc.cpu_percent.return_value = 1.5
        proc.memory_info.return_value = MagicMock(rss=12345)
        proc.create_time.return_value = 1_700_000_000.0
        proc.oneshot.return_value.__enter__ = MagicMock(return_value=proc)
        proc.oneshot.return_value.__exit__ = MagicMock(return_value=False)
        return proc

    fake.Process.side_effect = _make_proc
    monkeypatch.setattr(processes, "psutil", fake)
    return fake


def test_list_managed_includes_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_psutil(monkeypatch)
    fake.alive_pids = {os.getpid(), 9999}
    fake.process_names = {9999: "ollama"}

    processes.register_managed_pid(9999)
    infos = processes.list_managed()
    ollama_infos = [i for i in infos if i.pid == 9999]
    assert ollama_infos
    assert ollama_infos[0].role == "ollama"


def test_list_managed_skips_missing_process(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_psutil(monkeypatch)
    # 7777 is registered but NOT alive — _inspect should swallow NoSuchProcess.
    fake.alive_pids = {os.getpid()}
    processes.register_managed_pid(7777)

    infos = processes.list_managed()
    assert all(i.pid != 7777 for i in infos)


def test_list_managed_tags_non_ollama_as_child(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_psutil(monkeypatch)
    fake.alive_pids = {os.getpid(), 6666}
    fake.process_names = {6666: "some-helper"}

    processes.register_managed_pid(6666)
    infos = processes.list_managed()
    by_pid = {i.pid: i for i in infos}
    assert by_pid[6666].role == "child"


def test_uptime_seconds_non_negative() -> None:
    assert processes.uptime_seconds() >= 0.0


def test_to_payload_serializes_dict() -> None:
    infos = processes.list_managed()
    payload = processes.to_payload(infos)
    assert isinstance(payload, list)
    for entry in payload:
        assert "pid" in entry
        assert "role" in entry
        assert "rss_bytes" in entry


def test_process_info_to_dict_shape() -> None:
    pi = processes.ProcessInfo(
        pid=1, name="x", role="sidecar", status="running",
        cpu_percent=1.234, rss_bytes=100, started_at=0.0,
    )
    d = pi.to_dict()
    assert d["pid"] == 1
    assert d["cpu_percent"] == 1.23
