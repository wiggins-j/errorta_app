"""F120-03 — the provider Test wording fix (criterion #5).

A logged-out ``claude_cli`` Test must read ``logged_out`` + a "run the login
command" remediation, never a bare ``claude_cli_failed: exit 1:``. The fix
inspects stdout (``is_error`` / 401), not only stderr, and routes the result
through the SAME member-health classifier the run-loop uses so setup-time and
runtime wording match.
"""
from __future__ import annotations

import json

import pytest

from errorta_model_gateway.providers._cli_common import classify_test_result
from errorta_model_gateway.providers.async_claude_cli import ClaudeCliHandler


class _FakeProc:
    def __init__(self, *, stdout=b"", stderr=b"", returncode=0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self, input=None):
        return self._stdout, self._stderr

    def terminate(self):  # pragma: no cover - not exercised here
        pass

    def kill(self):  # pragma: no cover
        pass

    async def wait(self):  # pragma: no cover
        return self.returncode


def _patch_exec(monkeypatch, proc):
    async def fake_exec(*argv, **kwargs):
        return proc

    import errorta_model_gateway.providers._cli_common as common
    monkeypatch.setattr(common.asyncio, "create_subprocess_exec", fake_exec)


def _handler(monkeypatch) -> ClaudeCliHandler:
    # Pin a resolved binary so resolution never short-circuits to not_installed.
    return ClaudeCliHandler(binary="/usr/bin/claude")


# The verbatim live-incident envelope: exit 0, is_error true, 401 in stdout.
_401_STDOUT = json.dumps({
    "type": "result", "is_error": True,
    "result": ("API Error: 401 "
               '{"type":"error","error":{"type":"authentication_error",'
               '"message":"Please run /login"}}'),
}).encode()


@pytest.mark.asyncio
async def test_logged_out_exit0_is_error_401(monkeypatch):
    """exit 0 + is_error 401 stdout + EMPTY stderr -> logged_out, not a bare exit."""
    _patch_exec(monkeypatch, _FakeProc(stdout=_401_STDOUT, stderr=b"", returncode=0))
    h = _handler(monkeypatch)
    result = await h.test_connection(api_key=None)
    classified = classify_test_result(result)
    assert classified["state"] == "logged_out"
    assert "login" in classified["remediation"].lower()
    assert classified["detail"]  # actionable, not empty
    assert "claude_cli_failed: exit" not in classified["detail"]


@pytest.mark.asyncio
async def test_logged_out_exit1_auth_in_stdout(monkeypatch):
    """exit 1 with the auth message in STDOUT and empty stderr (the bare-exit
    bug) -> still classified logged_out, never `claude_cli_failed: exit 1:`."""
    _patch_exec(
        monkeypatch,
        _FakeProc(stdout=b"Please run /login to authenticate", stderr=b"", returncode=1),
    )
    h = _handler(monkeypatch)
    result = await h.test_connection(api_key=None)
    classified = classify_test_result(result)
    assert classified["state"] == "logged_out"
    assert "login" in classified["remediation"].lower()
    assert "exit 1" not in classified["detail"]


@pytest.mark.asyncio
async def test_connected_when_ok(monkeypatch):
    ok = json.dumps({
        "type": "result", "is_error": False, "result": "ok",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }).encode()
    _patch_exec(monkeypatch, _FakeProc(stdout=ok, returncode=0))
    h = _handler(monkeypatch)
    result = await h.test_connection(api_key=None)
    classified = classify_test_result(result)
    assert classified["state"] == "connected"
    assert classified["remediation"] == ""


@pytest.mark.asyncio
async def test_probe_auth_uses_same_classifier(monkeypatch):
    _patch_exec(monkeypatch, _FakeProc(stdout=_401_STDOUT, returncode=0))
    h = _handler(monkeypatch)
    auth = await h.probe_auth()
    assert auth["state"] == "logged_out"


def test_rate_limited_is_its_own_state_not_error():
    """F132 — a throttled CLI (e.g. cursor_cli_rate_limited) classifies as
    ``rate_limited`` so the UI can show an amber 'connected, try later' state
    instead of a red failure."""

    class _R:
        ok = False
        detail = "cursor_cli_rate_limited: You've hit your usage limit"
        latency_ms = 7

    classified = classify_test_result(_R())
    assert classified["state"] == "rate_limited"
    # A real failure (not a throttle) still classifies as error.

    class _Err:
        ok = False
        detail = "some unexpected crash"
        latency_ms = 3

    assert classify_test_result(_Err())["state"] == "error"
