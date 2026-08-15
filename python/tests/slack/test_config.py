from __future__ import annotations

import stat
from pathlib import Path

import pytest

from errorta_slack import config


@pytest.fixture(autouse=True)
def _isolated_errorta_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


def test_load_on_fresh_home_returns_defaults() -> None:
    loaded = config.load()

    assert loaded["enabled"] is False
    assert loaded["bindings"] == []
    assert loaded["window"] == 20
    assert loaded["timeout_minutes"] == 30
    # Carried from Task 7: auth.is_allowed reads these two keys. The default
    # must be an empty list on both, matching auth's fail-closed contract
    # (an empty allowlist denies everyone -- never allow-all).
    assert loaded["allowed_team_ids"] == []
    assert loaded["allowed_user_ids"] == []


def test_save_then_load_round_trips() -> None:
    config.save({
        "enabled": True,
        "bindings": [{"channel": "C123", "project": "errorta"}],
        "window": 50,
        "timeout_minutes": 15,
    })

    loaded = config.load()

    assert loaded["enabled"] is True
    assert loaded["bindings"] == [{"channel": "C123", "project": "errorta"}]
    assert loaded["window"] == 50
    assert loaded["timeout_minutes"] == 15


def test_slack_dir_created_with_owner_only_permissions() -> None:
    slack_dir = config.slack_dir()

    assert slack_dir.exists()
    assert slack_dir.is_dir()
    assert stat.S_IMODE(slack_dir.stat().st_mode) == 0o700


def test_config_file_written_with_owner_only_permissions() -> None:
    config.save({"enabled": False, "bindings": [], "window": 20, "timeout_minutes": 30})

    assert stat.S_IMODE(config.slack_dir().joinpath("config.json").stat().st_mode) == 0o600


def test_is_enabled_reflects_saved_config() -> None:
    assert config.is_enabled() is False

    config.save({"enabled": True, "bindings": [], "window": 20, "timeout_minutes": 30})

    assert config.is_enabled() is True


def test_normalize_round_trips_allowlists() -> None:
    normalized = config.normalize({
        "allowed_team_ids": ["T123"], "allowed_user_ids": ["U123", "U456"],
    })

    assert normalized["allowed_team_ids"] == ["T123"]
    assert normalized["allowed_user_ids"] == ["U123", "U456"]


def test_normalize_drops_non_string_allowlist_entries() -> None:
    normalized = config.normalize({
        "allowed_team_ids": ["T123", 42, None], "allowed_user_ids": "not-a-list",
    })

    assert normalized["allowed_team_ids"] == ["T123"]
    assert normalized["allowed_user_ids"] == []


def test_empty_allowlists_keep_auth_fail_closed() -> None:
    from errorta_slack import auth

    cfg = config.load()

    assert cfg["allowed_team_ids"] == []
    assert cfg["allowed_user_ids"] == []
    assert auth.is_allowed("T123", "U123", cfg) is False
