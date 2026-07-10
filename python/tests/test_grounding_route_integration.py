"""F024 integration tests — /judge/verdict surfaces grounding_match.

Hermetic: ``mock_aiar_pipeline`` short-circuits the AIAR adapter, and the
embedding fetch is monkeypatched so no Ollama call is made.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_app.routes import judge as judge_routes
from errorta_query import embeddings as emb
from errorta_query import grounding
from errorta_query.signature import prompt_signature


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(judge_routes.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_judge_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(judge_routes, "_judge_model_override", None, raising=True)
    monkeypatch.delenv("EVAL_JUDGE_MODEL", raising=False)


def _seed_grounding(
    canonical_prompt: str, correction: str, embedding: list[float]
) -> str:
    """Seed both SHA-256 and embedding stores; return the signature."""
    sig = prompt_signature(canonical_prompt)
    grounding.record(sig, correction)
    emb.EmbeddingStore().append(sig, embedding, canonical_prompt)
    return sig


def test_verdict_returns_similar_match_when_env_on(
    client: TestClient,
    tmp_errorta_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_aiar_pipeline: MagicMock,
) -> None:
    monkeypatch.setenv("ERRORTA_GROUNDING_EMBEDDINGS", "1")

    canonical = "what is the speed of light in vacuum?"
    sig = _seed_grounding(canonical, "299,792,458 m/s", [1.0, 0.0, 0.0])

    # Deterministic embedding for the paraphrased query — close to canonical.
    def _fake_embed(*args: Any, **kwargs: Any) -> list[float]:
        return [0.99, 0.05, 0.0]

    import errorta_query.embeddings as _e

    monkeypatch.setattr(_e, "ollama_embed", _fake_embed)

    paraphrased = "how fast does light travel in a vacuum?"
    resp = client.post("/judge/verdict", json={"prompt": paraphrased})
    assert resp.status_code == 200
    body = resp.json()

    # Paraphrased prompt has a different SHA-256, so the similarity layer fires.
    assert body["prompt_signature"] != sig
    match = body["grounding_match"]
    assert match is not None
    assert match["kind"] == "similar"
    assert match["original_signature"] == sig
    assert match["similarity"] is not None
    assert match["similarity"] >= 0.85
    assert body["prior_correction"] == "299,792,458 m/s"
    called_prompt = mock_aiar_pipeline.answer.call_args.kwargs["prompt"]
    assert paraphrased in called_prompt
    assert "semantically similar accepted correction" in called_prompt
    assert "299,792,458 m/s" in called_prompt


def test_verdict_returns_exact_match_when_signature_hits(
    client: TestClient,
    tmp_errorta_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_aiar_pipeline: MagicMock,
) -> None:
    # Env state irrelevant for exact match; default-off should still work.
    monkeypatch.delenv("ERRORTA_GROUNDING_EMBEDDINGS", raising=False)

    canonical = "what orbits the earth?"
    sig = _seed_grounding(canonical, "the moon", [1.0, 0.0])

    resp = client.post("/judge/verdict", json={"prompt": canonical})
    assert resp.status_code == 200
    body = resp.json()

    match = body["grounding_match"]
    assert match is not None
    assert match["kind"] == "exact"
    assert match["original_signature"] == sig
    assert match["similarity"] is None
    assert body["prior_correction"] == "the moon"
    called_prompt = mock_aiar_pipeline.answer.call_args.kwargs["prompt"]
    assert canonical in called_prompt
    assert "exact accepted correction" in called_prompt
    assert "the moon" in called_prompt


def test_verdict_grounding_match_absent_when_env_off(
    client: TestClient,
    tmp_errorta_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_aiar_pipeline: MagicMock,
) -> None:
    monkeypatch.delenv("ERRORTA_GROUNDING_EMBEDDINGS", raising=False)

    canonical = "what is the speed of light?"
    _seed_grounding(canonical, "299,792,458 m/s", [1.0, 0.0, 0.0])

    # Even if a similar prompt would semantically match, env-off means no call.
    def _should_not_be_called(*args: Any, **kwargs: Any) -> list[float]:
        raise AssertionError("ollama_embed must not be called when env is off")

    import errorta_query.embeddings as _e

    monkeypatch.setattr(_e, "ollama_embed", _should_not_be_called)

    paraphrased = "how fast does light travel?"
    resp = client.post("/judge/verdict", json={"prompt": paraphrased})
    assert resp.status_code == 200
    body = resp.json()
    # Field is null (absent in semantic sense) when neither layer hits.
    assert body.get("grounding_match") is None


def test_verdict_fails_open_on_embedding_error(
    client: TestClient,
    tmp_errorta_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_aiar_pipeline: MagicMock,
) -> None:
    monkeypatch.setenv("ERRORTA_GROUNDING_EMBEDDINGS", "1")
    _seed_grounding("canonical prompt", "correction", [1.0, 0.0])

    def _boom(*args: Any, **kwargs: Any) -> list[float]:
        raise emb.EmbeddingUnavailable("ollama down")

    import errorta_query.embeddings as _e

    monkeypatch.setattr(_e, "ollama_embed", _boom)

    resp = client.post("/judge/verdict", json={"prompt": "totally different"})
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("grounding_match") is None
