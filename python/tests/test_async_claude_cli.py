"""F040 — claude_cli handler tests (mocked subprocess, deterministic).

Drives the handler with a fake ``claude`` subprocess so the logic is tested
without a real CLI: argv construction, stdin (not argv) prompt, JSON parse,
usage, and the error/timeout paths. Also verifies the registry wires
claude_cli/codex_cli without poisoning the existing handlers.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from errorta_council.gateway_local import FatalError, RetryableError
from errorta_model_gateway.providers.async_base import AsyncProviderRequest
from errorta_model_gateway.providers.async_claude_cli import ClaudeCliHandler


class _FakeProc:
    def __init__(self, *, stdout=b"", stderr=b"", returncode=0, hang=False, wait_hang=False):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._hang = hang
        self._wait_hang = wait_hang
        self.terminated = False
        self.killed = False
        self.wait_calls = 0
        self.stdin_input: bytes | None = None

    async def communicate(self, input=None):
        self.stdin_input = input
        if self._hang:
            await asyncio.sleep(60)  # exceed the test timeout
        return self._stdout, self._stderr

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    async def wait(self):
        self.wait_calls += 1
        if self._wait_hang:
            await asyncio.sleep(60)
        return self.returncode


def _patch_exec(monkeypatch, proc=None, *, raises=None):
    """Patch create_subprocess_exec in the shared runner; capture argv."""
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


def _ok_json(text="The capital of France is Paris.", inp=2139, out=10):
    return json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": text, "num_turns": 1,
        "usage": {"input_tokens": inp, "output_tokens": out},
        "total_cost_usd": 0.0109,
    }).encode("utf-8")


def _req(model="haiku", prompt="What is the capital of France?"):
    return AsyncProviderRequest(
        model=model,
        messages=[{"role": "system", "content": "Be terse."},
                  {"role": "user", "content": prompt}],
        max_output_tokens=256, timeout_seconds=5,
    )


@pytest.mark.asyncio
async def test_parses_result_and_usage(monkeypatch):
    _patch_exec(monkeypatch, _FakeProc(stdout=_ok_json()))
    r = await ClaudeCliHandler().call(_req(), api_key=None)
    assert r.content == "The capital of France is Paris."
    assert r.provider_class == "claude_cli"
    assert r.model == "haiku"
    assert r.input_tokens == 2139 and r.output_tokens == 10
    assert r.raw_usage_available is True


def _cache_json(text="Reviewed.", inp=2, out=140, cache_read=9000, cache_write=120):
    """F143-01 Slice A: a claude-CLI payload whose ``usage`` carries cache fields.

    The CLI prompt-caches the piped prompt, so a cache-heavy review turn reports a
    tiny ``input_tokens`` (the uncached remainder) with the real bulk in
    ``cache_read_input_tokens`` — the "2 in" reviewer from the motivating run.
    """
    return json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": text, "num_turns": 1,
        "usage": {
            "input_tokens": inp, "output_tokens": out,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write,
        },
    }).encode("utf-8")


@pytest.mark.asyncio
async def test_captures_cache_tokens_when_present(monkeypatch):
    _patch_exec(monkeypatch, _FakeProc(stdout=_cache_json()))
    r = await ClaudeCliHandler().call(_req(), api_key=None)
    # Headline stays the (tiny) measured input; cache is captured as detail (D4).
    assert r.input_tokens == 2 and r.output_tokens == 140
    assert r.cache_read_input_tokens == 9000
    # Name map: cache_creation_input_tokens (provider) -> cache_write (our field).
    assert r.cache_write_input_tokens == 120
    assert r.raw_usage_available is True


@pytest.mark.asyncio
async def test_cache_tokens_absent_leaves_none(monkeypatch):
    # A payload with no cache fields keeps the cache slots at None (unchanged).
    _patch_exec(monkeypatch, _FakeProc(stdout=_ok_json()))
    r = await ClaudeCliHandler().call(_req(), api_key=None)
    assert r.cache_read_input_tokens is None
    assert r.cache_write_input_tokens is None


@pytest.mark.asyncio
async def test_constrained_argv_and_prompt_on_stdin_not_argv(monkeypatch):
    proc = _FakeProc(stdout=_ok_json())
    captured = _patch_exec(monkeypatch, proc)
    await ClaudeCliHandler().call(_req(model="sonnet", prompt="SECRET-PROMPT-XYZ"), api_key=None)
    argv = captured["argv"]
    # Load-bearing constraint + format + model.
    assert "-p" in argv
    assert "--tools" in argv and argv[argv.index("--tools") + 1] == ""
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
    assert "--model" in argv and argv[argv.index("--model") + 1] == "sonnet"
    assert "--max-turns" in argv
    # Prompt is on stdin, NEVER argv (ARG_MAX + ps-leak).
    assert not any("SECRET-PROMPT-XYZ" in a for a in argv)
    assert proc.stdin_input is not None and b"SECRET-PROMPT-XYZ" in proc.stdin_input
    # Isolated cwd + new session for kill-reach.
    assert captured["kwargs"].get("cwd")
    assert captured["kwargs"].get("start_new_session") is True


# --------------------------------------------------------------------------- #
# Spec 11 (P1a) — read-only in-turn worktree retrieval for DEV turns.
# --------------------------------------------------------------------------- #

_READONLY_TOOLS = {"Read", "Grep", "Glob"}
# Anything that could write files, run commands, or hit the network. NONE of
# these may ever appear in the retrieval allowlist — that would bypass the
# coding_turn.v1 review envelope.
_FORBIDDEN_TOOLS = {
    "Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "BashOutput",
    "KillShell", "WebFetch", "WebSearch", "Task",
}


def _req_with_worktree(root, model="opus", prompt="fix the audio init"):
    return AsyncProviderRequest(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_output_tokens=2048, timeout_seconds=30,
        extra={"metadata": {"dev_repo_read_root": str(root)}},
    )


def _tools_value(argv):
    """The single string handed to --tools (empty string when disabled)."""
    return argv[argv.index("--tools") + 1]


@pytest.mark.asyncio
async def test_dev_repo_read_sets_worktree_cwd_and_readonly_tools(monkeypatch, tmp_path):
    """GOLDEN (config half): a DEV turn carrying a worktree root runs with
    cwd=worktree, a READ-ONLY tool allowlist, and a raised turn budget — and NO
    write/exec/network tool is in the allowlist."""
    (tmp_path / "src").mkdir()
    proc = _FakeProc(stdout=_ok_json(text=json.dumps(
        {"schema_version": "coding_turn.v1", "role": "dev"})))
    captured = _patch_exec(monkeypatch, proc)

    await ClaudeCliHandler().call(_req_with_worktree(tmp_path), api_key=None)
    argv = captured["argv"]

    # cwd is the REAL worktree, not an isolated temp dir.
    assert captured["kwargs"]["cwd"] == str(tmp_path)
    # Read-only allowlist — exactly the three read tools, nothing else.
    tools = {t for t in _tools_value(argv).split(",") if t}
    assert tools == _READONLY_TOOLS
    assert not (tools & _FORBIDDEN_TOOLS)
    # Raised turn budget (a few read/grep calls before the envelope).
    assert int(argv[argv.index("--max-turns") + 1]) > 1


@pytest.mark.asyncio
async def test_dev_repo_read_lets_the_model_reach_the_other_file(monkeypatch, tmp_path):
    """GOLDEN (access half): reconstruct the window.Audio / window.AudioModule
    two-file mismatch. With cwd=worktree the CLI process can actually READ the
    producer file (audio.js) that the pre-baked context omitted. The fake CLI
    reads it FROM ITS cwd and reports the symbol — proving the dev can now reach
    the definition it previously couldn't see."""
    src = tmp_path / "src"
    src.mkdir()
    # fileA references symbolX; fileB defines it under a DIFFERENT name.
    (src / "main.js").write_text("if (window.Audio) { window.Audio.init(); }\n")
    (src / "audio.js").write_text("window.AudioModule = { init() {} };\n")

    import re

    async def fake_exec(*argv, **kwargs):
        # Simulate the read-only agentic loop: read audio.js from the cwd the
        # runner set (the worktree), extract the registered global, and emit the
        # correct fix envelope. If cwd were an empty temp dir this OPEN fails.
        import os
        cwd = kwargs["cwd"]
        body = open(os.path.join(cwd, "src", "audio.js")).read()  # noqa: SIM115
        m = re.search(r"window\.(\w+)\s*=", body)
        found = m.group(1) if m else "NONE"
        envelope = json.dumps({
            "schema_version": "coding_turn.v1", "role": "dev",
            "intent": {"kind": "tool_plan", "task_type": "implementation",
                       "tool_calls": [{"tool": "code_write", "args": {
                           "path": "src/main.js",
                           "content": f"if (window.{found}) {{ window.{found}.init(); }}\n"}}]}})
        return _FakeProc(stdout=_ok_json(text=envelope, out=42))

    import errorta_model_gateway.providers._cli_common as common
    monkeypatch.setattr(common.asyncio, "create_subprocess_exec", fake_exec)

    r = await ClaudeCliHandler().call(_req_with_worktree(tmp_path), api_key=None)
    # The dev reached audio.js and produced the PRODUCER-side name it never had.
    assert "AudioModule" in r.content
    assert "window.Audio " not in r.content  # the wrong consumer-only guess is gone


