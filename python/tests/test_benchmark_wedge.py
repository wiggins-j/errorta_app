"""BENCH-WEDGE — three-phase REAL-mode benchmark flow.

Hermetic test: drives ``BenchmarkRunner`` with an httpx ``MockTransport``
so the three phases (primary verdict, /judge/accept wedge amplification,
paraphrase re-run) all hit a deterministic in-memory handler. No live
sidecar, no Ollama, no network.

The handler is wired to:
  * return a failing primary verdict for every prompt;
  * accept any ``POST /judge/accept`` with 200 + ``grounding_recorded=True``;
  * return a paraphrase verdict whose ``grounding_match.kind == "similar"``
    for the prompts that were amplified — i.e. the wedge took effect —
    and ``None`` for the rest.

We then assert the new aggregation fields are populated and clearly
distinguishable from the legacy ``F024_paraphrase_delta``.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from errorta_benchmark.aggregator import BenchmarkAggregator
from errorta_benchmark.prompts import BenchmarkPrompt
from errorta_benchmark.runner import BenchmarkRunner, RecordedVerdict


def _prompt(pid: str) -> BenchmarkPrompt:
    return BenchmarkPrompt(
        id=pid,
        text=f"primary text {pid}",
        paraphrase=f"paraphrase text {pid}",
        expected_topics=[],
    )


class _Handler:
    """Stateful httpx MockTransport handler implementing the three phases."""

    def __init__(self) -> None:
        self.accept_calls: list[dict[str, Any]] = []
        # Verdict ids handed out for primary calls, keyed by prompt text.
        self.primary_ids: dict[str, str] = {}
        # Prompt ids that have been "amplified" via /judge/accept.
        self.amplified_ids: set[str] = set()
        # Count of verdict POSTs so we can mint stable ids.
        self._verdict_counter = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content.decode("utf-8")) if request.content else {}

        if path.endswith("/judge/verdict"):
            return self._handle_verdict(body)
        if path.endswith("/judge/accept"):
            return self._handle_accept(body)
        return httpx.Response(404, json={"detail": "unhandled"})

    def _handle_verdict(self, body: dict[str, Any]) -> httpx.Response:
        prompt = str(body.get("prompt") or "")
        self._verdict_counter += 1
        vid = f"v-{self._verdict_counter:04d}"
        # Map prompt text -> prompt id by string matching against "text p1"
        # style. We baked the id into the BenchmarkPrompt text fields, so
        # extract the trailing token.
        prompt_id = prompt.split()[-1] if prompt else ""
        is_paraphrase = prompt.startswith("paraphrase ")

        if not is_paraphrase:
            # Primary pass: fail everything so the wedge has work to do.
            self.primary_ids[prompt_id] = vid
            return httpx.Response(
                200,
                json={
                    "id": vid,
                    "prompt": prompt,
                    "answer": f"[mock] primary {prompt_id}",
                    "verdict": {"rating": "fail", "reason": "mock fail"},
                    # No grounding_match on primary.
                },
            )

        # Paraphrase pass: emit similar grounding match only for amplified
        # prompts; pass-rate over similar-match subset is high (all pass)
        # to make the new metric clearly differ from the legacy delta.
        if prompt_id in self.amplified_ids:
            return httpx.Response(
                200,
                json={
                    "id": vid,
                    "prompt": prompt,
                    "answer": f"[mock] paraphrase {prompt_id}",
                    "verdict": {"rating": "pass", "reason": "wedge lit"},
                    "grounding_match": {
                        "kind": "similar",
                        "similarity": 0.87,
                        "original_signature": f"sig-{prompt_id}",
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "id": vid,
                "prompt": prompt,
                "answer": f"[mock] paraphrase {prompt_id}",
                "verdict": {"rating": "fail", "reason": "no wedge"},
            },
        )

    def _handle_accept(self, body: dict[str, Any]) -> httpx.Response:
        self.accept_calls.append(body)
        vid = str(body.get("id") or "")
        # Reverse-lookup which prompt id the verdict id belongs to.
        for pid, mapped in self.primary_ids.items():
            if mapped == vid:
                self.amplified_ids.add(pid)
                break
        return httpx.Response(
            200,
            json={
                "id": vid,
                "prompt": "",
                "answer": "",
                "correction": body.get("correction"),
                "grounding_recorded": True,
            },
        )


@pytest.fixture
def real_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ERRORTA_REAL_BENCHMARK", "1")
    monkeypatch.setenv("ERRORTA_GROUNDING_EMBEDDINGS", "1")


def _build_client(handler: _Handler) -> httpx.Client:
    transport = httpx.MockTransport(handler)
    return httpx.Client(
        transport=transport, base_url="http://testserver", timeout=5.0
    )


def test_three_phase_run_populates_new_aggregation_fields(
    real_mode: None,
) -> None:
    handler = _Handler()
    client = _build_client(handler)
    # 10 prompts → 10 failed primaries → ~30% amplified ≈ 3 accepts.
    prompts = [_prompt(f"p{i}") for i in range(10)]

    runner = BenchmarkRunner(client=client)
    verdicts = runner.orchestrate_run(prompts, re_run_paraphrase=True)

    # 10 primary + 10 paraphrase = 20 verdicts; amplification phase does
    # NOT add to the verdict log (it lives in accept_calls).
    assert len(verdicts) == 20
    # At least one /judge/accept fired (default 30% of 10 = 3 rounded).
    assert len(handler.accept_calls) >= 1
    for call in handler.accept_calls:
        assert call["id"].startswith("v-")
        assert "[wedge-amplify]" in str(call.get("correction") or "")

    # Paraphrase verdicts carry grounding_match passthrough where the
    # wedge fired.
    paraphrase = [v for v in verdicts if v.is_paraphrase_re_run]
    similar = [v for v in paraphrase if v.grounding_match_kind == "similar"]
    assert similar, "expected at least one similar grounding_match passthrough"
    for v in similar:
        assert v.grounding_match_similarity == pytest.approx(0.87)
        assert v.grounding_match_signature == f"sig-{v.prompt_id}"

    # Aggregate and inspect the new fields.
    agg = BenchmarkAggregator().aggregate(verdicts)
    assert agg.f024_similar_match_count == len(similar)
    assert agg.f024_similar_match_rate is not None
    assert 0.0 < agg.f024_similar_match_rate <= 1.0
    # All similar-match paraphrases pass; their primaries all scored 0.0 →
    # score delta should be +1.0.
    assert agg.f024_similar_match_score_delta == pytest.approx(1.0)
    assert agg.f024_similar_match_mean_similarity == pytest.approx(0.87)

    # Legacy F024_paraphrase_delta is over *all* matched ids; only the
    # amplified subset flipped to pass while the rest stayed fail, so
    # the legacy delta is strictly smaller than +1.0 and must differ from
    # the new score delta.
    assert agg.F024_paraphrase_delta is not None
    assert agg.F024_paraphrase_delta < 1.0
    assert agg.F024_paraphrase_delta != agg.f024_similar_match_score_delta


def test_fake_mode_skips_amplification_phase() -> None:
    """FAKE mode (no real-mode env) must not POST to /judge/accept.

    Regression guard: the existing fake-mode behaviour stays a strict
    two-phase flow with no accept calls.
    """
    handler = _Handler()
    client = _build_client(handler)
    prompts = [_prompt(f"p{i}") for i in range(5)]

    # No env vars set → REAL mode predicate is False; even though we pass
    # the real httpx-style client, the amplification phase is gated by
    # the env vars and should skip.
    runner = BenchmarkRunner(client=client)
    verdicts = runner.orchestrate_run(prompts, re_run_paraphrase=True)

    assert len(verdicts) == 10
    assert handler.accept_calls == []
    # No similar matches because the wedge never fired.
    agg = BenchmarkAggregator().aggregate(verdicts)
    assert agg.f024_similar_match_count == 0
    assert agg.f024_similar_match_rate == 0.0
    assert agg.f024_similar_match_score_delta is None


def test_real_mode_without_embeddings_skips_amplification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REAL mode but no ERRORTA_GROUNDING_EMBEDDINGS=1 → no accept calls."""
    monkeypatch.setenv("ERRORTA_REAL_BENCHMARK", "1")
    monkeypatch.delenv("ERRORTA_GROUNDING_EMBEDDINGS", raising=False)

    handler = _Handler()
    client = _build_client(handler)
    prompts = [_prompt(f"p{i}") for i in range(5)]

    runner = BenchmarkRunner(client=client)
    runner.orchestrate_run(prompts, re_run_paraphrase=True)

    assert handler.accept_calls == []


