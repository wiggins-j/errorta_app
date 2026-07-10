"""cursor_cli handler tests (mocked subprocess, deterministic)."""
from __future__ import annotations

import asyncio
import json

import pytest

from errorta_council.gateway_local import FatalError, RetryableError
from errorta_model_gateway.providers.async_base import AsyncProviderRequest
from errorta_model_gateway.providers.async_cursor_cli import (
    CursorCliHandler,
    CursorCommand,
)


class _FakeProc:
    def __init__(self, *, stdout=b"", stderr=b"", returncode=0, hang=False):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._hang = hang
        self.terminated = False
        self.killed = False
        self.stdin_input: bytes | None = None

    async def communicate(self, input=None):
        self.stdin_input = input
        if self._hang:
            await asyncio.sleep(60)
        return self._stdout, self._stderr

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


def _patch(monkeypatch, proc=None, *, raises=None):
    captured = {}

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs
        if raises is not None:
            raise raises
        return proc

    import errorta_model_gateway.providers._cli_common as common

    monkeypatch.setattr(common.asyncio, "create_subprocess_exec", fake_exec)
    return captured


def _ok_json(text="Cursor answer"):
    return json.dumps({
        "type": "result",
        "subtype": "success",
        "duration_ms": 1234,
        "is_error": False,
        "result": text,
    }).encode("utf-8")


def _synthetic_cursor_result_with_usage(text="Cursor answer"):
    """SYNTHETIC cursor result payload carrying a `usage` block.

    We could NOT capture a real `cursor-agent --output-format json` payload in
    this environment (no cursor auth), so this fixture is invented: it assumes
    the cursor CLI, IF it emits usage, mirrors the claude/codex `usage` shape
    (`input_tokens`/`output_tokens`/`cache_read_input_tokens`/
    `cache_creation_input_tokens`). Replace with a real captured payload once
    cursor auth is available. If the real schema differs, the handler silently
    no-ops (see test_no_usage_block_falls_back_to_unreported).
    """
    return json.dumps({
        "type": "result",
        "subtype": "success",
        "duration_ms": 1234,
        "is_error": False,
        "result": text,
        "usage": {
            "input_tokens": 4096,
            "output_tokens": 512,
            "cache_read_input_tokens": 8192,
            "cache_creation_input_tokens": 1024,
        },
    }).encode("utf-8")


def _err_json(message="Authentication required. Please run 'agent login' first."):
    return json.dumps({
        "type": "result",
        "subtype": "error",
        "is_error": True,
        "result": message,
    }).encode("utf-8")


def _req(model="default", prompt="Say hello"):
    return AsyncProviderRequest(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_output_tokens=64,
        timeout_seconds=5,
    )


def _handler(argv_prefix=None):
    return CursorCliHandler(
        command=CursorCommand(argv_prefix or ["/fake/agent"], "/fake/agent")
    )


@pytest.mark.asyncio
async def test_parses_result_object(monkeypatch):
    _patch(monkeypatch, _FakeProc(stdout=_ok_json("Hello from Cursor.")))
    r = await _handler().call(_req(), api_key=None)
    assert r.content == "Hello from Cursor."
    assert r.provider_class == "cursor_cli"
    assert r.model == "default"
    assert r.raw_usage_available is False


@pytest.mark.asyncio
async def test_synthetic_usage_block_lands_measured(monkeypatch):
    # SYNTHETIC fixture, schema UNVERIFIED against a live cursor CLI (see
    # _synthetic_cursor_result_with_usage). Locks the parse so IF cursor emits a
    # claude/codex-shaped `usage` block, measured tokens + cache land on the
    # result and raw_usage_available flips True.
    _patch(monkeypatch, _FakeProc(stdout=_synthetic_cursor_result_with_usage("Hi.")))
    r = await _handler().call(_req(), api_key=None)
    assert r.content == "Hi."
    assert r.input_tokens == 4096
    assert r.output_tokens == 512
    assert r.cache_read_input_tokens == 8192
    assert r.cache_write_input_tokens == 1024
    assert r.raw_usage_available is True


