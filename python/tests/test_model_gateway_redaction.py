from __future__ import annotations

from errorta_model_gateway.redaction import redact_text

# Placeholder tokens only — this is a public repo. Never a real token.
BOT_TOKEN = "xoxb-9-cccddd"
APP_TOKEN = "xapp-1-AAA-bbb"


def test_redact_text_masks_slack_bot_token() -> None:
    out = redact_text(f"posting with {BOT_TOKEN} configured")

    assert BOT_TOKEN not in out
    assert "<token-redacted>" in out


def test_redact_text_masks_slack_app_token() -> None:
    out = redact_text(f"socket mode using {APP_TOKEN} configured")

    assert APP_TOKEN not in out
    assert "<token-redacted>" in out


def test_redact_text_still_masks_existing_provider_keys() -> None:
    # Additive change — pre-existing sk-ant redaction must still work.
    out = redact_text("provider key sk-ant-secretsecretsecretsecret in use")

    assert "sk-ant-secretsecretsecretsecret" not in out
    assert "<token-redacted>" in out
