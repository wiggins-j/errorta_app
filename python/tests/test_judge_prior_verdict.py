"""Tests for prior-verdict listing — backs the verdict-diff wedge surface.

Covers ``errorta_judge.metrics.list_prior_verdicts`` (helper) plus the
``GET /judge/prior-verdicts`` endpoint mounted by
``errorta_app.routes.judge``.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_app.routes import judge as judge_routes
from errorta_judge import metrics
from errorta_query.signature import prompt_signature


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(judge_routes.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_judge_model_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(judge_routes, "_judge_model_override", None, raising=True)
    monkeypatch.delenv("EVAL_JUDGE_MODEL", raising=False)
    yield


# ---------- list_prior_verdicts (helper) ----------


def test_list_prior_verdicts_empty_history(tmp_errorta_home: Path) -> None:
    sig = prompt_signature("anything")
    assert metrics.list_prior_verdicts(sig) == []


def test_list_prior_verdicts_excludes_current(tmp_errorta_home: Path) -> None:
    prompt = "what orbits earth?"
    sig = prompt_signature(prompt)
    metrics.record_verdict(prompt, "a1", {"rating": "fail"}, None, prompt_signature=sig)
    metrics.record_verdict(prompt, "a2", {"rating": "pass"}, None, prompt_signature=sig)
    priors = metrics.list_prior_verdicts(sig)
    # Only the older one — the most recent is "current".
    assert len(priors) == 1
    assert priors[0]["verdict"]["rating"] == "fail"


def test_list_prior_verdicts_honors_limit(tmp_errorta_home: Path) -> None:
    prompt = "p"
    sig = prompt_signature(prompt)
    for i in range(5):
        metrics.record_verdict(
            prompt, f"a{i}", {"rating": "pass"}, None, prompt_signature=sig
        )
    # 5 total → 4 priors available, clamped to limit=2.
    priors = metrics.list_prior_verdicts(sig, limit=2)
    assert len(priors) == 2


def test_list_prior_verdicts_skips_acceptance_supersede(tmp_errorta_home: Path) -> None:
    prompt = "p"
    sig = prompt_signature(prompt)
    eid1 = metrics.record_verdict(
        prompt, "a1", {"rating": "fail"}, None, prompt_signature=sig
    )
    metrics.record_acceptance(eid1, "fix-1")  # superseding follow-up — skipped
    metrics.record_verdict(prompt, "a2", {"rating": "pass"}, None, prompt_signature=sig)
    priors = metrics.list_prior_verdicts(sig)
    # The superseding follow-up must not double-count eid1.
    assert len(priors) == 1
    assert priors[0]["verdict"]["rating"] == "fail"


def test_list_prior_verdicts_legacy_fallback(tmp_errorta_home: Path) -> None:
    """Legacy entries (no persisted prompt_signature) match via recompute."""
    prompt = "legacy prompt"
    sig = prompt_signature(prompt)

    # Write a legacy entry by hand — no prompt_signature key.
    legacy_entry = {
        "id": "legacy-id",
        "prompt": prompt,
        "answer": "old",
        "verdict": {"rating": "fail", "failure_tags": []},
        "judge_model": None,
        "accepted": False,
        "correction": None,
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    with metrics.log_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(legacy_entry) + "\n")

    # Then a modern entry as "current".
    metrics.record_verdict(prompt, "new", {"rating": "pass"}, None, prompt_signature=sig)

    priors = metrics.list_prior_verdicts(sig)
    assert len(priors) == 1
    assert priors[0]["verdict"]["rating"] == "fail"


def test_list_prior_verdicts_isolates_by_signature(tmp_errorta_home: Path) -> None:
    sig_a = prompt_signature("prompt A")
    sig_b = prompt_signature("prompt B")
    metrics.record_verdict("prompt A", "a", {"rating": "pass"}, None, prompt_signature=sig_a)
    metrics.record_verdict("prompt A", "a2", {"rating": "fail"}, None, prompt_signature=sig_a)
    metrics.record_verdict("prompt B", "b", {"rating": "pass"}, None, prompt_signature=sig_b)

    priors_a = metrics.list_prior_verdicts(sig_a)
    assert len(priors_a) == 1
    assert priors_a[0]["verdict"]["rating"] == "pass"

    priors_b = metrics.list_prior_verdicts(sig_b)
    # Only one entry for B → that's "current" → no priors.
    assert priors_b == []


# ---------- GET /judge/prior-verdicts (endpoint) ----------


def test_prior_verdicts_endpoint_400_on_blank(
    client: TestClient, tmp_errorta_home: Path
) -> None:
    r = client.get("/judge/prior-verdicts", params={"signature": "  "})
    assert r.status_code == 400


def test_prior_verdicts_endpoint_returns_shape(
    client: TestClient,
    tmp_errorta_home: Path,
    mock_aiar_pipeline: MagicMock,
) -> None:
    # Two recorded verdicts on the same prompt → one prior expected.
    v1 = client.post("/judge/verdict", json={"prompt": "what orbits earth?"}).json()
    sig = v1["prompt_signature"]
    assert isinstance(sig, str) and len(sig) == 64
    client.post("/judge/verdict", json={"prompt": "what orbits earth?"})

    r = client.get("/judge/prior-verdicts", params={"signature": sig, "limit": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["signature"] == sig
    assert isinstance(body["priors"], list)
    assert len(body["priors"]) == 1
    prior = body["priors"][0]
    assert prior["verdict"]["rating"] == "pass"
    assert "created_at" in prior