def test_real_mode_amplifies_partial_primaries(
    real_mode: None,
) -> None:
    """Partial verdicts are correction-worthy for the real benchmark wedge."""

    class PartialHandler(_Handler):
        def _handle_verdict(self, body: dict[str, Any]) -> httpx.Response:
            prompt = str(body.get("prompt") or "")
            self._verdict_counter += 1
            vid = f"v-{self._verdict_counter:04d}"
            prompt_id = prompt.split()[-1] if prompt else ""
            is_paraphrase = prompt.startswith("paraphrase ")
            if not is_paraphrase:
                self.primary_ids[prompt_id] = vid
                return httpx.Response(
                    200,
                    json={
                        "id": vid,
                        "prompt": prompt,
                        "answer": f"[mock] partial {prompt_id}",
                        "verdict": {"rating": "partial", "reason": "mock partial"},
                    },
                )
            return super()._handle_verdict(body)

    handler = PartialHandler()
    client = _build_client(handler)

    runner = BenchmarkRunner(client=client)
    runner.orchestrate_run([_prompt("p1"), _prompt("p2")], re_run_paraphrase=True)

    assert len(handler.accept_calls) == 1


def test_similar_match_delta_uses_scores_not_pass_rate() -> None:
    verdicts = [
        RecordedVerdict("p1", "primary", False, "partial", 0.5),
        RecordedVerdict(
            "p1",
            "para",
            True,
            "partial",
            0.5,
            grounding_match_kind="similar",
            grounding_match_similarity=0.9,
        ),
        RecordedVerdict("p2", "primary", False, "fail", 0.0),
        RecordedVerdict(
            "p2",
            "para",
            True,
            "partial",
            0.5,
            grounding_match_kind="similar",
            grounding_match_similarity=0.9,
        ),
    ]

    agg = BenchmarkAggregator().aggregate(verdicts)

    assert agg.f024_similar_match_score_delta == pytest.approx(0.25)
