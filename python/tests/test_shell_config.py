"""Tests for errorta_shell.config — shell-tier persistent settings."""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from threading import Thread

import pytest


@pytest.fixture
def config_module(tmp_errorta_home: Path):
    """Reload errorta_shell.config under an isolated HOME so it picks up
    the tmp data dir and resets module-level state between tests."""
    import errorta_shell.config as config

    importlib.reload(config)
    return config


def test_get_ollama_host_returns_default_when_unset(config_module) -> None:
    assert config_module.get_ollama_host() == "http://127.0.0.1:11434"


def test_set_ollama_host_accepts_http(config_module) -> None:
    result = config_module.set_ollama_host("http://localhost:11434")
    assert result == "http://localhost:11434"
    assert config_module.get_ollama_host() == "http://localhost:11434"


def test_set_ollama_host_accepts_https(config_module) -> None:
    result = config_module.set_ollama_host("https://ollama.example.com")
    assert result == "https://ollama.example.com"


def test_set_ollama_host_rejects_non_http_scheme(config_module) -> None:
    for bad in ("ftp://localhost", "file:///etc/passwd", "ws://x.y"):
        with pytest.raises(ValueError, match="http://"):
            config_module.set_ollama_host(bad)


def test_set_ollama_host_rejects_scheme_less_url(config_module) -> None:
    with pytest.raises(ValueError):
        config_module.set_ollama_host("localhost:11434")


def test_set_ollama_host_rejects_empty(config_module) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        config_module.set_ollama_host("")
    with pytest.raises(ValueError, match="non-empty"):
        config_module.set_ollama_host("   ")


def test_set_ollama_host_rejects_missing_netloc(config_module) -> None:
    with pytest.raises(ValueError, match="host"):
        config_module.set_ollama_host("http://")


def test_set_ollama_host_rejects_too_long(config_module) -> None:
    host = "http://" + ("a" * 300)
    with pytest.raises(ValueError, match="characters"):
        config_module.set_ollama_host(host)


def test_round_trip_persists_across_reload(config_module, tmp_errorta_home: Path) -> None:
    config_module.set_ollama_host("http://10.0.0.5:11434")

    # Reload module — should re-read from disk and surface the prior value.
    reloaded = importlib.reload(config_module)
    assert reloaded.get_ollama_host() == "http://10.0.0.5:11434"

    # And the on-disk file is valid JSON containing the value.
    cfg_path = tmp_errorta_home / ".errorta" / "shell.json"
    assert cfg_path.exists()
    payload = json.loads(cfg_path.read_text())
    assert payload["ollama_host"] == "http://10.0.0.5:11434"


def test_concurrent_writes_do_not_corrupt_config(config_module, tmp_errorta_home: Path) -> None:
    hosts = [f"http://10.0.0.{i}:11434" for i in range(1, 21)]
    errors: list[BaseException] = []

    def worker(h: str) -> None:
        try:
            config_module.set_ollama_host(h)
        except BaseException as exc:  # pragma: no cover - surfaced via assert below
            errors.append(exc)

    threads = [Thread(target=worker, args=(h,)) for h in hosts]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent writes raised: {errors!r}"

    # File must be valid JSON and the final value must be one of the writes.
    cfg_path = tmp_errorta_home / ".errorta" / "shell.json"
    payload = json.loads(cfg_path.read_text())
    assert payload["ollama_host"] in hosts
    assert config_module.get_ollama_host() in hosts


def test_mark_ready_and_cold_start(config_module) -> None:
    assert config_module.cold_start_seconds() is None
    elapsed = config_module.mark_ready()
    assert elapsed >= 0
    # Subsequent calls return the same value (idempotent).
    assert config_module.mark_ready() == elapsed
    cold = config_module.cold_start_seconds()
    assert cold is not None and cold >= 0
