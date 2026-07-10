"""F110 — Ollama model-pull module (argv, validation, parsing, streaming)."""
from __future__ import annotations

import subprocess
from typing import List

import pytest

from errorta_ollama import pull as pull_module

# --------------------------------------------------------------------------- #
# model-name validation (injection defense)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name",
    [
        "llama3.2",
        "llama3.2:3b",
        "qwen2.5:7b",
        "library/mistral:latest",
        "registry.example.com/ns/model:tag",
    ],
)
def test_validate_accepts_real_model_names(name: str) -> None:
    assert pull_module.validate_model_name(name) == name


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "--flag",
        "-x",
        "a b",
        "a;rm -rf /",
        "a&&b",
        "a|b",
        "a$(whoami)",
        "a`id`",
        "model name\nwith newline",
    ],
)
def test_validate_rejects_injection_and_garbage(bad: str) -> None:
    with pytest.raises(pull_module.InvalidModelName):
        pull_module.validate_model_name(bad)


# --------------------------------------------------------------------------- #
# installed_models / is_model_installed
# --------------------------------------------------------------------------- #


_LIST_OUTPUT = (
    "NAME              ID            SIZE      MODIFIED\n"
    "llama3.2:latest   abc123        2.0 GB    2 days ago\n"
    "qwen2.5:7b        def456        4.7 GB    1 week ago\n"
)


def _fake_run(stdout: str = "", returncode: int = 0):
    class _Proc:
        def __init__(self) -> None:
            self.stdout = stdout
            self.stderr = ""
            self.returncode = returncode

    def run(argv, **kwargs):
        # Assert argv-only, no shell.
        assert isinstance(argv, list)
        assert argv[0] == "ollama"
        run.calls.append(argv)
        return _Proc()

    run.calls = []  # type: ignore[attr-defined]
    return run


def test_installed_models_parses_list(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_run(_LIST_OUTPUT)
    monkeypatch.setattr(subprocess, "run", fake)
    models = pull_module.installed_models()
    assert models == ["llama3.2:latest", "qwen2.5:7b"]
    assert fake.calls == [["ollama", "list"]]


def test_installed_models_failsoft_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a, **_k):
        raise FileNotFoundError("ollama not found")

    monkeypatch.setattr(subprocess, "run", boom)
    assert pull_module.installed_models() == []


def test_is_model_installed_implicit_latest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", _fake_run(_LIST_OUTPUT))
    # "llama3.2" should match "llama3.2:latest".
    assert pull_module.is_model_installed("llama3.2") is True
    assert pull_module.is_model_installed("llama3.2:latest") is True
    assert pull_module.is_model_installed("qwen2.5:7b") is True
    assert pull_module.is_model_installed("nonexistent") is False


# --------------------------------------------------------------------------- #
# pull_model — argv, streaming, short-circuit, failure
# --------------------------------------------------------------------------- #


class _FakePopen:
    """Minimal Popen stand-in streaming canned stdout lines."""

    def __init__(self, argv, *, lines: List[str], returncode: int = 0, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.stdout = iter(lines)
        self._returncode = returncode

    def wait(self, timeout=None):
        return self._returncode

    def kill(self):
        pass


def _patch_popen(monkeypatch, *, lines, returncode=0, capture):
    def factory(argv, **kwargs):
        capture["argv"] = argv
        return _FakePopen(argv, lines=lines, returncode=returncode, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", factory)


def test_pull_argv_and_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    # Not already installed.
    monkeypatch.setattr(pull_module, "is_model_installed", lambda m: False)
    capture: dict = {}
    _patch_popen(
        monkeypatch,
        lines=[
            "pulling manifest\n",
            "pulling abc...  10%\n",
            "pulling abc...  90%\n",
            "success\n",
        ],
        returncode=0,
        capture=capture,
    )
    frames: List[pull_module.PullProgress] = []
    result = pull_module.pull_model("llama3.2:3b", on_progress=frames.append)

    # argv-only, exact shape.
    assert capture["argv"] == ["ollama", "pull", "llama3.2:3b"]
    assert result.succeeded is True
    assert result.model == "llama3.2:3b"
    # Progress frames streamed, percents parsed.
    percents = [f.percent for f in frames if f.percent is not None]
    assert 10.0 in percents and 90.0 in percents


def test_pull_rejects_injection_before_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"spawn": False}

    def factory(*_a, **_k):
        called["spawn"] = True
        raise AssertionError("Popen must not be called for an invalid name")

    monkeypatch.setattr(subprocess, "Popen", factory)
    with pytest.raises(pull_module.InvalidModelName):
        pull_module.pull_model("--rm; evil")
    assert called["spawn"] is False


def test_pull_already_installed_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pull_module, "is_model_installed", lambda m: True)

    def no_spawn(*_a, **_k):
        raise AssertionError("Popen must not run when model already installed")

    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    frames: List[pull_module.PullProgress] = []
    result = pull_module.pull_model("qwen2.5:7b", on_progress=frames.append)
    assert result.succeeded is True
    assert "already installed" in result.message.lower()
    assert frames and frames[-1].percent == 100.0


def test_pull_failure_surfaces_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pull_module, "is_model_installed", lambda m: False)
    capture: dict = {}
    _patch_popen(
        monkeypatch,
        lines=["pulling manifest\n", "Error: model not found\n"],
        returncode=1,
        capture=capture,
    )
    result = pull_module.pull_model("bogus:404", on_progress=lambda _f: None)
    assert result.succeeded is False
    assert result.error
    assert "model not found" in (result.error or "").lower()


def test_pull_spawn_oserror_is_clean_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pull_module, "is_model_installed", lambda m: False)

    def boom(*_a, **_k):
        raise OSError("no such binary")

    monkeypatch.setattr(subprocess, "Popen", boom)
    result = pull_module.pull_model("llama3.2", on_progress=lambda _f: None)
    assert result.succeeded is False
    assert "could not start ollama" in result.message.lower()