@pytest.mark.asyncio
async def test_no_usage_block_falls_back_to_unreported(monkeypatch):
    # No `usage` field (the current real-schema assumption): byte-identical to
    # the prior behavior — None tokens, raw_usage_available False, no cache. A
    # wrong schema guess therefore causes zero harm (cursor stays estimated).
    _patch(monkeypatch, _FakeProc(stdout=_ok_json("Plain answer.")))
    r = await _handler().call(_req(), api_key=None)
    assert r.content == "Plain answer."
    assert r.input_tokens is None
    assert r.output_tokens is None
    assert r.cache_read_input_tokens is None
    assert r.cache_write_input_tokens is None
    assert r.raw_usage_available is False


@pytest.mark.asyncio
async def test_constrained_argv_and_prompt_on_stdin(monkeypatch):
    proc = _FakeProc(stdout=_ok_json())
    cap = _patch(monkeypatch, proc)
    await _handler().call(_req(prompt="SECRET-CURSOR-PROMPT"), api_key=None)
    argv = cap["argv"]
    assert argv[:1] == ["/fake/agent"]
    assert "-p" in argv
    assert "--mode" in argv and argv[argv.index("--mode") + 1] == "ask"
    assert "--trust" in argv
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
    assert "--model" not in argv
    assert not any("SECRET-CURSOR-PROMPT" in a for a in argv)
    assert proc.stdin_input is not None and b"SECRET-CURSOR-PROMPT" in proc.stdin_input
    assert cap["kwargs"].get("start_new_session") is True


@pytest.mark.asyncio
async def test_explicit_model_passes_model_flag(monkeypatch):
    cap = _patch(monkeypatch, _FakeProc(stdout=_ok_json()))
    await _handler().call(_req(model="gpt-5"), api_key=None)
    argv = cap["argv"]
    assert "--model" in argv and argv[argv.index("--model") + 1] == "gpt-5"


@pytest.mark.asyncio
async def test_cursor_launcher_prefix_supported(monkeypatch):
    cap = _patch(monkeypatch, _FakeProc(stdout=_ok_json()))
    await _handler(["/fake/cursor", "agent"]).call(_req(), api_key=None)
    assert cap["argv"][:2] == ["/fake/cursor", "agent"]


@pytest.mark.asyncio
async def test_not_installed_when_command_unresolved(monkeypatch):
    import errorta_model_gateway.providers.async_cursor_cli as mod

    monkeypatch.setattr(mod, "resolve_cursor_command", lambda: None)
    with pytest.raises(FatalError) as e:
        await CursorCliHandler().call(_req(), api_key=None)
    assert "not_installed" in str(e.value)


@pytest.mark.asyncio
async def test_auth_error_is_fatal(monkeypatch):
    _patch(monkeypatch, _FakeProc(stdout=_err_json(), returncode=1))
    with pytest.raises(FatalError) as e:
        await _handler().call(_req(), api_key=None)
    assert "not_authenticated" in str(e.value)


@pytest.mark.asyncio
async def test_rate_limit_error_is_retryable(monkeypatch):
    _patch(monkeypatch, _FakeProc(stdout=_err_json("429 usage limit reached"), returncode=1))
    with pytest.raises(RetryableError) as e:
        await _handler().call(_req(), api_key=None)
    assert "rate_limited" in str(e.value)


@pytest.mark.asyncio
async def test_model_error_is_fatal(monkeypatch):
    _patch(monkeypatch, _FakeProc(stdout=_err_json("invalid model: nope"), returncode=1))
    with pytest.raises(FatalError) as e:
        await _handler().call(_req(model="nope"), api_key=None)
    assert "model_rejected" in str(e.value)