@pytest.mark.asyncio
async def test_envelope_parses_after_tool_use_turns(monkeypatch, tmp_path):
    """CRITICAL: enabling tools must not break envelope parsing. The CLI's
    terminal result JSON (num_turns>1, i.e. tool-use turns preceded it) still
    carries the coding_turn.v1 envelope as its final text, and the real
    parse_coding_turn accepts it."""
    tmp_path.joinpath("src").mkdir()
    envelope = json.dumps({
        "schema_version": "coding_turn.v1", "role": "dev", "task_id": "t1",
        "intent": {"kind": "tool_plan", "task_type": "implementation",
                   "tool_calls": [{"tool": "code_write",
                                   "args": {"path": "src/main.js",
                                            "content": "// fixed\n"}}]}})
    # num_turns=4 => several tool-use turns happened before the final message.
    result_json = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": envelope, "num_turns": 4,
        "usage": {"input_tokens": 10, "output_tokens": 30},
    }).encode("utf-8")
    _patch_exec(monkeypatch, _FakeProc(stdout=result_json))

    r = await ClaudeCliHandler().call(_req_with_worktree(tmp_path), api_key=None)

    from errorta_council.coding.schemas import TurnParseError, parse_coding_turn
    parsed = parse_coding_turn("dev", "t1", r.content)
    assert not isinstance(parsed, TurnParseError)
    assert parsed.intent.tool_calls[0].tool == "code_write"


