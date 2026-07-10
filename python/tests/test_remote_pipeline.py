"""Tests for ``errorta_query.remote_pipeline.RemoteHttpPipeline``.

Covers F-INFRA-12 Phase B Slice 8: the proxy pipeline that forwards
judge calls to a remote sidecar over the SSH tunnel (or to a cloud
HTTPS URL). Each test pins one slice of behavior:

* URL stripping, token header presence/absence
* request shape (method, target, JSON body) for both
  ``answer`` and ``record_grounding``
* 5xx, 4xx, network-error and network-error fail-loud behavior
* the security regression: the auth token must not appear in any
  ``PipelineError`` string ever raised by this module
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from errorta_query.remote_pipeline import PipelineError, RemoteHttpPipeline

# ---------------------------------------------------------------------------
# Local fixture: install a fake httpx.Client whose post() is fully scriptable.
# ---------------------------------------------------------------------------


class _RecordedRequest:
    def __init__(self) -> None:
        self.url: str | None = None
        self.json: dict[str, Any] | None = None
        self.headers: dict[str, str] | None = None


@pytest.fixture
def fake_post(monkeypatch: pytest.MonkeyPatch):
    """Patch ``httpx.Client`` so ``.post`` is observable + scriptable.

    Returns a small handle:
        handle.recorded — captured (url, json, headers) of the last call
        handle.set_response(status_code=..., json_data=...)
        handle.set_exception(exc)
        handle.set_json_raises(exc) — make response.json() raise
    Subsequent ``RemoteHttpPipeline`` methods will use the configured
    response (or raise the configured exception) on the next call.
    """
    recorded = _RecordedRequest()

    state: dict[str, Any] = {
        "exc": None,
        "status_code": 200,
        "json_data": {"answer": "", "verdict": None},
        "json_raises": None,
    }

    def _post(url, *, json=None, headers=None):  # noqa: A002 - mirrors httpx
        recorded.url = url
        recorded.json = json
        recorded.headers = headers
        if state["exc"] is not None:
            raise state["exc"]
        resp = MagicMock()
        resp.status_code = state["status_code"]
        if state["json_raises"] is not None:
            resp.json.side_effect = state["json_raises"]
        else:
            resp.json.return_value = state["json_data"]
        return resp

    client_instance = MagicMock()
    client_instance.post.side_effect = _post
    client_instance.__enter__ = MagicMock(return_value=client_instance)
    client_instance.__exit__ = MagicMock(return_value=False)

    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: client_instance)

    class _Handle:
        def __init__(self) -> None:
            self.recorded = recorded

        def set_response(self, *, status_code: int = 200, json_data: Any = None) -> None:
            state["exc"] = None
            state["json_raises"] = None
            state["status_code"] = status_code
            state["json_data"] = json_data if json_data is not None else {}

        def set_exception(self, exc: BaseException) -> None:
            state["exc"] = exc

        def set_json_raises(self, exc: BaseException) -> None:
            state["exc"] = None
            state["json_raises"] = exc

    return _Handle()


# ---------------------------------------------------------------------------
# Construction / URL hygiene
# ---------------------------------------------------------------------------


def test_base_url_strips_trailing_slash() -> None:
    p = RemoteHttpPipeline("http://127.0.0.1:18770/")
    assert p.base_url == "http://127.0.0.1:18770"


def test_base_url_strips_multiple_trailing_slashes() -> None:
    # rstrip("/") collapses any run of trailing slashes — defends
    # against operator-typed config with copy-paste double slashes.
    p = RemoteHttpPipeline("https://errorta.example.com///")
    assert p.base_url == "https://errorta.example.com"


def test_empty_base_url_raises() -> None:
    with pytest.raises(ValueError):
        RemoteHttpPipeline("   ")


# ---------------------------------------------------------------------------
# answer() — happy path + request-shape assertions
# ---------------------------------------------------------------------------


def test_answer_posts_to_judge_verdict_no_token(fake_post) -> None:
    fake_post.set_response(
        status_code=200,
        json_data={
            "id": "x",
            "prompt": "what orbits earth?",
            "answer": "the moon",
            "verdict": {
                "rating": "good",
                "reason": "ok",
                "failure_tags": [],
                "confidence": 0.9,
            },
            "judge_model": "llama3.1:8b",
            "prompt_signature": "a" * 64,
        },
    )

    p = RemoteHttpPipeline("http://127.0.0.1:18770", timeout_s=5.0)
    result = p.answer(prompt="what orbits earth?", corpus="aerospace", judge=True, reground=True)

    assert fake_post.recorded.url == "http://127.0.0.1:18770/judge/verdict"
    assert fake_post.recorded.json == {
        "prompt": "what orbits earth?",
        "corpus": "aerospace",
    }
    assert fake_post.recorded.headers is not None
    assert fake_post.recorded.headers.get("Content-Type") == "application/json"
    # No token → no X-Errorta-Token header.
    assert "X-Errorta-Token" not in fake_post.recorded.headers

    assert result.answer == "the moon"
    assert result.verdict is not None
    assert result.verdict.rating == "good"
    assert result.verdict.usable is True
    assert result.aiar is True
    assert result.prompt_signature == "a" * 64
    assert result.model == "llama3.1:8b"


def test_answer_sends_token_header_when_configured(fake_post) -> None:
    fake_post.set_response(
        status_code=200,
        json_data={"answer": "ok", "verdict": None},
    )

    p = RemoteHttpPipeline("https://errorta.example.com", token="s3cret-token-abc")
    p.answer(prompt="hi", corpus="c", judge=True, reground=False)

    headers = fake_post.recorded.headers or {}
    assert headers.get("X-Errorta-Token") == "s3cret-token-abc"


def test_answer_forwards_judge_model_when_set(fake_post) -> None:
    fake_post.set_response(status_code=200, json_data={"answer": "", "verdict": None})

    p = RemoteHttpPipeline("http://127.0.0.1:18770")
    p.answer(prompt="hi", corpus="c", judge=True, reground=True, model="qwen2:7b")

    assert fake_post.recorded.json is not None
    assert fake_post.recorded.json["judge_model"] == "qwen2:7b"


def test_answer_preserves_remote_aiar_telemetry(fake_post) -> None:
    fake_post.set_response(
        status_code=200,
        json_data={
            "answer": "ok",
            "verdict": None,
            "model": "llama3.1:8b",
            "call_id": "call-remote",
            "instance": "welcome",
            "grounded": True,
            "reground_applied": False,
            "rag_enabled": True,
            "latency": 31.5,
        },
    )

    p = RemoteHttpPipeline("http://127.0.0.1:18770")
    result = p.answer(prompt="hi", corpus="welcome", judge=True, reground=True)

    assert result.model == "llama3.1:8b"
    assert result.call_id == "call-remote"
    assert result.instance == "welcome"
    assert result.grounded is True
    assert result.reground_applied is False
    assert result.rag_enabled is True
    assert result.latency == 31.5


def test_answer_skips_corpus_when_empty(fake_post) -> None:
    fake_post.set_response(status_code=200, json_data={"answer": "", "verdict": None})

    p = RemoteHttpPipeline("http://127.0.0.1:18770")
    p.answer(prompt="hi", corpus="", judge=True, reground=True)

    assert fake_post.recorded.json == {"prompt": "hi"}


# ---------------------------------------------------------------------------
# answer() — failure modes (fail loud, no silent fallback)
# ---------------------------------------------------------------------------


def test_answer_502_raises_pipeline_error(fake_post) -> None:
    fake_post.set_response(status_code=502, json_data={"detail": "bad gateway"})

    p = RemoteHttpPipeline("http://127.0.0.1:18770")
    with pytest.raises(PipelineError) as ei:
        p.answer(prompt="hi", corpus="c", judge=True, reground=True)
    assert "502" in str(ei.value)


def test_answer_4xx_raises_pipeline_error(fake_post) -> None:
    fake_post.set_response(status_code=400, json_data={"detail": "bad"})

    p = RemoteHttpPipeline("http://127.0.0.1:18770")
    with pytest.raises(PipelineError) as ei:
        p.answer(prompt="hi", corpus="c", judge=True, reground=True)
    assert "400" in str(ei.value)


def test_answer_network_error_raises_pipeline_error(fake_post) -> None:
    fake_post.set_exception(httpx.ConnectError("connection refused"))

    p = RemoteHttpPipeline("http://127.0.0.1:18770")
    with pytest.raises(PipelineError) as ei:
        p.answer(prompt="hi", corpus="c", judge=True, reground=True)
    assert "remote unreachable" in str(ei.value)


def test_answer_malformed_json_raises_pipeline_error(fake_post) -> None:
    fake_post.set_json_raises(ValueError("not json"))

    p = RemoteHttpPipeline("http://127.0.0.1:18770")
    with pytest.raises(PipelineError):
        p.answer(prompt="hi", corpus="c", judge=True, reground=True)


def test_answer_pipeline_error_never_contains_token(fake_post) -> None:
    """Security regression: token must not appear in any error string.

    Simulate a network error whose ``str(exc)`` *did* happen to embed
    the token (defensive: ``httpx`` doesn't normally do this, but a
    middleware or proxy could). The adapter redacts before raising.
    """
    secret = "tok-must-never-leak"
    fake_post.set_exception(httpx.ConnectError("refused with header tok-must-never-leak"))

    p = RemoteHttpPipeline("https://errorta.example.com", token=secret)
    with pytest.raises(PipelineError) as ei:
        p.answer(prompt="hi", corpus="c", judge=True, reground=True)
    assert secret not in str(ei.value)
    assert "<redacted>" in str(ei.value)


def test_query_strict_forwards_strict_remote_retrieval(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def _fake_remote_aiar_retrieve(**kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr(
        "errorta_query.aiar_retrieve.remote_aiar_retrieve",
        _fake_remote_aiar_retrieve,
    )

    p = RemoteHttpPipeline("http://127.0.0.1:18770")
    assert p.query_strict(prompt="q", corpus_ids=["welcome"], top_k=3) == []
    assert calls == [
        {
            "prompt": "q",
            "corpus_ids": ["welcome"],
            "top_k": 3,
            "strict": True,
        }
    ]


# ---------------------------------------------------------------------------
# record_grounding()
# ---------------------------------------------------------------------------


def test_record_grounding_posts_to_judge_accept(fake_post) -> None:
    fake_post.set_response(status_code=200, json_data={"id": "x"})

    p = RemoteHttpPipeline("http://127.0.0.1:18770")
    ok = p.record_grounding(
        prompt="p",
        answer="a",
        correction="the right one",
        verdict={"rating": "fail", "reason": "wrong"},
    )

    assert ok is True
    assert fake_post.recorded.url == "http://127.0.0.1:18770/judge/accept"
    assert fake_post.recorded.json == {
        "prompt": "p",
        "answer": "a",
        "correction": "the right one",
        "verdict": {"rating": "fail", "reason": "wrong"},
    }


def test_record_grounding_returns_false_on_4xx(fake_post) -> None:
    fake_post.set_response(status_code=404, json_data={"detail": "id missing"})

    p = RemoteHttpPipeline("http://127.0.0.1:18770")
    ok = p.record_grounding(prompt="p", answer="a", correction=None, verdict=None)
    assert ok is False


def test_record_grounding_returns_false_on_network_error(fake_post) -> None:
    fake_post.set_exception(httpx.ConnectError("nope"))

    p = RemoteHttpPipeline("http://127.0.0.1:18770")
    ok = p.record_grounding(prompt="p", answer="a", correction="x", verdict=None)
    assert ok is False


def test_record_grounding_sends_token_header(fake_post) -> None:
    fake_post.set_response(status_code=200, json_data={})

    p = RemoteHttpPipeline("https://errorta.example.com", token="t-123")
    p.record_grounding(prompt="p", answer="a", correction="c", verdict=None)

    headers = fake_post.recorded.headers or {}
    assert headers.get("X-Errorta-Token") == "t-123"


def test_record_grounding_forwards_instance_when_present(fake_post) -> None:
    fake_post.set_response(status_code=200, json_data={})

    p = RemoteHttpPipeline("https://errorta.example.com")
    p.record_grounding(
        prompt="p", answer="a", correction="c", verdict=None, instance="welcome"
    )

    assert fake_post.recorded.json is not None
    assert fake_post.recorded.json["instance"] == "welcome"
