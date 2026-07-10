"""F040-01 S1 — resolver provenance, cheap detect, auth classification.

Hermetic: no real CLI / network / billable model call. ``cli_version`` and
``test_connection`` are monkeypatched everywhere a probe would otherwise spawn.
"""
from __future__ import annotations

import os
import stat

import pytest

from errorta_model_gateway.providers import (
    _cli_common,
    async_claude_cli,
    async_codex_cli,
    async_cursor_cli,
)
from errorta_model_gateway.providers.async_base import (
    TestConnectionResult as ConnResult,
)


def _make_executable(path) -> str:
    path.write_text("#!/bin/sh\necho stub\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


# ---------------------------------------------------------------------------
# resolve_cli_binary_detailed — source provenance + precedence
# ---------------------------------------------------------------------------


def test_detailed_reports_override_settings_source(tmp_path) -> None:
    binary = _make_executable(tmp_path / "mytool")
    out = _cli_common.resolve_cli_binary_detailed(["mytool"], override_path=binary)
    assert out == {"path": binary, "source": "override_settings", "name_used": "mytool"}


def test_detailed_reports_override_env_source(tmp_path, monkeypatch) -> None:
    binary = _make_executable(tmp_path / "envtool")
    monkeypatch.setenv("MY_ENV_VAR", binary)
    out = _cli_common.resolve_cli_binary_detailed(["envtool"], env_var="MY_ENV_VAR")
    assert out["source"] == "override_env"
    assert out["path"] == binary


def test_detailed_reports_path_source(tmp_path, monkeypatch) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    binary = _make_executable(bindir / "pathtool")
    monkeypatch.setenv("PATH", str(bindir))
    out = _cli_common.resolve_cli_binary_detailed(["pathtool"])
    assert out is not None
    assert out["source"] == "path"
    assert out["path"] == binary
    assert out["name_used"] == "pathtool"


def test_detailed_reports_app_bundle_source(tmp_path, monkeypatch) -> None:
    # Empty PATH so the only hit is the extra_paths (app-bundle) fallback.
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(_cli_common.Path, "home", classmethod(lambda cls: tmp_path / "nohome"))
    bundle = _make_executable(tmp_path / "Bundled.app-codex")
    out = _cli_common.resolve_cli_binary_detailed(["codex"], extra_paths=[bundle])
    assert out is not None
    assert out["source"] == "app_bundle"
    assert out["path"] == bundle


def test_detailed_returns_none_when_absent(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(_cli_common.Path, "home", classmethod(lambda cls: tmp_path / "nohome"))
    out = _cli_common.resolve_cli_binary_detailed(["definitely-not-real-tool"])
    assert out is None


def test_detailed_precedence_override_beats_env(tmp_path, monkeypatch) -> None:
    override = _make_executable(tmp_path / "override-tool")
    env_bin = _make_executable(tmp_path / "env-tool")
    monkeypatch.setenv("MY_ENV_VAR", env_bin)
    out = _cli_common.resolve_cli_binary_detailed(
        ["tool"], override_path=override, env_var="MY_ENV_VAR"
    )
    assert out["source"] == "override_settings"
    assert out["path"] == override


def test_detailed_precedence_env_beats_path(tmp_path, monkeypatch) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _make_executable(bindir / "tool")
    env_bin = _make_executable(tmp_path / "env-tool")
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setenv("MY_ENV_VAR", env_bin)
    out = _cli_common.resolve_cli_binary_detailed(["tool"], env_var="MY_ENV_VAR")
    assert out["source"] == "override_env"
    assert out["path"] == env_bin


def test_detailed_ignores_non_executable_override(tmp_path) -> None:
    plain = tmp_path / "not-exec"
    plain.write_text("x", encoding="utf-8")
    plain.chmod(0o644)
    out = _cli_common.resolve_cli_binary_detailed(["tool"], override_path=str(plain))
    # Non-executable override is ignored — falls through to (here empty) resolution.
    assert out is None or out["source"] != "override_settings"


def test_resolve_cli_binary_wrapper_back_compat(tmp_path) -> None:
    binary = _make_executable(tmp_path / "wrap")
    assert _cli_common.resolve_cli_binary(["wrap"], override_path=binary) == binary


# ---------------------------------------------------------------------------
# cli_version — cheap, redacted, never raises
# ---------------------------------------------------------------------------


def test_cli_version_returns_first_line(tmp_path) -> None:
    script = tmp_path / "verstub"
    script.write_text("#!/bin/sh\necho 'tool 1.2.3'\n", encoding="utf-8")
    script.chmod(0o755)
    assert _cli_common.cli_version(str(script)) == "tool 1.2.3"


def test_cli_version_none_when_not_executable(tmp_path) -> None:
    plain = tmp_path / "plain"
    plain.write_text("x", encoding="utf-8")
    plain.chmod(0o644)
    assert _cli_common.cli_version(str(plain)) is None


def test_cli_version_redacts_token(tmp_path) -> None:
    script = tmp_path / "leaky"
    script.write_text(
        "#!/bin/sh\necho 'v1 sk-ant-aaaaaaaaaaaaaaaaaaaa'\n", encoding="utf-8"
    )
    script.chmod(0o755)
    out = _cli_common.cli_version(str(script))
    assert out is not None
    assert "sk-ant-" not in out
    assert "<token-redacted>" in out


# ---------------------------------------------------------------------------
# classify_test_result — connected / logged_out / error + redaction
# ---------------------------------------------------------------------------


def test_classify_connected() -> None:
    out = _cli_common.classify_test_result(
        ConnResult(True, "subscription CLI ready", 12)
    )
    assert out["state"] == "connected"


def test_classify_logged_out() -> None:
    out = _cli_common.classify_test_result(
        ConnResult(False, "claude CLI not logged in", 12)
    )
    assert out["state"] == "logged_out"


def test_classify_error_other() -> None:
    out = _cli_common.classify_test_result(
        ConnResult(False, "claude_cli_failed: exit 2", 12)
    )
    assert out["state"] == "error"


def test_classify_redacts_detail(tmp_path) -> None:
    out = _cli_common.classify_test_result(
        ConnResult(False, "boom sk-ant-aaaaaaaaaaaaaaaaaaaa", 12)
    )
    assert "sk-ant-" not in out["detail"]
    assert out["state"] == "error"


# ---------------------------------------------------------------------------
# Per-provider resolve_details — not_installed vs installed (+ version)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module,handler_cls,resolver_attr",
    [
        (async_claude_cli, async_claude_cli.ClaudeCliHandler, "resolve_claude_binary"),
        (async_codex_cli, async_codex_cli.CodexCliHandler, "resolve_codex_binary"),
    ],
)
def test_resolve_details_not_installed(
    module, handler_cls, resolver_attr, monkeypatch
) -> None:
    monkeypatch.setattr(module, "resolve_cli_binary_detailed", lambda *a, **k: None)
    out = handler_cls().resolve_details()
    assert out["state"] == "not_installed"
    assert out["found"] is False
    assert out["path"] == ""
    assert out["version"] == ""


def test_claude_resolve_details_installed_with_version(monkeypatch) -> None:
    monkeypatch.setattr(
        async_claude_cli,
        "resolve_cli_binary_detailed",
        lambda *a, **k: {"path": "/x/claude", "source": "path", "name_used": "claude"},
    )
    monkeypatch.setattr(async_claude_cli, "cli_version", lambda p: "claude 9.9.9")
    out = async_claude_cli.ClaudeCliHandler().resolve_details()
    assert out["state"] == "installed"
    assert out["found"] is True
    assert out["path"] == "/x/claude"
    assert out["source"] == "path"
    assert out["version"] == "claude 9.9.9"
    # Cheap detect must NOT claim a connection state.
    assert out["login"] == ""


def test_codex_resolve_details_installed_version_absent(monkeypatch) -> None:
    monkeypatch.setattr(
        async_codex_cli,
        "resolve_cli_binary_detailed",
        lambda *a, **k: {"path": "/x/codex", "source": "app_bundle", "name_used": "codex"},
    )
    monkeypatch.setattr(async_codex_cli, "cli_version", lambda p: None)
    out = async_codex_cli.CodexCliHandler().resolve_details()
    assert out["state"] == "installed"
    assert out["version"] == ""  # absence is non-fatal


def test_cursor_resolve_details_uses_override(monkeypatch, tmp_path) -> None:
    binary = _make_executable(tmp_path / "agent")
    monkeypatch.setattr(async_cursor_cli, "cli_version", lambda p: "cursor 1.0")
    out = async_cursor_cli.CursorCliHandler().resolve_details(override_path=binary)
    assert out["state"] == "installed"
    assert out["source"] == "override_settings"
    assert out["path"] == binary


def test_cursor_resolve_details_not_installed(monkeypatch) -> None:
    monkeypatch.setattr(
        async_cursor_cli, "resolve_cursor_command_detailed", lambda **k: None
    )
    out = async_cursor_cli.CursorCliHandler().resolve_details()
    assert out["state"] == "not_installed"
    assert out["found"] is False


# ---------------------------------------------------------------------------
# probe_auth — wraps test_connection, classifies (monkeypatched: no real call)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "handler_cls",
    [
        async_claude_cli.ClaudeCliHandler,
        async_codex_cli.CodexCliHandler,
        async_cursor_cli.CursorCliHandler,
    ],
)
@pytest.mark.parametrize(
    "result,expected_state",
    [
        (ConnResult(True, "subscription CLI ready", 5), "connected"),
        (ConnResult(False, "CLI not logged in", 5), "logged_out"),
        (ConnResult(False, "something else broke", 5), "error"),
    ],
)
@pytest.mark.asyncio
async def test_probe_auth_classifies(handler_cls, result, expected_state) -> None:
    handler = handler_cls()

    async def _fake_test_connection(*, api_key):
        return result

    handler.test_connection = _fake_test_connection  # type: ignore[method-assign]
    out = await handler.probe_auth()
    assert out["state"] == expected_state


# Ensure no env override leaks the test's PATH manipulation into other tests.
@pytest.fixture(autouse=True)
def _clean_overrides(monkeypatch) -> None:
    for var in ("ERRORTA_CLAUDE_CLI", "ERRORTA_CODEX_CLI", "ERRORTA_CURSOR_CLI"):
        monkeypatch.delenv(var, raising=False)
    yield
    os.environ.pop("MY_ENV_VAR", None)