@pytest.mark.asyncio
async def test_renamed_model_is_model_rejected(monkeypatch):
    # The verbatim 2026-06 Cursor catalog-rename failure: `gpt-5` was dropped
    # for the `gpt-5.3-codex` family. This must classify as a model rejection
    # (FatalError, terminal) so the run stops fast with a "pick a valid model"
    # remediation — not a generic provider error that suggests re-login.
    _patch(monkeypatch, _FakeProc(stdout=_err_json(
        "Cannot use this model: gpt-5. Available models: auto, gpt-5.3-codex"
    ), returncode=1))
    with pytest.raises(FatalError) as e:
        await _handler().call(_req(model="gpt-5"), api_key=None)
    assert "model_rejected" in str(e.value)


@pytest.mark.asyncio
async def test_timeout_is_retryable(monkeypatch):
    proc = _FakeProc(hang=True)
    _patch(monkeypatch, proc)
    with pytest.raises(RetryableError) as e:
        await _handler().call(_req(), api_key=None)
    assert "timeout" in str(e.value)
    assert proc.terminated is True


@pytest.mark.asyncio
async def test_empty_result_is_fatal(monkeypatch):
    _patch(monkeypatch, _FakeProc(stdout=json.dumps({"result": ""}).encode()))
    with pytest.raises(FatalError) as e:
        await _handler().call(_req(), api_key=None)
    assert "empty_result" in str(e.value)


@pytest.mark.asyncio
async def test_unparseable_output_is_fatal(monkeypatch):
    _patch(monkeypatch, _FakeProc(stdout=b"not-json"))
    with pytest.raises(FatalError) as e:
        await _handler().call(_req(), api_key=None)
    assert "unparseable" in str(e.value)


def test_validate_route():
    h = _handler()
    assert h.validate_route("cursor_cli.default").ok is True
    assert h.validate_route("cursor_cli.gpt-5").ok is True
    assert h.validate_route("cursor_cli.").ok is False
    assert h.validate_route("codex_cli.default").ok is False


def test_resolves_cursor_agent_from_common_path(monkeypatch, tmp_path):
    import errorta_model_gateway.providers.async_cursor_cli as mod

    monkeypatch.delenv("ERRORTA_CURSOR_CLI", raising=False)
    monkeypatch.setattr(mod, "_cursor_app_cli", lambda: str(tmp_path / "missing-cursor"))
    monkeypatch.setattr(mod.Path, "home", classmethod(lambda cls: tmp_path))

    fake = tmp_path / ".local" / "bin" / "agent"
    fake.parent.mkdir(parents=True)
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setattr(
        mod,
        "resolve_cli_binary_detailed",
        lambda names, override_path=None, env_var=None, extra_paths=None: {
            "path": str(fake), "source": "common_dir", "name_used": "agent",
        },
    )
    cmd = mod.resolve_cursor_command()
    assert cmd is not None
    assert cmd.argv_prefix == [str(fake)]


def test_env_override_cursor_launcher_uses_agent_subcommand(monkeypatch, tmp_path):
    import errorta_model_gateway.providers.async_cursor_cli as mod

    fake = tmp_path / "cursor"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("ERRORTA_CURSOR_CLI", str(fake))
    cmd = mod.resolve_cursor_command()
    assert cmd is not None
    assert cmd.argv_prefix == [str(fake), "agent"]


# --- live model discovery ---------------------------------------------------

# Verbatim shape of `cursor-agent models` (header + `<id> - <Label>` lines +
# trailing tip). The parser must ignore everything but the model lines.
_MODELS_STDOUT = """Available models

auto - Auto
gpt-5.3-codex - Codex 5.3
gpt-5.3-codex-high - Codex 5.3 High
claude-4.5-opus-high - Opus 4.5
gemini-3.1-pro - Gemini 3.1 Pro
grok-4.3 - Grok 4.3 1M

Tip: use --model <id> (or /model <id> in interactive mode) to switch.
"""


def test_parse_cursor_models_ignores_header_and_tip():
    import errorta_model_gateway.providers.async_cursor_cli as mod

    models = mod.parse_cursor_models(_MODELS_STDOUT)
    ids = [m[0] for m in models]
    assert ids == [
        "auto", "gpt-5.3-codex", "gpt-5.3-codex-high",
        "claude-4.5-opus-high", "gemini-3.1-pro", "grok-4.3",
    ]
    # The "Tip:" line and the "Available models" header are not models.
    assert "Tip" not in ids and "Available" not in ids
    assert dict(models)["gpt-5.3-codex"] == "Codex 5.3"


