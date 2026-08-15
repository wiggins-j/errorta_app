from __future__ import annotations

import stat
from pathlib import Path

import pytest

from errorta_slack import secrets

# Placeholder tokens only — this is a public repo. Never a real Slack token.
APP_TOKEN = "xapp-1-AAA-bbb"
BOT_TOKEN = "xoxb-9-cccddd"


@pytest.fixture(autouse=True)
def _isolated_errorta_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


def test_load_tokens_on_fresh_home_returns_none() -> None:
    assert secrets.load_tokens() is None


def test_save_then_load_round_trips() -> None:
    secrets.save_tokens(APP_TOKEN, BOT_TOKEN)

    loaded = secrets.load_tokens()

    assert loaded == {"app_token": APP_TOKEN, "bot_token": BOT_TOKEN}


def test_token_file_written_with_owner_only_permissions() -> None:
    secrets.save_tokens(APP_TOKEN, BOT_TOKEN)

    token_path = secrets.path()
    assert token_path.name == "tokens.json"
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_mask_reduces_tokens_to_last_four() -> None:
    secrets.save_tokens(APP_TOKEN, BOT_TOKEN)

    masked = secrets.mask()

    assert masked == {"app_token": "…" + APP_TOKEN[-4:], "bot_token": "…" + BOT_TOKEN[-4:]}


def test_mask_on_fresh_home_returns_none_values() -> None:
    masked = secrets.mask()

    assert masked == {"app_token": None, "bot_token": None}


def test_redaction_patterns_mask_bot_token_in_text() -> None:
    text = f"got {BOT_TOKEN} here"

    for pattern in secrets.REDACTION_PATTERNS:
        text = pattern.sub("<slack-token-redacted>", text)

    assert BOT_TOKEN not in text


def test_redaction_patterns_mask_app_token_in_text() -> None:
    text = f"got {APP_TOKEN} here"

    for pattern in secrets.REDACTION_PATTERNS:
        text = pattern.sub("<slack-token-redacted>", text)

    assert APP_TOKEN not in text
