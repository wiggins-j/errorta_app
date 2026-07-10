from __future__ import annotations

import json
import stat

import pytest

from errorta_app import settings


def test_load_creates_default_settings_file(tmp_errorta_home) -> None:
    settings_path = settings.path()

    loaded = settings.load()

    assert loaded == {"log_level": "info"}
    assert settings_path.exists()
    assert json.loads(settings_path.read_text(encoding="utf-8")) == loaded
    assert stat.S_IMODE(settings_path.stat().st_mode) == 0o600


def test_save_is_atomic_and_preserves_owner_only_mode(tmp_errorta_home) -> None:
    settings_path = settings.path()

    saved = settings.save({"log_level": "debug"})

    assert saved == {"log_level": "debug"}
    assert json.loads(settings_path.read_text(encoding="utf-8")) == saved
    assert stat.S_IMODE(settings_path.stat().st_mode) == 0o600
    assert not list(settings_path.parent.glob(".settings-*.json"))


def test_load_merges_missing_defaults(tmp_errorta_home) -> None:
    settings.path().write_text("{}", encoding="utf-8")

    assert settings.load() == {"log_level": "info"}


def test_save_rejects_unknown_log_level(tmp_errorta_home) -> None:
    with pytest.raises(ValueError):
        settings.save({"log_level": "trace"})


def test_path_uses_errorta_home_helper(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path / "custom-home"))

    assert settings.path() == tmp_path / "custom-home" / "settings.json"


# ----------------------------------------------------------------------
# F040-01 — cli_binaries override persistence (normalizer must not drop it)
# ----------------------------------------------------------------------


def test_cli_binaries_survives_save_load_round_trip(tmp_errorta_home) -> None:
    settings.save({
        "log_level": "info",
        "cli_binaries": {"claude_cli": "/abs/claude", "codex_cli": "/abs/codex"},
    })
    loaded = settings.load()
    assert loaded["cli_binaries"] == {
        "claude_cli": "/abs/claude",
        "codex_cli": "/abs/codex",
    }


def test_normalizer_drops_unknown_cli_providers_and_blanks(tmp_errorta_home) -> None:
    saved = settings.save({
        "cli_binaries": {
            "claude_cli": "/abs/claude",
            "bogus_cli": "/abs/bogus",
            "codex_cli": "   ",
        },
    })
    assert saved["cli_binaries"] == {"claude_cli": "/abs/claude"}


def test_get_set_clear_cli_binary(tmp_errorta_home) -> None:
    assert settings.get_cli_binary("claude_cli") is None
    settings.set_cli_binary("claude_cli", "/abs/claude")
    assert settings.get_cli_binary("claude_cli") == "/abs/claude"
    # log_level untouched.
    assert settings.load()["log_level"] == "info"
    settings.clear_cli_binary("claude_cli")
    assert settings.get_cli_binary("claude_cli") is None
    # The empty map is dropped entirely on clear.
    assert "cli_binaries" not in settings.load()


def test_set_cli_binary_rejects_unknown_provider(tmp_errorta_home) -> None:
    with pytest.raises(ValueError):
        settings.set_cli_binary("bogus_cli", "/abs/x")


def test_settings_with_no_cli_binaries_omits_key(tmp_errorta_home) -> None:
    assert settings.save({"log_level": "debug"}) == {"log_level": "debug"}


def test_model_family_allowlist_distinguishes_derived_from_explicit_empty(
    tmp_errorta_home,
) -> None:
    assert settings.get_model_family_allowlist() is None
    assert settings.set_model_family_allowlist([]) == []
    assert settings.get_model_family_allowlist() == []
    assert settings.set_model_family_allowlist(["openai", "openai", "local"]) == [
        "local", "openai"]
    assert settings.set_model_family_allowlist(None) is None
