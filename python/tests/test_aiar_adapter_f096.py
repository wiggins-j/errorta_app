from __future__ import annotations

import sys
from types import ModuleType

import pytest

from errorta_judge.aiar_adapter import AiarGroundingRecordError, AiarPipeline
from errorta_query.signature import prompt_signature


def _install_grounding_store(monkeypatch: pytest.MonkeyPatch, store: ModuleType) -> None:
    aiar = ModuleType("aiar")
    grounding = ModuleType("aiar.grounding")
    grounding.store = store  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "aiar", aiar)
    monkeypatch.setitem(sys.modules, "aiar.grounding", grounding)
    monkeypatch.setitem(sys.modules, "aiar.grounding.store", store)


def test_record_grounding_uses_signature_verdict_correction_and_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []
    store = ModuleType("aiar.grounding.store")

    def record(**kwargs):
        calls.append(kwargs)

    store.record = record  # type: ignore[attr-defined]
    _install_grounding_store(monkeypatch, store)
    pipe = object.__new__(AiarPipeline)

    ok = pipe.record_grounding(
        prompt="prompt text",
        answer="wrong answer",
        correction="right answer",
        verdict={"rating": "good", "reason": "accepted", "failure_tags": "x"},
        instance="welcome",
    )

    assert ok is True
    assert calls == [
        {
            "signature": prompt_signature("prompt text"),
            "verdict": {
                "rating": "pass",
                "reason": "accepted",
                "failure_tags": ["x"],
                "confidence": None,
            },
            "correction": "right answer",
            "instance": "welcome",
        }
    ]
    assert "wrong answer" not in calls[0].values()


def test_record_grounding_raises_typed_error_for_stale_aiar_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple, dict]] = []
    store = ModuleType("aiar.grounding.store")

    def record(*args, **kwargs):
        calls.append((args, kwargs))
        raise TypeError("unexpected keyword argument 'signature'")

    store.record = record  # type: ignore[attr-defined]
    _install_grounding_store(monkeypatch, store)
    pipe = object.__new__(AiarPipeline)

    with pytest.raises(AiarGroundingRecordError):
        pipe.record_grounding(
            prompt="prompt text",
            answer="wrong answer",
            correction="right answer",
            verdict={"rating": "fail"},
            instance="welcome",
        )

    assert len(calls) == 1
    assert calls[0][0] == ()
    assert calls[0][1]["signature"] == prompt_signature("prompt text")


def test_answer_preserves_existing_aiar_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    pipe = object.__new__(AiarPipeline)

    def _answer_prompt(self, prompt, corpus, judge, judge_model):
        assert corpus == "welcome"
        return {
            "answer": "grounded answer",
            "verdict": {"rating": "pass", "reason": "ok", "failure_tags": []},
            "call_id": "call-123",
            "instance": "welcome",
            "model": "llama3.1:8b",
            "grounded": True,
            "reground_applied": False,
            "rag_enabled": True,
            "latency": "42.5",
        }

    monkeypatch.setattr(AiarPipeline, "_invoke_answer_prompt", _answer_prompt)

    result = pipe.answer(
        prompt="what orbits earth?",
        corpus="welcome",
        judge=True,
        reground=True,
        model=None,
    )

    assert result.answer == "grounded answer"
    assert result.model == "llama3.1:8b"
    assert result.call_id == "call-123"
    assert result.instance == "welcome"
    assert result.grounded is True
    assert result.reground_applied is False
    assert result.rag_enabled is True
    assert result.latency == 42.5
    assert result.retrieval.grounded is True
