"""F001 Judge router — verdict review + grounding store wiring.

Endpoints (all mounted under /judge):
  POST /judge/verdict           run prompt through the injected pipeline (judge=True)
  POST /judge/correction-draft  propose initial correction text for the user
  POST /judge/accept            persist a correction via the injected pipeline
  GET  /judge/metrics           pass rate + 7d trend from ~/.errorta/verdicts.jsonl
  GET  /judge/preflight         report judge model availability against Ollama
  GET  /judge/model             current judge model + history
  PUT  /judge/model             switch judge model (process-local for v0.1)

All AIAR calls go through the active ``Pipeline`` resolved by
``default_pipeline()`` at request time, unless tests install an explicit
override. ``default_pipeline()`` selects ``AiarPipeline`` when AIAR is
importable and ``StubPipeline`` otherwise. **No direct ``from aiar`` imports
anywhere in this file.**
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from errorta_judge import correction_draft, metrics, schema_guard
from errorta_judge import replay as _replay
from errorta_judge.latency import stopwatch
from errorta_query import grounding as _stub_grounding
from errorta_query.pipeline import Pipeline, default_pipeline
from errorta_query.signature import prompt_signature as _prompt_signature

router = APIRouter(prefix="/judge", tags=["judge"])


def _alpha_enforce_not_locked() -> None:
    """F-DIST-01 lock gate for the answering surface (lazy import so the alpha
    package is only touched when a request actually runs)."""
    from errorta_alpha.state import enforce_not_locked

    enforce_not_locked()


def _alpha_record_feature(name: str) -> None:
    """F-DIST-01 slice 6 — record an allowlisted feature use (counts only, no
    content). No-op when the alpha gate is off or extras are opted out."""
    try:
        from errorta_alpha import telemetry as _alpha_telemetry

        _alpha_telemetry.record_feature_used(name)
    except Exception:  # pragma: no cover - telemetry must never break answering
        pass


# Optional test/override hook. Production requests resolve ``default_pipeline()``
# at call time so residency/model-policy changes become effective without a
# sidecar restart.
_pipeline: Pipeline | None = None


# ---------- models ----------


class VerdictModel(BaseModel):
    rating: str
    reason: Optional[str] = None
    failure_tags: list[str] = []
    confidence: Optional[float] = None
    latency_ms: Optional[float] = None


class VerdictRequest(BaseModel):
    prompt: str
    corpus: Optional[str] = None
    judge_model: Optional[str] = None


class GroundingMatch(BaseModel):
    kind: str  # "exact" | "similar"
    similarity: Optional[float] = None
    original_signature: Optional[str] = None


class VerdictResponse(BaseModel):
    id: str
    prompt: str
    answer: str
    verdict: VerdictModel
    judge_model: Optional[str] = None
    model: Optional[str] = None
    prior_correction: Optional[str] = None
    prompt_signature: Optional[str] = None
    grounding_match: Optional[GroundingMatch] = None
    call_id: Optional[str] = None
    instance: Optional[str] = None
    grounded: Optional[bool] = None
    reground_applied: Optional[bool] = None
    rag_enabled: Optional[bool] = None
    latency: Optional[float] = None


class PriorVerdictPayload(BaseModel):
    verdict: Optional[VerdictModel] = None
    judge_model: Optional[str] = None
    created_at: Optional[str] = None


class PriorVerdictsResponse(BaseModel):
    signature: str
    priors: list[PriorVerdictPayload]


class CorrectionDraftRequest(BaseModel):
    answer: str
    verdict: VerdictModel


class CorrectionDraftResponse(BaseModel):
    draft: str


class AcceptRequest(BaseModel):
    id: str
    correction: Optional[str] = None


class AcceptResponse(BaseModel):
    id: str
    prompt: str
    answer: str
    correction: Optional[str] = None
    verdict: Optional[VerdictModel] = None
    grounding_recorded: bool
    created_at: Optional[str] = None


class VerdictTimelineEntry(BaseModel):
    rating: str
    judge_model: Optional[str] = None
    created_at: Optional[str] = None
    reason_snippet: Optional[str] = None


class CorrectedPromptEntry(BaseModel):
    prompt: str
    count: int
    prompt_signature: Optional[str] = None
    verdict_timeline: list[VerdictTimelineEntry] = []


class LatencyHistogramBucket(BaseModel):
    label: str
    count: int


class LatencyHistogram(BaseModel):
    buckets: list[LatencyHistogramBucket]
    p50_ms: Optional[float] = None
    p95_ms: Optional[float] = None
    p99_ms: Optional[float] = None


class MetricsResponse(BaseModel):
    total: int
    pass_rate: Optional[float] = None
    total_7d: int
    pass_rate_7d: Optional[float] = None
    rating_counts: dict
    trend_7d: list[dict]
    most_corrected_prompts: list[CorrectedPromptEntry]
    latency_histogram: Optional[LatencyHistogram] = None
    log_path: str


class PreflightResponse(BaseModel):
    judge_model: Optional[str] = None
    judge_model_source: str  # "env" | "default"
    aiar_available: bool
    ollama_reachable: bool
    model_available: Optional[bool] = None
    error: Optional[str] = None
    runtime_kind: Optional[str] = None
    display_name: Optional[str] = None
    aiar_connected: Optional[bool] = None
    backend_id: Optional[str] = None
    answer_available: Optional[bool] = None
    judge_available: Optional[bool] = None
    active_model: Optional[str] = None
    active_model_ready: Optional[bool] = None
    available_models: list[str] = []
    model_source: Optional[str] = None
    capabilities: dict[str, Any] = {}


class ModelGetResponse(BaseModel):
    judge_model: Optional[str] = None
    source: str  # "env" | "override" | "default"


class ModelPutRequest(BaseModel):
    judge_model: Optional[str] = None


# ---------- helpers ----------

# Process-local override; the env var still wins on cold start.
_judge_model_override: Optional[str] = None
DEFAULT_JUDGE_MODEL = "llama3.1:8b"


def _current_judge_model() -> tuple[str | None, str]:
    if _judge_model_override:
        return _judge_model_override, "override"
    env = os.environ.get("EVAL_JUDGE_MODEL")
    if env:
        return env, "env"
    return DEFAULT_JUDGE_MODEL, "default"


def _active_pipeline() -> Pipeline:
    if _pipeline is not None:
        return _pipeline
    return default_pipeline()


def _lookup_grounding_match(
    prompt: str, signature: str
) -> tuple[Optional[GroundingMatch], Optional[str]]:
    """Return the best local grounding match and correction before answering."""
    exact_correction = _stub_grounding.lookup(signature)
    if exact_correction:
        return (
            GroundingMatch(kind="exact", similarity=None, original_signature=signature),
            exact_correction,
        )

    try:
        similar = _stub_grounding.lookup_by_similarity(prompt)
    except Exception:
        similar = None
    if similar is None:
        return None, None

    sim_sig, correction, sim_score = similar
    return (
        GroundingMatch(
            kind="similar",
            similarity=float(sim_score),
            original_signature=sim_sig,
        ),
        correction,
    )


def _prompt_with_grounding_context(
    prompt: str,
    correction: Optional[str],
    grounding_match: Optional[GroundingMatch],
) -> str:
    """Inject accepted correction context while preserving the user's prompt."""
    correction_text = (correction or "").strip()
    if not correction_text:
        return prompt
    source = "accepted correction"
    if grounding_match is not None and grounding_match.kind == "similar":
        source = "semantically similar accepted correction"
    elif grounding_match is not None and grounding_match.kind == "exact":
        source = "exact accepted correction"
    return (
        f"{prompt}\n\n"
        "Errorta grounding context: use the following "
        f"{source} if it is relevant to the user's prompt.\n"
        f"Correction: {correction_text}"
    )


