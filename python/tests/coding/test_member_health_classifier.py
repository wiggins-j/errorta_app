"""F120-01 — member-health classifier locked against the REAL gateway strings.

Every status must come out of an actual exception string the gateway raises
(criterion #1). The marquee case is the live 401 incident: a ``claude_cli`` 401
returns exit 0 with ``is_error: true`` and the handler raises
``FatalError("claude_cli_error: API Error: 401 … Please run /login")`` — that
must classify ``auth_failed``, not a generic failure.
"""
from __future__ import annotations

import pytest

from errorta_council.coding import member_health as mh
from errorta_council.coding.autonomy import CodingAutonomyPolicy
from errorta_council.gateway_local import FatalError, RetryableError

# The verbatim string ClaudeCliHandler.call raises for the live 401 incident
# (async_claude_cli.py ~line 242: FatalError(f"claude_cli_error: {msg[:200]}")).
CLAUDE_401 = (
    "claude_cli_error: API Error: 401 "
    '{"type":"error","error":{"type":"authentication_error",'
    '"message":"Please run /login"}}'
)


@pytest.mark.parametrize(
    "exc, expected",
    [
        # --- the marquee 401 (exit-0, is_error path) ---
        (FatalError(CLAUDE_401), mh.AUTH_FAILED),
        # --- auth, the other real shapes ---
        (FatalError(
            "claude_cli_not_authenticated: run 'claude' and log in with your subscription"
        ), mh.AUTH_FAILED),
        (FatalError(
            "codex_cli_not_authenticated: run 'codex login' with your subscription"
        ), mh.AUTH_FAILED),
        (FatalError(
            "cursor_cli_not_authenticated: run 'agent login'"
        ), mh.AUTH_FAILED),
        (FatalError("anthropic_http_401: unauthorized"), mh.AUTH_FAILED),
        # --- binary_missing ---
        (FatalError(
            "claude_cli_not_installed: the 'claude' CLI is not on PATH or in a known location"
        ), mh.BINARY_MISSING),
        (FatalError("codex_cli_not_found: CLI binary not found"), mh.BINARY_MISSING),
        # --- model_rejected — provider dropped/renamed the requested model.
        # The verbatim Cursor CLI string from the 2026-06 catalog rename
        # (gpt-5 / gpt-5-codex removed in favor of the gpt-5.3-codex family).
        (FatalError(
            "cursor_cli_model_rejected: Cannot use this model: gpt-5. "
            "Available models: auto, gpt-5.3-codex, gpt-5.2"
        ), mh.MODEL_REJECTED),
        # Even un-normalized (raw provider message, no gateway prefix) must classify.
        (FatalError(
            "cursor_cli_failed: exit 1: Cannot use this model: gpt-5-codex. "
            "Available models: auto, gpt-5.3-codex"
        ), mh.MODEL_REJECTED),
        (FatalError("openai_http_400: invalid model 'gpt-9'"), mh.MODEL_REJECTED),
        # --- rate_limited ---
        (RetryableError("claude_cli_rate_limited"), mh.RATE_LIMITED),
        (RetryableError("claude_cli_rate_limited: usage limit reached"), mh.RATE_LIMITED),
        (FatalError("openai_http_429: too many requests"), mh.RATE_LIMITED),
        # --- timeout ---
        (RetryableError("claude_cli_timeout"), mh.TIMEOUT),
        (RetryableError("codex_cli_timeout"), mh.TIMEOUT),
        # --- unparseable ---
        (FatalError("claude_cli_unparseable_output"), mh.UNPARSEABLE),
        (FatalError("claude_cli_empty_result"), mh.UNPARSEABLE),
        (FatalError("claude_cli_empty_model"), mh.UNPARSEABLE),
        # --- errored (the safe default — NEVER ok) ---
        (FatalError("claude_cli_failed: exit 2: segfault"), mh.ERRORED),
        (RuntimeError("something completely unexpected"), mh.ERRORED),
        (ValueError(""), mh.ERRORED),
    ],
)
def test_classify_status(exc, expected):
    result = mh.classify_member_failure(exc)
    assert result.status == expected
    assert result.status in mh.STATUSES
    # An unknown error is never silently treated as a success.
    assert result.status != mh.OK


def test_remediation_keyed_off_status():
    auth = mh.classify_member_failure(FatalError(CLAUDE_401))
    assert "login" in auth.remediation.lower()
    binmiss = mh.classify_member_failure(FatalError("claude_cli_not_installed: x"))
    assert "install" in binmiss.remediation.lower() or "locate" in binmiss.remediation.lower()
    err = mh.classify_member_failure(RuntimeError("boom"))
    assert err.remediation  # non-empty even for the catch-all
    rejected = mh.classify_member_failure(
        FatalError("cursor_cli_model_rejected: Cannot use this model: gpt-5")
    )
    # The fix must point at the room editor / model choice, not provider login.
    assert "model" in rejected.remediation.lower()
    assert "login" not in rejected.remediation.lower()


def test_detail_is_redacted():
    # A token-shaped secret in the error text must NOT survive into the detail.
    leaky = FatalError(
        "claude_cli_failed: exit 1: token sk-ant-abcdefghijklmnopqrstuvwxyz0123456789 leaked"
    )
    out = mh.classify_member_failure(leaky)
    assert "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789" not in out.detail
    assert "<token-redacted>" in out.detail


def test_classify_aware_cap_terminal_vs_transient():
    policy = CodingAutonomyPolicy(member_failure_limit=3)
    # Terminal reasons cap at 1 (stop fast).
    assert mh.classify_aware_cap(mh.AUTH_FAILED, policy) == 1
    assert mh.classify_aware_cap(mh.BINARY_MISSING, policy) == 1
    # A rejected model never heals by retrying — stop after a single attempt.
    assert mh.classify_aware_cap(mh.MODEL_REJECTED, policy) == 1
    # Transient / catch-all reasons cap at member_failure_limit.
    assert mh.classify_aware_cap(mh.TIMEOUT, policy) == 3
    assert mh.classify_aware_cap(mh.RATE_LIMITED, policy) == 3
    assert mh.classify_aware_cap(mh.UNPARSEABLE, policy) == 3
    assert mh.classify_aware_cap(mh.ERRORED, policy) == 3


def test_classify_aware_cap_respects_limit_floor():
    # member_failure_limit is floored at 1 even if mis-set.
    policy = CodingAutonomyPolicy(member_failure_limit=0)
    assert mh.classify_aware_cap(mh.ERRORED, policy) == 1
