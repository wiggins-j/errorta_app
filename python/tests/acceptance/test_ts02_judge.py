"""TS-02 — Judge & Grounding: acceptance journey.

Drives the judge surface end to end with a mocked pipeline (the AUTH/UX path is
under test, not the model): run a verdict (TC-02.1/02.2) -> read metrics
(TC-02.8) -> get/set the judge model (TC-02.6) -> preflight (judge readiness) ->
prior-verdicts for a signature (TC-02.4/02.5).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_app.routes import judge as judge_routes

pytestmark = [pytest.mark.acceptance, pytest.mark.regression]


@pytest.fixture
def client(tmp_errorta_home, monkeypatch) -> TestClient:
    monkeypatch.setattr(judge_routes, "_judge_model_override", None, raising=True)
    monkeypatch.setattr(judge_routes, "_pipeline", None, raising=True)
    monkeypatch.delenv("EVAL_JUDGE_MODEL", raising=False)
    app = FastAPI()
    app.include_router(judge_routes.router)
    return TestClient(app)


def test_ts02_judge_journey(client, mock_aiar_pipeline: MagicMock) -> None:
    # TC-02.1/02.2: run a prompt -> grounded answer + verdict (rating/latency).
    verdict = client.post("/judge/verdict", json={"prompt": "what orbits earth?"})
    assert verdict.status_code == 200
    body = verdict.json()
    assert body["answer"] == "stub answer"
    assert body["verdict"]["rating"] == "pass"
    assert body["verdict"]["latency_ms"] is not None
    assert body["id"]

    # TC-02.8: metrics surface.
    assert client.get("/judge/metrics").status_code == 200

    # TC-02.6: get + set the judge model round-trips.
    assert client.get("/judge/model").status_code == 200
    put = client.put("/judge/model", json={"judge_model": "mistral-small3.1"})
    assert put.status_code == 200
    assert put.json()["judge_model"] == "mistral-small3.1"

    # Judge readiness preflight is reachable.
    assert client.get("/judge/preflight").status_code == 200

    # TC-02.4/02.5: prior-verdicts for the prompt's signature.
    sig = body["prompt_signature"]
    assert len(sig) == 64
    again = client.post("/judge/verdict", json={"prompt": "what orbits earth?"})
    assert again.status_code == 200
    priors = client.get(f"/judge/prior-verdicts?signature={sig}")
    assert priors.status_code == 200
    prior_body = priors.json()
    assert prior_body["signature"] == sig
    assert len(prior_body["priors"]) == 1
    assert prior_body["priors"][0]["verdict"]["rating"] == "pass"