def _pipeline_answer(
    prompt: str,
    corpus: str | None,
    judge_model: str | None,
) -> dict[str, Any]:
    """Run the prompt through the injected pipeline and extract answer + raw verdict.

    The adapter (``AiarPipeline``) attaches the raw judge output as
    ``result.raw_verdict`` so we can hand it to ``schema_guard.normalize_verdict``
    here — keeping verdict-shape policy in one place. The stub pipeline returns
    an already-typed ``Verdict`` and we round-trip it through the same path.
    """
    try:
        result = _active_pipeline().answer(
            prompt=prompt,
            corpus=corpus or "",
            judge=True,
            reground=True,
            model=judge_model,
        )
    except Exception as exc:  # pragma: no cover - pipeline failure
        return {
            "answer": "",
            "verdict_raw": {
                "rating": "fail",
                "reason": f"pipeline error: {exc}",
                "failure_tags": ["pipeline_error"],
            },
        }

    raw = getattr(result, "raw_verdict", None)
    if raw is None and result.verdict is not None:
        # Stub path: round-trip the typed Verdict through the same normalizer.
        raw = result.verdict.to_dict()
    return {
        "answer": result.answer or "",
        "verdict_raw": raw,
        "model": _result_field(result, "model"),
        "call_id": _result_field(result, "call_id"),
        "instance": _result_field(result, "instance"),
        "grounded": _result_field(result, "grounded"),
        "reground_applied": _result_field(result, "reground_applied"),
        "rag_enabled": _result_field(result, "rag_enabled"),
        "latency": _result_field(result, "latency"),
    }


