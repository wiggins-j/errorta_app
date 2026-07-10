"""F040 — codex_cli handler tests (mocked subprocess, deterministic).

Verified against codex-cli 0.133: `--json` emits a JSONL event stream; the
answer is the last `item.completed`/`agent_message`, usage is in
`turn.completed`, and failures arrive as `error`/`turn.failed` events on stdout
(exit 1, empty stderr). A ChatGPT-account Codex rejects explicit `-m`.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from errorta_model_gateway.providers.async_base import AsyncProviderRequest
from errorta_model_gateway.providers.async_codex_cli import CodexCliHandler
from errorta_council.gateway_local import FatalError, RetryableError


class _FakeProc:
    def __init__(self, *, stdout=b"", stderr=b"", returncode=0, hang=False):
        self._stdout, self._stderr = stdout, stderr
        self.returncode = returncode
        self._hang = hang
        self.terminated = self.killed = False
        self.stdin_input = None

    async def communicate(self, input=None):
        self.stdin_input = input
        if self._hang:
            await asyncio.sleep(60)
        return self._stdout, self._stderr

    def terminate(self): self.terminated = True
    def kill(self): self.killed = True
    async def wait(self): return self.returncode


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


def _jsonl(text="The capital of France is Paris.", inp=28919, out=40):
    return (
        json.dumps({"type": "thread.started", "thread_id": "t1"}) + "\n"
        + json.dumps({"type": "turn.started"}) + "\n"
        + json.dumps({"type": "item.completed",
                      "item": {"id": "item_0", "type": "agent_message", "text": text}}) + "\n"
        + json.dumps({"type": "turn.completed",
                      "usage": {"input_tokens": inp, "output_tokens": out}}) + "\n"
    ).encode("utf-8")


def _err_jsonl(message="The 'gpt-5' model is not supported when using Codex with a ChatGPT account."):
    return (
        json.dumps({"type": "thread.started"}) + "\n"
        + json.dumps({"type": "error", "message": message}) + "\n"
        + json.dumps({"type": "turn.failed", "error": {"message": message}}) + "\n"
    ).encode("utf-8")


def _req(model="default", prompt="What is the capital of France?"):
    return AsyncProviderRequest(
        model=model, messages=[{"role": "user", "content": prompt}],
        max_output_tokens=64, timeout_seconds=5,
    )


def _handler():
    # Pin a fake binary so the handler doesn't depend on real codex resolution.
    return CodexCliHandler(binary="/fake/codex")


@pytest.mark.asyncio
async def test_parses_agent_message_and_usage(monkeypatch):
    _patch(monkeypatch, _FakeProc(stdout=_jsonl()))
    r = await _handler().call(_req(), api_key=None)
    assert r.content == "The capital of France is Paris."
    assert r.provider_class == "codex_cli"
    assert r.input_tokens == 28919 and r.output_tokens == 40
    assert r.raw_usage_available is True


def _cache_jsonl(text="Reviewed.", inp=2, out=140, cache_read=9000, cache_write=120):
    """F143-01 Slice A: a codex-CLI JSONL stream whose ``turn.completed`` usage
    event carries cache fields (detail only, D4)."""
    return (
        json.dumps({"type": "thread.started", "thread_id": "t1"}) + "\n"
        + json.dumps({"type": "item.completed",
                      "item": {"id": "item_0", "type": "agent_message", "text": text}}) + "\n"
        + json.dumps({"type": "turn.completed",
                      "usage": {
                          "input_tokens": inp, "output_tokens": out,
                          "cache_read_input_tokens": cache_read,
                          "cache_creation_input_tokens": cache_write,
                      }}) + "\n"
    ).encode("utf-8")


@pytest.mark.asyncio
async def test_captures_cache_tokens_when_present(monkeypatch):
    _patch(monkeypatch, _FakeProc(stdout=_cache_jsonl()))
    r = await _handler().call(_req(), api_key=None)
    assert r.input_tokens == 2 and r.output_tokens == 140
    assert r.cache_read_input_tokens == 9000
    # Name map: cache_creation_input_tokens (provider) -> cache_write (our field).
    assert r.cache_write_input_tokens == 120
    assert r.raw_usage_available is True


@pytest.mark.asyncio
async def test_cache_tokens_absent_leaves_none(monkeypatch):
    _patch(monkeypatch, _FakeProc(stdout=_jsonl()))
    r = await _handler().call(_req(), api_key=None)
    assert r.cache_read_input_tokens is None
    assert r.cache_write_input_tokens is None


@pytest.mark.asyncio
async def test_default_model_omits_dash_m_and_prompt_on_stdin(monkeypatch):
    proc = _FakeProc(stdout=_jsonl())
    cap = _patch(monkeypatch, proc)
    await _handler().call(_req(model="default", prompt="SECRET-CDX"), api_key=None)
    argv = cap["argv"]
    assert "exec" in argv and "--json" in argv
    assert "--sandbox" in argv and argv[argv.index("--sandbox") + 1] == "read-only"
    assert "--skip-git-repo-check" in argv
    assert "-m" not in argv  # ChatGPT-account default rejects explicit -m
    assert not any("SECRET-CDX" in a for a in argv)  # prompt on stdin, not argv
    assert proc.stdin_input is not None and b"SECRET-CDX" in proc.stdin_input
    assert cap["kwargs"].get("start_new_session") is True


@pytest.mark.asyncio
async def test_explicit_model_passes_dash_m(monkeypatch):
    cap = _patch(monkeypatch, _FakeProc(stdout=_jsonl()))
    await _handler().call(_req(model="o3"), api_key=None)
    argv = cap["argv"]
    assert "-m" in argv and argv[argv.index("-m") + 1] == "o3"


@pytest.mark.asyncio
async def test_model_rejected_error_event_is_fatal(monkeypatch):
    # exit 1 + error JSONL on stdout (empty stderr) — the real failure shape.
    _patch(monkeypatch, _FakeProc(stdout=_err_jsonl(), returncode=1))
    with pytest.raises(FatalError) as e:
        await _handler().call(_req(model="gpt-5"), api_key=None)
    assert "model_rejected" in str(e.value) or "not supported" in str(e.value)


@pytest.mark.asyncio
async def test_not_installed_when_binary_unresolved(monkeypatch):
    import errorta_model_gateway.providers.async_codex_cli as mod
    monkeypatch.setattr(mod, "resolve_codex_binary", lambda: None)
    with pytest.raises(FatalError) as e:
        await CodexCliHandler().call(_req(), api_key=None)
    assert "not_installed" in str(e.value)


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
    no_msg = (json.dumps({"type": "turn.completed", "usage": {}}) + "\n").encode()
    _patch(monkeypatch, _FakeProc(stdout=no_msg))
    with pytest.raises(FatalError) as e:
        await _handler().call(_req(), api_key=None)
    assert "empty_result" in str(e.value)


@pytest.mark.asyncio
async def test_stderr_auth_error_is_fatal(monkeypatch):
    # Error on stderr + non-zero exit (the non-JSONL failure path).
    _patch(monkeypatch, _FakeProc(stderr=b"Error: 401 unauthorized, please log in", returncode=1, stdout=b""))
    with pytest.raises(FatalError) as e:
        await _handler().call(_req(), api_key=None)
    assert "not_authenticated" in str(e.value)


@pytest.mark.asyncio
async def test_rate_limit_error_event_is_retryable(monkeypatch):
    _patch(monkeypatch, _FakeProc(stdout=_err_jsonl("429 usage limit reached"), returncode=1))
    with pytest.raises(RetryableError) as e:
        await _handler().call(_req(), api_key=None)
    assert "rate_limited" in str(e.value)


@pytest.mark.asyncio
async def test_transient_model_error_is_not_misclassified_as_fatal(monkeypatch):
    # Review #4: a transient error that merely mentions "model" + "not" (e.g. a
    # network failure) must NOT be classified as a fatal model rejection — it
    # falls through to a generic failure, not codex_cli_model_rejected.
    _patch(monkeypatch, _FakeProc(stdout=_err_jsonl("could not reach the model endpoint (network)"), returncode=1))
    with pytest.raises(FatalError) as e:
        await _handler().call(_req(), api_key=None)
    assert "model_rejected" not in str(e.value)


def test_validate_route():
    h = _handler()
    assert h.validate_route("codex_cli.default").ok is True
    assert h.validate_route("codex_cli.").ok is False
    assert h.validate_route("openai.x").ok is False
