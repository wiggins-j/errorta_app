from __future__ import annotations

import hashlib
import hmac

from errorta_slack import auth

# PUBLIC REPO — placeholder secret/ids only, never a real signing secret.
SIGNING_SECRET = "test_secret"
TEAM_ID = "T123"
USER_ID = "U123"


def _sign(timestamp: str, body: bytes, secret: str = SIGNING_SECRET) -> str:
    base = f"v0:{timestamp}:{body.decode('utf-8')}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return f"v0={digest}"


def _headers(timestamp: str, body: bytes, secret: str = SIGNING_SECRET) -> dict:
    return {
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": _sign(timestamp, body, secret),
    }


# --- verify_signature ---------------------------------------------------


def test_known_good_signature_verifies() -> None:
    now = 1_700_000_000.0
    body = b'{"type":"event_callback"}'
    headers = _headers(str(int(now)), body)

    assert auth.verify_signature(headers, body, SIGNING_SECRET, now=now) is True


def test_tampered_body_fails() -> None:
    now = 1_700_000_000.0
    body = b'{"type":"event_callback"}'
    headers = _headers(str(int(now)), body)

    tampered_body = b'{"type":"event_callback","evil":true}'

    assert auth.verify_signature(headers, tampered_body, SIGNING_SECRET, now=now) is False


def test_tampered_signature_header_fails() -> None:
    now = 1_700_000_000.0
    body = b'{"type":"event_callback"}'
    headers = _headers(str(int(now)), body)
    headers["X-Slack-Signature"] = headers["X-Slack-Signature"][:-1] + (
        "0" if headers["X-Slack-Signature"][-1] != "0" else "1"
    )

    assert auth.verify_signature(headers, body, SIGNING_SECRET, now=now) is False


def test_wrong_signing_secret_fails() -> None:
    now = 1_700_000_000.0
    body = b'{"type":"event_callback"}'
    headers = _headers(str(int(now)), body, secret="a_different_secret")

    assert auth.verify_signature(headers, body, SIGNING_SECRET, now=now) is False


def test_stale_timestamp_fails() -> None:
    now = 1_700_000_000.0
    body = b'{"type":"event_callback"}'
    stale_timestamp = str(int(now) - 301)
    headers = _headers(stale_timestamp, body)

    assert auth.verify_signature(headers, body, SIGNING_SECRET, now=now) is False


def test_timestamp_just_inside_window_verifies() -> None:
    now = 1_700_000_000.0
    body = b'{"type":"event_callback"}'
    fresh_timestamp = str(int(now) - 300)
    headers = _headers(fresh_timestamp, body)

    assert auth.verify_signature(headers, body, SIGNING_SECRET, now=now) is True


def test_future_timestamp_beyond_window_fails() -> None:
    now = 1_700_000_000.0
    body = b'{"type":"event_callback"}'
    future_timestamp = str(int(now) + 301)
    headers = _headers(future_timestamp, body)

    assert auth.verify_signature(headers, body, SIGNING_SECRET, now=now) is False


def test_headers_are_read_case_insensitively() -> None:
    now = 1_700_000_000.0
    body = b'{"type":"event_callback"}'
    headers = {
        "x-slack-request-timestamp": str(int(now)),
        "x-slack-signature": _sign(str(int(now)), body),
    }

    assert auth.verify_signature(headers, body, SIGNING_SECRET, now=now) is True


def test_missing_headers_fail_closed() -> None:
    now = 1_700_000_000.0
    body = b'{"type":"event_callback"}'

    assert auth.verify_signature({}, body, SIGNING_SECRET, now=now) is False


def test_defaults_now_to_current_time_and_verifies_fresh_request() -> None:
    import time

    body = b'{"type":"event_callback"}'
    headers = _headers(str(int(time.time())), body)

    assert auth.verify_signature(headers, body, SIGNING_SECRET) is True


# --- is_allowed -----------------------------------------------------------


def test_is_allowed_true_for_listed_team_and_user() -> None:
    cfg = {"allowed_team_ids": [TEAM_ID], "allowed_user_ids": [USER_ID]}

    assert auth.is_allowed(TEAM_ID, USER_ID, cfg) is True


def test_is_allowed_denies_unlisted_user() -> None:
    cfg = {"allowed_team_ids": [TEAM_ID], "allowed_user_ids": [USER_ID]}

    assert auth.is_allowed(TEAM_ID, "U999", cfg) is False


def test_is_allowed_denies_unlisted_team() -> None:
    cfg = {"allowed_team_ids": [TEAM_ID], "allowed_user_ids": [USER_ID]}

    assert auth.is_allowed("T999", USER_ID, cfg) is False


def test_is_allowed_denies_when_allowlist_is_empty() -> None:
    cfg = {"allowed_team_ids": [], "allowed_user_ids": []}

    assert auth.is_allowed(TEAM_ID, USER_ID, cfg) is False


def test_is_allowed_denies_when_allowlist_keys_are_missing() -> None:
    assert auth.is_allowed(TEAM_ID, USER_ID, {}) is False
