"""F-DEMO-01 runner — uses TestClient with the mock_aiar_pipeline fixture.

Mirrors the test_judge_routes.py pattern. No live Ollama, no real network.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_app.routes import judge as judge_routes
from errorta_benchmark.prompts import BenchmarkPrompt
from errorta_benchmark.runner import BenchmarkRunner


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(judge_routes.router)
    return TestClient(app)


def _prompt(pid: str) -> BenchmarkPrompt:
    return BenchmarkPrompt(
        id=pid,
        text=f"primary {pid}",
        paraphrase=f"para {pid}",
        expected_topics=[],
    )


def test_orchestrate_run_sends_primary_text_only(
    client: TestClient,
    tmp_errorta_home: Path,
    mock_aiar_pipeline: MagicMock,
) -> None:
    runner = BenchmarkRunner(client)
    prompts = [_prompt("a"), _prompt("b")]
    out = runner.orchestrate_run(prompts, re_run_paraphrase=False)

    assert len(out) == 2
    assert all(v.is_paraphrase_re_run is False for v in out)
    assert [v.prompt_text for v in out] == ["primary a", "primary b"]
    assert all(v.rating == "pass" for v in out)
    assert all(v.score == 1.0 for v in out)
    # Two prompts posted; pipeline.answer called twice.
    assert mock_aiar_pipeline.answer.call_count == 2


def test_orchestrate_run_appends_paraphrase_re_runs(
    client: TestClient,
    tmp_errorta_home: Path,
    mock_aiar_pipeline: MagicMock,
) -> None:
    runner = BenchmarkRunner(client)
    prompts = [_prompt("a"), _prompt("b")]
    out = runner.orchestrate_run(prompts, re_run_paraphrase=True)

    assert len(out) == 4
    primary = [v for v in out if not v.is_paraphrase_re_run]
    paraphrase = [v for v in out if v.is_paraphrase_re_run]
    assert [v.prompt_text for v in primary] == ["primary a", "primary b"]
    assert [v.prompt_text for v in paraphrase] == ["para a", "para b"]
    assert mock_aiar_pipeline.answer.call_count == 4


def test_orchestrate_run_records_failed_placeholder_on_http_500(
    client: TestClient,
    tmp_errorta_home: Path,
    mock_aiar_pipeline: MagicMock,
) -> None:
    # Empty prompt path returns 400. To force 500-ish failure observable to
    # the runner, we hand it a client whose post() returns a 500.
    class BadResp:
        status_code = 500

        def json(self) -> dict:
            return {"detail": "boom"}

    class BadClient:
        def post(self, url: str, json: dict):  # noqa: A002 - mirrors stdlib
            return BadResp()

    runner = BenchmarkRunner(BadClient())
    out = runner.orchestrate_run([_prompt("a")])
    assert len(out) == 1
    v = out[0]
    assert v.rating == "error"
    assert v.score == 0.0
    assert v.error is not None and "500" in v.error


def test_orchestrate_run_rejects_empty_prompt_via_real_router(
    client: TestClient,
    tmp_errorta_home: Path,
    mock_aiar_pipeline: MagicMock,
) -> None:
    """A prompt whose text passes through the router as empty yields error rating.

    Constructing a BenchmarkPrompt directly with empty text isn't possible via
    the YAML loader, but the runner should still treat the 400 as an error
    placeholder rather than raising.
    """
    runner = BenchmarkRunner(client)
    bad = BenchmarkPrompt(id="x", text="   ", paraphrase="   ", expected_topics=[])
    out = runner.orchestrate_run([bad])
    assert len(out) == 1
    assert out[0].rating == "error"
    assert out[0].score == 0.0
