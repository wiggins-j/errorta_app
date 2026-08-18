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


def test_save_tokens_refuses_placeholder_values_and_keeps_the_old_ones() -> None:
    """A setup snippet pasted verbatim must not destroy working tokens.

    This happened for real: the documented one-liner carries `xapp-...` /
    `xoxb-...`, it was run unedited, and save_tokens wrote the 8-character
    placeholders straight over live credentials — the bridge could not
    authenticate and the originals were unrecoverable from disk. (It is the
    second placeholder-clobber in this project; the first overwrote
    studio.json with a literal C_YOUR_STUDIO_CHANNEL_ID.)

    Validation must therefore run BEFORE the file is touched, so a refused
    call leaves the good tokens exactly where they were.
    """
    secrets.save_tokens(APP_TOKEN, BOT_TOKEN)

    for app, bot in (
        ("xapp-...", "xoxb-..."),
        ("<your-app-token>", "<your-bot-token>"),
        ("xapp-", "xoxb-"),
        ("", ""),
    ):
        with pytest.raises(ValueError):
            secrets.save_tokens(app, bot)

    # The originals survived every refusal.
    assert secrets.load_tokens() == {"app_token": APP_TOKEN, "bot_token": BOT_TOKEN}


def test_save_tokens_refuses_a_wrong_prefix() -> None:
    """Swapping the two arguments is the other easy paste error."""
    with pytest.raises(ValueError, match="app_token"):
        secrets.save_tokens(BOT_TOKEN, BOT_TOKEN)
    with pytest.raises(ValueError, match="bot_token"):
        secrets.save_tokens(APP_TOKEN, APP_TOKEN)


def test_save_tokens_error_never_contains_the_value() -> None:
    """The message is user-facing and may be pasted into an issue."""
    secret = "xapp-this-must-never-be-echoed-back"
    with pytest.raises(ValueError) as exc:
        secrets.save_tokens(secret, "nope")
    assert secret not in str(exc.value)


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