def _result_field(result: Any, name: str) -> Any:
    value = getattr(result, name, None)
    # Most route tests use MagicMock result doubles. Unset MagicMock attributes
    # auto-create child mocks; those are not real telemetry and must not leak
    # into the Pydantic response.
    if value.__class__.__module__ == "unittest.mock":
        return None
    return value


def _record_grounding(
    prompt: str,
    answer: str,
    correction: str | None,
    verdict: dict[str, Any] | None,
    instance: str | None = None,
) -> bool:
    """Persist a correction via the active pipeline and local F024 store."""
    local_ok = False
    if (correction or "").strip():
        try:
            from errorta_query.signature import prompt_signature

            sig = prompt_signature(prompt)
            _stub_grounding.record_with_embedding(sig, correction or "", prompt)
            local_ok = True
        except Exception:
            local_ok = False

    record = getattr(_active_pipeline(), "record_grounding", None)
    if not callable(record):
        return local_ok
    try:
        pipeline_ok = bool(
            record(
                prompt=prompt,
                answer=answer,
                correction=correction,
                verdict=verdict,
                instance=instance,
            )
        )
    except Exception:
        pipeline_ok = False
    return pipeline_ok or local_ok


# ---------- endpoints ----------


@router.post("/verdict", response_model=VerdictResponse)
def run_verdict(req: VerdictRequest) -> VerdictResponse:
    # F-DIST-01 invariant 5: the answering surface is locked server-side when the
    # alpha gate is on and the license is unactivated/expired/revoked/EOL. No-op
    # when the gate is off (production posture).
    _alpha_enforce_not_locked()

    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    # F-DIST-01 slice 6 — count that a judge run happened (name only, never the
    # prompt or any content). No-op unless the alpha gate is on + extras opted in.
    _alpha_record_feature("judge_run")

    judge_model = req.judge_model or _current_judge_model()[0]
    signature = _prompt_signature(prompt)
    grounding_match, grounding_correction = _lookup_grounding_match(prompt, signature)
    answer_prompt = _prompt_with_grounding_context(
        prompt, grounding_correction, grounding_match
    )

    with stopwatch() as sw:
        result = _pipeline_answer(answer_prompt, req.corpus, judge_model)
    latency_ms = sw.elapsed_ms

    answer = result["answer"]
    verdict_dict = schema_guard.normalize_verdict(result.get("verdict_raw"))
    verdict_dict["latency_ms"] = round(latency_ms, 2)

    event_id = metrics.record_verdict(
        prompt=prompt,
        answer=answer,
        verdict=verdict_dict,
        judge_model=judge_model,
        prompt_signature=signature,
        corpus=req.corpus,
    )

    prior_correction = metrics.find_accepted_correction(prompt) or grounding_correction

    return VerdictResponse(
        id=event_id,
        prompt=prompt,
        answer=answer,
        verdict=VerdictModel(**verdict_dict),
        judge_model=judge_model,
        model=result.get("model"),
        prior_correction=prior_correction,
        prompt_signature=signature,
        grounding_match=grounding_match,
        call_id=result.get("call_id"),
        instance=result.get("instance"),
        grounded=result.get("grounded"),
        reground_applied=result.get("reground_applied"),
        rag_enabled=result.get("rag_enabled"),
        latency=result.get("latency"),
    )


