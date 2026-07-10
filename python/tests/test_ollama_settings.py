"""Tests for errorta_ollama.settings.

Verifies the on-disk JSON state used by the Ollama integration:
load() defaults, save() atomicity, update() partial merges, and
malformed-JSON resilience.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from errorta_ollama import settings as ollama_settings
from errorta_ollama.settings import (
    DEFAULT_HOST,
    OllamaSettings,
    load,
    save,
    update,
)


@pytest.fixture(autouse=True)
def _clear_state_dir_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure ERRORTA_STATE_DIR doesn't leak from the ambient env."""
    monkeypatch.delenv("ERRORTA_STATE_DIR", raising=False)


def test_load_returns_defaults_when_file_absent(tmp_errorta_home: Path) -> None:
    # No ollama.json exists in the freshly created ~/.errorta.
    assert not (tmp_errorta_home / ".errorta" / "ollama.json").exists()
    s = load()
    assert isinstance(s, OllamaSettings)
    assert s.host == DEFAULT_HOST
    assert s.storage_path is None
    assert s.managed_by_errorta is False
    assert s.installed_version is None
    assert s.last_install_at is None
    assert s.expect_running is False
    assert s.extra == {}


def test_save_persists_json_under_tmp_home(tmp_errorta_home: Path) -> None:
    s = OllamaSettings(
        host="http://127.0.0.1:11434",
        storage_path="/tmp/ollama",
        managed_by_errorta=True,
        installed_version="0.1.42",
        expect_running=True,
    )
    save(s)

    p = tmp_errorta_home / ".errorta" / "ollama.json"
    assert p.exists()
    data = json.loads(p.read_text())
    assert data["host"] == "http://127.0.0.1:11434"
    assert data["storage_path"] == "/tmp/ollama"
    assert data["managed_by_errorta"] is True
    assert data["installed_version"] == "0.1.42"
    assert data["expect_running"] is True


def test_save_is_atomic_no_tmp_left_behind(tmp_errorta_home: Path) -> None:
    save(OllamaSettings(host="http://example:11434"))
    state_dir = tmp_errorta_home / ".errorta"
    leftovers = list(state_dir.glob("*.tmp"))
    assert leftovers == []
    assert (state_dir / "ollama.json").exists()


def test_save_creates_state_dir_if_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "nested" / "state"
    monkeypatch.setenv("ERRORTA_STATE_DIR", str(target))
    assert not target.exists()
    save(OllamaSettings())
    assert (target / "ollama.json").exists()


def test_load_after_save_roundtrips(tmp_errorta_home: Path) -> None:
    original = OllamaSettings(
        host="http://localhost:11500",
        managed_by_errorta=True,
        installed_version="0.2.0",
        last_install_at="2026-06-07T12:00:00Z",
    )
    save(original)
    loaded = load()
    assert loaded.host == original.host
    assert loaded.managed_by_errorta is True
    assert loaded.installed_version == "0.2.0"
    assert loaded.last_install_at == "2026-06-07T12:00:00Z"


def test_update_merges_partial_without_dropping_unrelated_keys(
    tmp_errorta_home: Path,
) -> None:
    save(
        OllamaSettings(
            host="http://localhost:11434",
            storage_path="/data/ollama",
            managed_by_errorta=True,
            installed_version="0.1.0",
        )
    )
    merged = update(installed_version="0.1.1", expect_running=True)
    # Returned settings reflect merge.
    assert merged.installed_version == "0.1.1"
    assert merged.expect_running is True
    # Unrelated fields preserved.
    assert merged.host == "http://localhost:11434"
    assert merged.storage_path == "/data/ollama"
    assert merged.managed_by_errorta is True
    # Persisted to disk too.
    again = load()
    assert again.installed_version == "0.1.1"
    assert again.expect_running is True
    assert again.storage_path == "/data/ollama"


def test_update_ignores_unknown_fields(tmp_errorta_home: Path) -> None:
    merged = update(host="http://localhost:9999", not_a_field="ignored")  # type: ignore[arg-type]
    assert merged.host == "http://localhost:9999"
    assert not hasattr(merged, "not_a_field")
    raw = json.loads((tmp_errorta_home / ".errorta" / "ollama.json").read_text())
    assert "not_a_field" not in raw


def test_load_ignores_malformed_json_and_returns_defaults(
    tmp_errorta_home: Path,
) -> None:
    p = tmp_errorta_home / ".errorta" / "ollama.json"
    p.write_text("{this is not json")
    s = load()
    assert s.host == DEFAULT_HOST
    assert s.managed_by_errorta is False
    assert s.extra == {}


def test_load_preserves_unknown_keys_in_extra(tmp_errorta_home: Path) -> None:
    p = tmp_errorta_home / ".errorta" / "ollama.json"
    p.write_text(
        json.dumps(
            {
                "host": "http://localhost:11434",
                "future_flag": "lookahead",
                "another": 42,
            }
        )
    )
    s = load()
    assert s.host == "http://localhost:11434"
    assert s.extra == {"future_flag": "lookahead", "another": 42}


def test_state_dir_respects_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    override = tmp_path / "custom-state"
    monkeypatch.setenv("ERRORTA_STATE_DIR", str(override))
    save(OllamaSettings(host="http://custom:11434"))
    assert (override / "ollama.json").exists()
    assert ollama_settings._settings_path() == override / "ollama.json"
    loaded = load()
    assert loaded.host == "http://custom:11434"
