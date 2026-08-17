from __future__ import annotations

import stat
from pathlib import Path

import pytest

from errorta_slack import config


@pytest.fixture(autouse=True)
def _isolated_errorta_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


def test_autopilot_defaults_off() -> None:
    # Slice 5: autopilot lets the PM approve+execute C-class actions itself.
    # It MUST default off -- enabling autonomous spend/publish is the owner's
    # own deliberate config write, never a side effect of installing.
    assert config.load()["autopilot"] is False


def test_autopilot_true_round_trips() -> None:
    config.save({"autopilot": True})
    assert config.load()["autopilot"] is True


def test_autopilot_normalizes_to_bool() -> None:
    # A truthy non-bool coerces to True, a falsy one to False -- never raises.
    config.save({"autopilot": "yes"})
    assert config.load()["autopilot"] is True
    config.save({"autopilot": 0})
    assert config.load()["autopilot"] is False


def test_load_on_fresh_home_returns_defaults() -> None:
    loaded = config.load()

    assert loaded["autopilot"] is False
    assert loaded["enabled"] is False
    assert loaded["bindings"] == []
    assert loaded["window"] == 20
    assert loaded["timeout_minutes"] == 30
    # Carried from Task 7: auth.is_allowed reads these two keys. The default
    # must be an empty list on both, matching auth's fail-closed contract
    # (an empty allowlist denies everyone -- never allow-all).
    assert loaded["allowed_team_ids"] == []
    assert loaded["allowed_user_ids"] == []
    # fix/slack-studio-model: the studio manager (app-level PM) isn't tied
    # to a project, so it needs its own configured model route -- default
    # is the known-good claude_cli.opus.
    assert loaded["studio_model_route"] == "claude_cli.opus"


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


def test_studio_model_route_override_round_trips() -> None:
    config.save({"studio_model_route": "claude_cli.sonnet"})

    loaded = config.load()

    assert loaded["studio_model_route"] == "claude_cli.sonnet"


def test_studio_default_team_default_has_six_role_route_specs() -> None:
    # fix/slack-studio-default-team: the studio spins up projects with an
    # explicit, configurable default team (not a probed-availability
    # projection) so a freshly created project always has a working PM.
    loaded = config.load()

    assert loaded["studio_default_team"] == [
        {"coding_role": "pm", "gateway_route_id": "claude_cli.opus"},
        {"coding_role": "dev", "gateway_route_id": "cursor_cli.composer-2.5"},
        {"coding_role": "dev", "gateway_route_id": "cursor_cli.composer-2.5"},
        {"coding_role": "dev", "gateway_route_id": "cursor_cli.composer-2.5"},
        {"coding_role": "reviewer", "gateway_route_id": "claude_cli.sonnet"},
        {"coding_role": "tester", "gateway_route_id": "claude_cli.sonnet"},
    ]


def test_studio_default_team_override_round_trips() -> None:
    custom = [{"coding_role": "pm", "gateway_route_id": "claude_cli.sonnet"}]

    config.save({"studio_default_team": custom})

    assert config.load()["studio_default_team"] == custom


def test_studio_default_team_falls_back_to_default_when_empty_or_invalid() -> None:
    assert config.normalize({"studio_default_team": []})["studio_default_team"] == \
        config.DEFAULT_CONFIG["studio_default_team"]
    assert config.normalize({"studio_default_team": "nope"})["studio_default_team"] == \
        config.DEFAULT_CONFIG["studio_default_team"]
    # Entries missing either field are dropped; if that empties the list,
    # the whole thing falls back to default rather than shipping a partial
    # or empty team.
    assert config.normalize(
        {"studio_default_team": [{"coding_role": "pm"}]}
    )["studio_default_team"] == config.DEFAULT_CONFIG["studio_default_team"]


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