@router.get("/prior-verdicts", response_model=PriorVerdictsResponse)
def get_prior_verdicts(
    signature: str = "",
    limit: int = 5,
) -> PriorVerdictsResponse:
    sig = (signature or "").strip()
    if not sig:
        raise HTTPException(status_code=400, detail="signature is required")
    try:
        clamped = int(limit)
    except (TypeError, ValueError):
        clamped = 5
    clamped = max(1, min(20, clamped))

    raw = metrics.list_prior_verdicts(sig, limit=clamped)
    payloads: list[PriorVerdictPayload] = []
    for p in raw:
        v = p.get("verdict")
        payloads.append(
            PriorVerdictPayload(
                verdict=VerdictModel(**v) if isinstance(v, dict) else None,
                judge_model=p.get("judge_model"),
                created_at=p.get("created_at"),
            )
        )
    return PriorVerdictsResponse(signature=sig, priors=payloads)


@router.post("/correction-draft", response_model=CorrectionDraftResponse)
def make_correction_draft(req: CorrectionDraftRequest) -> CorrectionDraftResponse:
    draft = correction_draft.draft_correction(
        answer=req.answer or "",
        verdict=req.verdict.model_dump(),
    )
    return CorrectionDraftResponse(draft=draft)


@router.post("/accept", response_model=AcceptResponse)
def accept(req: AcceptRequest) -> AcceptResponse:
    entry = metrics.record_acceptance(req.id, req.correction)
    if entry is None:
        raise HTTPException(status_code=404, detail="verdict id not found")

    verdict_dict = entry.get("verdict") or None
    grounding_ok = _record_grounding(
        prompt=entry.get("prompt", ""),
        answer=entry.get("answer", ""),
        correction=req.correction,
        verdict=verdict_dict,
        instance=entry.get("corpus"),
    )

    return AcceptResponse(
        id=req.id,
        prompt=entry.get("prompt", ""),
        answer=entry.get("answer", ""),
        correction=req.correction,
        verdict=VerdictModel(**verdict_dict) if verdict_dict else None,
        grounding_recorded=grounding_ok,
        created_at=entry.get("created_at"),
    )


@router.get("/metrics", response_model=MetricsResponse)
def get_metrics() -> MetricsResponse:
    return MetricsResponse(**metrics.summary())