@pytest.mark.asyncio
async def test_gate_off_absent_metadata_uses_tempdir_and_no_tools(monkeypatch):
    """Gate OFF (no worktree metadata) => the legacy single-shot path is byte-for-
    byte unchanged: empty tools, --max-turns 1, isolated temp-dir cwd."""
    proc = _FakeProc(stdout=_ok_json())
    captured = _patch_exec(monkeypatch, proc)
    await ClaudeCliHandler().call(_req(), api_key=None)  # plain request, no extra
    argv = captured["argv"]
    assert _tools_value(argv) == ""                       # empty allowlist
    assert argv[argv.index("--max-turns") + 1] == "1"     # single-shot
    # cwd is an isolated temp dir (errorta-claude-cli-*), never a real tree.
    assert "errorta-claude-cli-" in captured["kwargs"]["cwd"]


@pytest.mark.asyncio
async def test_nonexistent_worktree_root_fails_safe_to_tempdir(monkeypatch):
    """A worktree root that does not exist must NOT be used as cwd — fall back to
    the isolated temp dir + empty tools (never point cwd at a bad path)."""
    proc = _FakeProc(stdout=_ok_json())
    captured = _patch_exec(monkeypatch, proc)
    req = AsyncProviderRequest(
        model="opus", messages=[{"role": "user", "content": "hi"}],
        max_output_tokens=64, timeout_seconds=5,
        extra={"metadata": {"dev_repo_read_root": "/no/such/worktree/xyz"}})
    await ClaudeCliHandler().call(req, api_key=None)
    argv = captured["argv"]
    assert _tools_value(argv) == ""
    assert argv[argv.index("--max-turns") + 1] == "1"
    assert "errorta-claude-cli-" in captured["kwargs"]["cwd"]


