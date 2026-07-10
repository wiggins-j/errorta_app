"""F-DEMO-01 real-mode runner — hermetic test with httpx.Client patched.

This test asserts the live-run branch of ``errorta_benchmark.runner``:

  * When ``ERRORTA_REAL_BENCHMARK=1``, ``_client_for_mode`` instantiates
    ``httpx.Client`` (which we patch to a ``MagicMock``).
  * ``orchestrate_run`` POSTs every prompt to ``/judge/verdict`` with a
    JSON payload containing the ``prompt`` key.
  * The mock client's ``.post`` is invoked once per prompt and the returned
    verdict shape is honoured by the runner.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from errorta_benchmark import runner as runner_module
from errorta_benchmark.__main__ import _probe_run_metadata, main
from errorta_benchmark.prompts import BenchmarkPrompt
from errorta_benchmark.runner import _client_for_mode, orchestrate_run


def _prompt(pid: str) -> BenchmarkPrompt:
    return BenchmarkPrompt(
        id=pid,
        text=f"primary {pid}",
        paraphrase=f"para {pid}",
        expected_topics=[],
    )


@pytest.fixture
def patched_httpx_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace ``errorta_benchmark.runner.httpx.Client`` with a MagicMock factory.

    The factory returns a single client instance whose ``.post`` returns a
    canned 200 response with a passing verdict. Tests inspect the instance's
    call log to confirm URL and payload shape.
    """
    instance = MagicMock(name="httpx_client_instance")

    canned_response = MagicMock(name="httpx_response")
    canned_response.status_code = 200
    canned_response.json.return_value = {
        "answer": "live answer",
        "verdict": {"rating": "pass", "reason": "looks good"},
    }
    instance.post.return_value = canned_response

    factory = MagicMock(name="httpx_Client_factory", return_value=instance)
    monkeypatch.setattr(runner_module.httpx, "Client", factory)
    monkeypatch.setenv("ERRORTA_REAL_BENCHMARK", "1")
    return instance


@pytest.mark.parametrize(
    "prompt_ids",
    [
        ["a"],
        ["a", "b", "c"],
    ],
)
def test_real_mode_posts_each_prompt_to_judge_verdict(
    patched_httpx_client: MagicMock, prompt_ids: list[str]
) -> None:
    prompts = [_prompt(pid) for pid in prompt_ids]

    verdicts = orchestrate_run(prompts)

    # One verdict per prompt, all driven through the mocked httpx.Client.
    assert len(verdicts) == len(prompts)
    assert patched_httpx_client.post.call_count == len(prompts)

    # Every call must target /judge/verdict with a JSON payload containing
    # 'prompt'. We accept the URL via positional or keyword form.
    for call, prompt in zip(patched_httpx_client.post.call_args_list, prompts):
        args, kwargs = call
        url = kwargs.get("url") if "url" in kwargs else (args[0] if args else None)
        payload = kwargs.get("json")
        assert url == "/judge/verdict", f"unexpected url: {url!r}"
        assert isinstance(payload, dict) and "prompt" in payload
        assert payload["prompt"] == prompt.text
        assert payload["corpus"] == "welcome"

    # The mocked response carries a passing verdict — confirm it flowed
    # through unchanged so we know the live path consumed the response.
    assert all(v.rating == "pass" for v in verdicts)
    assert all(v.score == 1.0 for v in verdicts)


def test_real_mode_client_uses_configured_judge_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = MagicMock(name="httpx_Client_factory")
    monkeypatch.setattr(runner_module.httpx, "Client", factory)
    monkeypatch.setenv("ERRORTA_REAL_BENCHMARK", "1")
    monkeypatch.setenv("ERRORTA_JUDGE_URL", "http://127.0.0.1:18888/")

    _client_for_mode()

    factory.assert_called_once_with(
        base_url="http://127.0.0.1:18888", timeout=180.0
    )


def test_real_mode_client_falls_back_to_sidecar_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = MagicMock(name="httpx_Client_factory")
    monkeypatch.setattr(runner_module.httpx, "Client", factory)
    monkeypatch.setenv("ERRORTA_REAL_BENCHMARK", "1")
    monkeypatch.delenv("ERRORTA_JUDGE_URL", raising=False)
    monkeypatch.setenv("ERRORTA_SIDECAR_PORT", "18889")

    _client_for_mode()

    factory.assert_called_once_with(
        base_url="http://127.0.0.1:18889", timeout=180.0
    )


