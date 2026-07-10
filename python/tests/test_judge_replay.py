"""Tests for F-WEDGE-DEEPEN-V1: judge replay subsystem."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from errorta_judge import metrics, replay


# ---------- helpers ----------


def _seed(corpus: str | None, rating: str, *, accepted: bool = False) -> str:
    eid = metrics.record_verdict(
        prompt=f"prompt-{rating}",
        answer="ans",
        verdict={"rating": rating, "failure_tags": [], "confidence": 0.5},
        judge_model="llama3.1:8b",
        corpus=corpus,
    )
    if accepted:
        metrics.record_acceptance(eid, "correction")
    return eid


def _make_pipeline_improved() -> MagicMock:
    """Pipeline mock returning an 'improved' verdict (pass + high conf)."""
    pipe = MagicMock()
    fake_result = MagicMock()
    fake_result.answer = "replay answer"
    fake_result.raw_verdict = {
        "rating": "pass",
        "reason": "now correct",
        "failure_tags": [],
        "confidence": 0.95,
    }
    fake_result.verdict = None
    fake_result.grounding_match = {"kind": "exact", "similarity": None}
    pipe.answer = MagicMock(return_value=fake_result)
    return pipe


# ---------- unit tests ----------


def test_record_verdict_accepts_corpus_and_persists(tmp_errorta_home: Path) -> None:
    eid = metrics.record_verdict(
        prompt="p", answer="a", verdict={"rating": "pass"}, judge_model=None,
        corpus="kitchen",
    )
    line = metrics.log_path().read_text(encoding="utf-8").splitlines()[0]
    entry = json.loads(line)
    assert entry["id"] == eid
    assert entry["corpus"] == "kitchen"


def test_record_verdict_back_compat_without_corpus(tmp_errorta_home: Path) -> None:
    # Hand-write an old-format line (no corpus field).
    log = metrics.log_path()
    log.write_text(
        json.dumps(
            {
                "id": "abc123",
                "prompt": "p",
                "answer": "a",
                "verdict": {"rating": "pass"},
                "judge_model": None,
                "accepted": False,
                "correction": None,
                "created_at": "2026-06-01T00:00:00+00:00",
                "prompt_signature": "sig",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    # Reading should not raise; entries return corpus=None implicitly.
    entries = list(metrics._iter_entries())
    assert len(entries) == 1
    assert entries[0].get("corpus") is None


def test_list_verdicts_for_corpus_filters_accepted(tmp_errorta_home: Path) -> None:
    _seed("kitchen", "pass")
    _seed("kitchen", "fail", accepted=True)  # should be excluded
    _seed("kitchen", "partial")
    _seed("garage", "pass")  # different corpus

    out = replay.list_verdicts_for_corpus("kitchen")
    # Exactly two non-accepted kitchen entries.
    ratings = sorted(e["verdict"]["rating"] for e in out)
    assert ratings == ["partial", "pass"]
    assert all(e.get("corpus") == "kitchen" for e in out)


def test_list_verdicts_for_corpus_with_zero_returns_empty(
    tmp_errorta_home: Path,
) -> None:
    assert replay.list_verdicts_for_corpus("nothing-here") == []


def test_replay_verdict_happy_path(tmp_errorta_home: Path) -> None:
    pipe = _make_pipeline_improved()
    entry = {
        "id": "e1",
        "prompt": "what is the airspeed?",
        "answer": "unknown",
        "verdict": {"rating": "fail", "confidence": 0.2, "failure_tags": []},
        "judge_model": "llama3.1:8b",
        "corpus": "aerospace",
        "grounding_match": None,
    }
    result = replay.replay_verdict(entry, pipe)
    assert result.score_delta > 0
    assert result.replay_verdict["rating"] == "pass"
    assert result.replay_grounding_match == {"kind": "exact", "similarity": None}
    assert result.grounding_change == "added"
    pipe.answer.assert_called_once()


def test_replay_dry_run_no_pipeline_calls(tmp_errorta_home: Path) -> None:
    _seed("kitchen", "fail")
    _seed("kitchen", "partial")
    pipe = MagicMock()

    import asyncio

    async def _collect():
        out = []
        async for r in replay.replay_corpus_stream(
            "kitchen", pipe, dry_run=True
        ):
            out.append(r)
        return out

    results = asyncio.run(_collect())
    assert len(results) == 2
    assert all(r.replay_answer == "" for r in results)
    assert all(r.score_delta == 0.0 for r in results)
    pipe.answer.assert_not_called()


# ---------- route tests ----------


def _client(monkeypatch: pytest.MonkeyPatch, pipeline: MagicMock) -> TestClient:
    from fastapi import FastAPI

    from errorta_app.routes import judge as judge_routes

    monkeypatch.setattr(judge_routes, "_pipeline", pipeline)
    app = FastAPI()
    app.include_router(judge_routes.router)
    return TestClient(app)


def test_replay_endpoint_dry_run_json(
    tmp_errorta_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed("kitchen", "fail")
    _seed("kitchen", "partial")
    pipe = MagicMock()
    client = _client(monkeypatch, pipe)

    resp = client.post(
        "/judge/replay",
        json={"corpus": "kitchen", "dry_run": True},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 2
    for item in data:
        assert "prompt" in item
        assert "original_verdict" in item
        assert "score_delta" in item
    pipe.answer.assert_not_called()


def test_replay_corpus_stream_sse_format(
    tmp_errorta_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed("kitchen", "fail")
    _seed("kitchen", "partial")
    pipe = _make_pipeline_improved()
    client = _client(monkeypatch, pipe)

    with client.stream(
        "POST",
        "/judge/replay",
        json={"corpus": "kitchen", "dry_run": False},
        headers={"Accept": "text/event-stream"},
    ) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        body = b"".join(resp.iter_bytes()).decode("utf-8")

    frames = [f for f in body.split("\n\n") if f.strip()]
    assert len(frames) == 2
    for frame in frames:
        assert frame.startswith("data: ")
        payload = json.loads(frame[len("data: "):])
        assert "prompt" in payload
        assert payload["replay_verdict"]["rating"] == "pass"


def test_replay_endpoint_empty_corpus_returns_empty_list(
    tmp_errorta_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipe = MagicMock()
    client = _client(monkeypatch, pipe)
    resp = client.post(
        "/judge/replay",
        json={"corpus": "no-such-corpus", "dry_run": True},
    )
    assert resp.status_code == 200
    assert resp.json() == []