@pytest.mark.asyncio
async def test_not_installed(monkeypatch):
    _patch_exec(monkeypatch, raises=FileNotFoundError("claude"))
    with pytest.raises(FatalError) as e:
        await ClaudeCliHandler().call(_req(), api_key=None)
    assert "not_installed" in str(e.value)


@pytest.mark.asyncio
async def test_not_authenticated(monkeypatch):
    _patch_exec(monkeypatch, _FakeProc(stderr=b"Please log in to continue", returncode=1))
    with pytest.raises(FatalError) as e:
        await ClaudeCliHandler().call(_req(), api_key=None)
    assert "not_authenticated" in str(e.value)


@pytest.mark.asyncio
async def test_is_error_rate_limit_is_retryable(monkeypatch):
    err = json.dumps({"type": "result", "is_error": True,
                      "result": "usage limit reached"}).encode()
    _patch_exec(monkeypatch, _FakeProc(stdout=err))
    with pytest.raises(RetryableError):
        await ClaudeCliHandler().call(_req(), api_key=None)


@pytest.mark.asyncio
async def test_empty_result_is_fatal(monkeypatch):
    empty = json.dumps({"type": "result", "is_error": False, "result": ""}).encode()
    _patch_exec(monkeypatch, _FakeProc(stdout=empty))
    with pytest.raises(FatalError) as e:
        await ClaudeCliHandler().call(_req(), api_key=None)
    assert "empty_result" in str(e.value)


@pytest.mark.asyncio
async def test_unparseable_output_is_fatal(monkeypatch):
    _patch_exec(monkeypatch, _FakeProc(stdout=b"not json at all"))
    with pytest.raises(FatalError) as e:
        await ClaudeCliHandler().call(_req(), api_key=None)
    assert "unparseable" in str(e.value)


@pytest.mark.asyncio
async def test_timeout_terminates_and_is_retryable(monkeypatch):
    proc = _FakeProc(hang=True)
    _patch_exec(monkeypatch, proc)
    with pytest.raises(RetryableError) as e:
        await ClaudeCliHandler().call(_req(), api_key=None)
    assert "timeout" in str(e.value)
    assert proc.terminated is True


def test_validate_route():
    h = ClaudeCliHandler()
    assert h.validate_route("claude_cli.haiku").ok is True
    assert h.validate_route("claude_cli.").ok is False
    assert h.validate_route("anthropic.x").ok is False