def test_routes_from_models_default_first_and_families():
    import errorta_model_gateway.providers.async_cursor_cli as mod

    routes = mod.routes_from_models(mod.parse_cursor_models(_MODELS_STDOUT))
    assert routes[0].route_id == "cursor_cli.default"  # account default, always first
    by_id = {r.route_id: r for r in routes}
    # `auto` folds into default (no duplicate route).
    assert "cursor_cli.auto" not in by_id
    assert by_id["cursor_cli.gpt-5.3-codex"].family == "gpt"
    assert by_id["cursor_cli.claude-4.5-opus-high"].family == "claude"
    assert by_id["cursor_cli.gemini-3.1-pro"].family == "gemini"
    assert by_id["cursor_cli.grok-4.3"].family == "grok"


def _stub_discovery(monkeypatch, *, stdout, calls):
    """Point discovery at a fake CLI + captured `cursor-agent models` stdout."""
    import errorta_model_gateway.providers.async_cursor_cli as mod

    mod.reset_models_cache()
    monkeypatch.setattr(
        mod, "resolve_cursor_command_detailed",
        lambda override_path=None: (CursorCommand(["/fake/cursor-agent"], "/fake/cursor-agent"), "path"),
    )

    def fake_capture(argv, *, timeout=8.0):
        calls.append(argv)
        return stdout

    monkeypatch.setattr(mod, "run_cli_capture", fake_capture)
    return mod


def test_discover_uses_live_models_and_caches(monkeypatch):
    calls: list = []
    mod = _stub_discovery(monkeypatch, stdout=_MODELS_STDOUT, calls=calls)

    routes = mod.discover_cursor_routes(_now=1000.0)
    ids = {r.route_id for r in routes}
    assert "cursor_cli.gpt-5.3-codex" in ids
    assert "cursor_cli.gpt-5" not in ids  # the removed id never appears
    assert calls[-1] == ["/fake/cursor-agent", "models"]
    # A second call within the TTL is served from cache (no re-shell).
    mod.discover_cursor_routes(_now=1001.0)
    assert len(calls) == 1
    # Past the TTL, it re-discovers.
    mod.discover_cursor_routes(_now=1000.0 + mod._MODELS_CACHE_TTL_SECONDS + 1)
    assert len(calls) == 2


def test_discover_returns_none_when_cli_absent(monkeypatch):
    import errorta_model_gateway.providers.async_cursor_cli as mod

    mod.reset_models_cache()
    monkeypatch.setattr(mod, "resolve_cursor_command_detailed", lambda override_path=None: None)
    assert mod.discover_cursor_routes() is None


def test_discover_returns_none_on_empty_or_unparseable(monkeypatch):
    calls: list = []
    mod = _stub_discovery(monkeypatch, stdout="not a model list\n\n", calls=calls)
    assert mod.discover_cursor_routes(_now=1.0) is None


def test_list_routes_prefers_live_then_falls_back(monkeypatch):
    calls: list = []
    mod = _stub_discovery(monkeypatch, stdout=_MODELS_STDOUT, calls=calls)
    handler = mod.CursorCliHandler()

    # configured + discovery available → live routes.
    live = handler.list_routes(configured=True)
    assert any(r.route_id == "cursor_cli.gpt-5.3-codex" for r in live)

    # Not configured → never shells out, returns the curated fallback.
    calls.clear()
    mod.reset_models_cache()
    fallback = handler.list_routes(configured=False)
    assert calls == []
    assert any(r.route_id == "cursor_cli.default" for r in fallback)

    # configured but discovery fails → curated fallback, dropdown never empty.
    mod.reset_models_cache()
    monkeypatch.setattr(mod, "run_cli_capture", lambda argv, *, timeout=8.0: None)
    miss = handler.list_routes(configured=True)
    assert miss == list(mod._DEFAULT_ROUTES)
    assert len(miss) >= 1