def test_benchmark_corpus_can_be_overridden(
    patched_httpx_client: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ERRORTA_BENCHMARK_CORPUS", "custom-corpus")

    orchestrate_run([_prompt("a")])

    payload = patched_httpx_client.post.call_args.kwargs["json"]
    assert payload["corpus"] == "custom-corpus"


def test_real_mode_metadata_uses_judge_url_and_judge_model_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Response:
        def __init__(self, body: dict) -> None:
            self._body = body

        def json(self) -> dict:
            return self._body

    calls: list[str] = []

    def fake_get(url: str, timeout: float) -> _Response:
        calls.append(url)
        if url == "http://127.0.0.1:18890/healthz":
            return _Response({"aiar_pin": {"source": "pinned"}})
        if url == "http://127.0.0.1:18890/judge/model":
            return _Response({"judge_model": "qwen2.5:3b"})
        if url == "http://127.0.0.1:11434/api/version":
            return _Response({"version": "0.24.0"})
        raise AssertionError(f"unexpected URL: {url}")

    import httpx

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setenv("ERRORTA_JUDGE_URL", "http://127.0.0.1:18890/")

    meta = _probe_run_metadata()

    assert meta == {
        "aiar_pin_source": "pinned",
        "judge_model": "qwen2.5:3b",
        "ollama_version": "0.24.0",
    }
    assert calls[:2] == [
        "http://127.0.0.1:18890/healthz",
        "http://127.0.0.1:18890/judge/model",
    ]


def test_cli_real_mode_writes_metadata_without_fake_banner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = tmp_path / "seed.yaml"
    seed.write_text(
        "- id: p1\n"
        "  text: What is Errorta?\n"
        "  paraphrase: Explain Errorta differently.\n",
        encoding="utf-8",
    )
    report_dir = tmp_path / "reports"
    markdown_path = tmp_path / "BENCHMARK.md"
    judge_url = "http://127.0.0.1:9123"

    class _Response:
        def __init__(self, body: dict, status_code: int = 200) -> None:
            self._body = body
            self.status_code = status_code

        def json(self) -> dict:
            return self._body

    class _Client:
        def __init__(self, *, base_url: str, timeout: float) -> None:
            self.base_url = base_url
            self.timeout = timeout
            self.posts: list[tuple[str, dict]] = []

        def post(self, url: str, json: dict) -> _Response:  # noqa: A002
            self.posts.append((url, json))
            return _Response(
                {
                    "id": "verdict-1",
                    "answer": "real answer",
                    "judge_model": "llama3.1:8b",
                    "verdict": {"rating": "pass", "reason": "ok"},
                }
            )

    clients: list[_Client] = []

    def client_factory(*, base_url: str, timeout: float) -> _Client:
        client = _Client(base_url=base_url, timeout=timeout)
        clients.append(client)
        return client

    import httpx

    seen_gets: list[str] = []

    def fake_get(url: str, timeout: float) -> _Response:
        seen_gets.append(url)
        if url == f"{judge_url}/healthz":
            return _Response({"aiar_pin": {"source": "pinned"}})
        if url == f"{judge_url}/judge/model":
            return _Response({"judge_model": None})
        if url == "http://127.0.0.1:11434/api/version":
            return _Response({"version": "0.5.4"})
        return _Response({}, status_code=404)

    monkeypatch.setenv("ERRORTA_REAL_BENCHMARK", "1")
    monkeypatch.setattr(runner_module.httpx, "Client", client_factory)
    monkeypatch.setattr(httpx, "get", fake_get)

    rc = main(
        [
            "--real",
            "--seed",
            str(seed),
            "--report-dir",
            str(report_dir),
            "--output-markdown",
            str(markdown_path),
            "--judge-url",
            judge_url,
        ]
    )

    assert rc == 0
    assert len(clients) == 1
    assert clients[0].base_url == judge_url
    assert clients[0].posts == [
        (
            "/judge/verdict",
            {
                "prompt": "What is Errorta?",
                "_mock_prompt_id": "p1",
                "_mock_phase": "primary",
                "corpus": "welcome",
            },
        )
    ]
    assert f"{judge_url}/healthz" in seen_gets
    assert f"{judge_url}/judge/model" in seen_gets

    md = markdown_path.read_text(encoding="utf-8")
    assert md.startswith("# Errorta benchmark report")
    assert "FAKE DATA" not in md
    assert "## Run metadata" in md
    assert "| judge_model | `llama3.1:8b` |" in md
    assert "| ollama_version | `0.5.4` |" in md
    assert "| aiar_pin_source | `pinned` |" in md
    assert "| seed_sha256 | `" in md

    report = json.loads((report_dir / "latest.json").read_text(encoding="utf-8"))
    assert report["real_run"] is True
    assert report["run_metadata"]["judge_model"] == "llama3.1:8b"
    assert report["run_metadata"]["ollama_version"] == "0.5.4"
    assert report["run_metadata"]["aiar_pin_source"] == "pinned"
    assert report["run_metadata"]["seed_sha256"]
    assert report["verdicts"][0]["raw"]["judge_model"] == "llama3.1:8b"