def test_resolves_claude_outside_path(monkeypatch, tmp_path):
    """The bundled .app has a minimal PATH that excludes ~/.local/bin. The
    handler must still find claude in a known install location."""
    import errorta_model_gateway.providers._cli_common as common
    import errorta_model_gateway.providers.async_claude_cli as mod

    # Not on PATH, and home points at an empty tmp dir...
    monkeypatch.setattr(common.shutil, "which", lambda _name, path=None: None)
    monkeypatch.setattr(mod.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(common.os.path, "isfile", lambda p: False)
    monkeypatch.setattr(common.os, "access", lambda p, mode: False)
    assert mod.resolve_claude_binary() is None
    assert mod.is_available() is False

    # ...but present at a known candidate location under home.
    fake = tmp_path / ".local" / "bin" / "claude"
    fake.parent.mkdir(parents=True)
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setattr(common.os.path, "isfile", lambda p: p == str(fake))
    monkeypatch.setattr(common.os, "access", lambda p, mode: p == str(fake))
    assert mod.resolve_claude_binary() == str(fake)
    assert mod.is_available() is True


@pytest.mark.asyncio
async def test_terminate_then_kill_reaps_the_process():
    # When SIGTERM doesn't land within grace, the cascade must SIGKILL AND then
    # reap (a second wait) — no zombie (review BLOCKER #1).
    import errorta_model_gateway.providers._cli_common as common
    proc = _FakeProc(wait_hang=True)  # wait() never returns → both graces time out
    await common._terminate_then_kill(proc, grace=0.05)
    assert proc.terminated is True
    assert proc.killed is True
    assert proc.wait_calls >= 2  # grace-after-terminate AND grace-after-kill (reap)


@pytest.mark.asyncio
async def test_semaphore_caps_concurrent_spawns(monkeypatch):
    # The cap must bound concurrent *spawned processes*, not just I/O — the
    # spawn happens inside the semaphore (review BLOCKER #2). Drive 5 calls at
    # once and assert peak concurrency never exceeds the cap (2).
    import errorta_model_gateway.providers._cli_common as common
    import errorta_model_gateway.providers.async_claude_cli as mod

    state = {"cur": 0, "peak": 0}

    class _CountingProc(_FakeProc):
        async def communicate(self, input=None):
            state["cur"] += 1
            state["peak"] = max(state["peak"], state["cur"])
            await asyncio.sleep(0.05)  # hold the slot so calls overlap
            state["cur"] -= 1
            return _ok_json(), b""

    async def fake_exec(*argv, **kwargs):
        return _CountingProc()

    monkeypatch.setattr(common.asyncio, "create_subprocess_exec", fake_exec)
    # Fresh semaphore bound to this test's loop, cap 2.
    monkeypatch.setattr(mod, "_CLAUDE_SEMAPHORE", asyncio.Semaphore(2))

    results = await asyncio.gather(*[
        ClaudeCliHandler().call(_req(), api_key=None) for _ in range(5)
    ])
    assert len(results) == 5
    assert all(r.content for r in results)
    assert state["peak"] <= 2, f"peak concurrency {state['peak']} exceeded cap 2"


def test_registry_wires_cli_providers_without_poisoning():
    # The bootstrap import must register claude_cli/codex_cli/cursor_cli AND leave the
    # existing handlers intact (a poisoned bootstrap would silently drop all).
    from errorta_model_gateway.providers import async_registry
    async_registry.ensure_bootstrapped()
    assert async_registry.get_handler("claude_cli") is not None
    assert async_registry.get_handler("codex_cli") is not None
    assert async_registry.get_handler("cursor_cli") is not None
    assert async_registry.get_handler("anthropic") is not None  # not poisoned


def test_clean_subprocess_env_strips_pyinstaller_loader_vars(monkeypatch):
    """The spawned CLI must not inherit PyInstaller's DYLD_* loader injection
    (it crashes the vendor CLI's runtime inside the frozen .app)."""
    import errorta_model_gateway.providers._cli_common as common

    monkeypatch.setenv("DYLD_LIBRARY_PATH", "/var/folders/_MEIxxxx/lib")
    monkeypatch.setenv("DYLD_LIBRARY_PATH_ORIG", "/original/lib")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/frozen/lib")  # no _ORIG -> dropped
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    env = common._clean_subprocess_env()
    # _ORIG value restored, _MEI value gone.
    assert env["DYLD_LIBRARY_PATH"] == "/original/lib"
    assert "DYLD_LIBRARY_PATH_ORIG" not in env
    # No _ORIG to restore -> the var is removed entirely.
    assert "LD_LIBRARY_PATH" not in env
    # PATH augmented with the common toolchain dirs.
    assert "/opt/homebrew/bin" in env["PATH"]
    assert "/usr/local/bin" in env["PATH"]