@router.get("/preflight", response_model=PreflightResponse)
def preflight() -> PreflightResponse:
    judge_model, source = _current_judge_model()
    runtime = None
    try:
        from errorta_aiar_connection import resolve_aiar_runtime

        runtime = resolve_aiar_runtime()
    except Exception:
        runtime = None

    if (
        runtime is not None
        and runtime.kind == "disconnected"
        and runtime.config_source != "none"
    ):
        return PreflightResponse(
            judge_model=judge_model,
            judge_model_source=source,
            aiar_available=False,
            ollama_reachable=False,
            model_available=False,
            error=runtime.error_message,
            runtime_kind=runtime.kind,
            display_name=runtime.display_name,
            aiar_connected=False,
            backend_id=runtime.backend_id,
            answer_available=False,
            judge_available=False,
            active_model=runtime.active_model,
            active_model_ready=False,
            available_models=list(runtime.available_models),
            model_source=source,
            capabilities=runtime.capabilities.to_dict(),
        )

    if runtime is not None and runtime.kind not in {"local-aiar", "disconnected"}:
        caps = runtime.capabilities.to_dict()
        return PreflightResponse(
            judge_model=judge_model,
            judge_model_source=source,
            aiar_available=runtime.connected and (runtime.capabilities.answer or runtime.capabilities.judge),
            ollama_reachable=runtime.active_model_ready is not False,
            model_available=runtime.active_model_ready,
            error=runtime.error_message,
            runtime_kind=runtime.kind,
            display_name=runtime.display_name,
            aiar_connected=runtime.connected,
            backend_id=runtime.backend_id,
            answer_available=runtime.capabilities.answer,
            judge_available=runtime.capabilities.judge,
            active_model=runtime.active_model,
            active_model_ready=runtime.active_model_ready,
            available_models=list(runtime.available_models),
            model_source="aiar-active" if runtime.active_model else source,
            capabilities=caps,
        )

    aiar_available = bool(runtime.connected) if runtime is not None else False

    ollama_reachable = False
    model_available: Optional[bool] = None
    error: Optional[str] = runtime.error_message if runtime is not None else None
    if runtime is not None and runtime.kind == "disconnected" and runtime.config_source == "none":
        try:
            from errorta_judge.aiar_adapter import AiarPipeline

            AiarPipeline()
            aiar_available = True
            error = None
        except Exception as exc:
            aiar_available = False
            error = str(exc)
    try:
        import httpx  # type: ignore

        host = os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434"
        try:
            r = httpx.get(f"{host.rstrip('/')}/api/tags", timeout=2.0)
            ollama_reachable = r.status_code == 200
            if ollama_reachable and judge_model:
                tags = r.json().get("models", []) or []
                names = {m.get("name") or m.get("model") for m in tags}
                # Models can be referenced with or without an explicit tag.
                model_available = judge_model in names or any(
                    isinstance(n, str) and n.split(":", 1)[0] == judge_model.split(":", 1)[0]
                    for n in names
                )
        except Exception as exc:
            error = f"ollama probe failed: {exc}"
    except Exception:  # pragma: no cover
        error = "httpx unavailable"

    return PreflightResponse(
        judge_model=judge_model,
        judge_model_source=source,
        aiar_available=aiar_available,
        ollama_reachable=ollama_reachable,
        model_available=model_available,
        error=error,
        runtime_kind=runtime.kind if runtime is not None else "local-aiar",
        display_name=runtime.display_name if runtime is not None else "This Mac",
        aiar_connected=aiar_available,
        backend_id=runtime.backend_id if runtime is not None else None,
        answer_available=aiar_available,
        judge_available=aiar_available,
        active_model=judge_model,
        active_model_ready=model_available,
        available_models=[],
        model_source=source,
        capabilities=(runtime.capabilities.to_dict() if runtime is not None else {}),
    )


@router.get("/model", response_model=ModelGetResponse)
def get_model() -> ModelGetResponse:
    model, source = _current_judge_model()
    return ModelGetResponse(judge_model=model, source=source)


@router.put("/model", response_model=ModelGetResponse)
def put_model(req: ModelPutRequest) -> ModelGetResponse:
    global _judge_model_override
    value = (req.judge_model or "").strip()
    _judge_model_override = value or None
    model, source = _current_judge_model()
    return ModelGetResponse(judge_model=model, source=source)


# ---------- F-WEDGE-DEEPEN-V1: replay endpoint ----------


class ReplayRequest(BaseModel):
    corpus: str
    dry_run: bool = False
    limit: Optional[int] = None


@router.post("/replay")
async def replay_corpus(req: ReplayRequest, request: Request):
    """Replay every non-accepted verdict for a corpus.

    Two response shapes, selected by ``Accept``:

    * ``text/event-stream`` — SSE frames in ``data: {json}\\n\\n`` form,
      one per replayed verdict.
    * default JSON — full list of :class:`ReplayResult` dicts.
    """
    corpus_name = (req.corpus or "").strip()
    if not corpus_name:
        raise HTTPException(status_code=400, detail="corpus is required")

    accept = (request.headers.get("accept") or "").lower()
    wants_sse = "text/event-stream" in accept
    pipeline = _active_pipeline()

    if wants_sse:
        async def _sse():
            async for result in _replay.replay_corpus_stream(
                corpus_name,
                pipeline,
                limit=req.limit,
                dry_run=req.dry_run,
            ):
                payload = json.dumps(result.to_dict(), ensure_ascii=False)
                yield f"data: {payload}\n\n"

        return StreamingResponse(_sse(), media_type="text/event-stream")

    results: list[dict[str, Any]] = []
    async for result in _replay.replay_corpus_stream(
        corpus_name,
        pipeline,
        limit=req.limit,
        dry_run=req.dry_run,
    ):
        results.append(result.to_dict())
    return results
