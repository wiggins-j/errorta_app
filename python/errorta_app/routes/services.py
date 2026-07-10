"""F009-01 Service API surface for SDK callers."""

from __future__ import annotations

import datetime as _dt
import secrets
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from errorta_app import __version__ as ERRORTA_VERSION
from errorta_app.auth import audit
from errorta_app.auth.middleware import require_service_token
from errorta_app.corpus_catalog import list_all_corpora
from errorta_app.health.aiar_pin import check_aiar_pin
from errorta_judge import schema_guard
from errorta_judge.latency import stopwatch
from errorta_query.models import AnswerResult, QueryResult
from errorta_query.pipeline import default_pipeline

from . import judge as judge_routes
from .services_runtime import (
    ServiceApiError,
    ServicePipelineCapabilities,
    classify_service_pipeline,
    filter_meta_corpora,
    resolve_service_catalog,
    resolve_service_corpus,
    to_http_exception,
    validate_metadata,
    validate_service_answer,
)

SDK_CONTRACT_VERSION = "1.0"

router = APIRouter(prefix="/services", tags=["services"])


class PromptRequest(BaseModel):
    prompt: str
    corpus: str
    model: Optional[str] = None
    judge: bool = True
    system: Optional[str] = None
    top_k: int = Field(default=4, ge=1, le=20)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Citation(BaseModel):
    source_path: str
    chunk_text: str
    page_num: Optional[int] = None


class PromptResponse(BaseModel):
    id: str
    answer: str
    verdict: Optional[dict[str, Any]] = None
    citations: list[Citation]
    judge_model: Optional[str] = None
    latency_ms: float


class ServicesMetaResponse(BaseModel):
    errorta_version: str
    aiar_version: Optional[str] = None
    sdk_contract_version: str
    judge_available: bool
    default_model: Optional[str] = None
    default_judge_model: Optional[str] = None
    corpora: list[dict[str, Any]]
    corpus_source: str
    catalog_verified: bool


def _prompt_id() -> str:
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"prompt-{ts}-{secrets.token_hex(2)}"


def _citation_from_result(item: QueryResult) -> Citation | None:
    text = (item.content or "").strip()
    if not text:
        return None
    source_path = (
        item.source
        or str(item.metadata.get("source_path") or "")
        or item.title
        or item.citation_id
        or item.chunk_id
    )
    page_num = item.page_span[0] if item.page_span else None
    return Citation(source_path=source_path, chunk_text=text, page_num=page_num)


def _retrieve_citations(
    *,
    pipeline: Any,
    prompt: str,
    corpus: str,
    top_k: int,
) -> tuple[list[Citation], str, int]:
    # F009-02: the Service API REQUIRES strict retrieval — a pipeline that only
    # offers best-effort ``query`` (which swallows transport errors and returns
    # []) would let a retrieval-backend outage masquerade as a 200 "no_hits"
    # answer. Fail closed rather than feature-detect-and-degrade.
    query_strict = getattr(pipeline, "query_strict", None)
    if not callable(query_strict):
        raise ServiceApiError(
            "retrieval_unavailable",
            audit_fields={"retrieval_error_class": "pipeline_not_strict"},
        )
    try:
        hits = query_strict(prompt=prompt, corpus_ids=[corpus], top_k=top_k)
    except Exception as exc:
        raise ServiceApiError(
            "retrieval_unavailable",
            audit_fields={"retrieval_error_class": exc.__class__.__name__},
        ) from exc
    if not isinstance(hits, list):
        raise ServiceApiError("retrieval_unavailable")
    if not hits:
        return [], "no_hits", 0
    citations: list[Citation] = []
    for hit in hits:
        if not isinstance(hit, QueryResult):
            raise ServiceApiError("retrieval_unavailable")
        citation = _citation_from_result(hit)
        if citation is not None:
            citations.append(citation)
    if not citations:
        raise ServiceApiError("retrieval_unavailable")
    return citations, "ok", len(hits)


def _raw_verdict(result: AnswerResult) -> dict[str, Any] | None:
    raw = getattr(result, "raw_verdict", None)
    if raw is None and result.verdict is not None:
        raw = result.verdict.to_dict()
    return raw if isinstance(raw, dict) else None


def _verdict_payload(result: AnswerResult, *, judge: bool) -> dict[str, Any] | None:
    if not judge:
        return None
    normalized = schema_guard.normalize_verdict(_raw_verdict(result))
    rating = str(normalized.get("rating") or "unknown")
    reason = str(normalized.get("reason") or "")
    return {
        "rating": rating,
        "reason": reason,
        "failure_tags": list(normalized.get("failure_tags") or []),
        "confidence": normalized.get("confidence"),
        # Compatibility aliases for the parent F009 SDK response draft.
        "score": _rating_score(rating),
        "reasoning": reason,
        "groundedness": "high" if rating in {"good", "pass"} else "unknown",
        "judge_criteria": "default-v1",
    }


def _rating_score(rating: str) -> int:
    return {
        "pass": 4,
        "good": 4,
        "partial": 2,
        "bad": 1,
        "fail": 0,
        "unknown": 0,
    }.get(rating, 0)


def _answer_prompt(
    req: PromptRequest,
    *,
    token: dict[str, Any],
    runtime: ServicePipelineCapabilities,
) -> tuple[str, str, bool, str]:
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt_required")
    corpus = (req.corpus or "").strip()
    if not corpus:
        raise HTTPException(status_code=400, detail="corpus_required")
    if (req.system or "").strip() and not runtime.supports_system:
        raise HTTPException(status_code=400, detail="system_not_supported")
    if req.top_k != 4 and not runtime.supports_top_k_answer:
        raise HTTPException(status_code=503, detail="service_pipeline_contract_mismatch")
    catalog = resolve_service_catalog(list_all_corpora, runtime=runtime)
    resolution = resolve_service_corpus(
        corpus=corpus,
        token=token,
        catalog=catalog,
        runtime=runtime,
    )

    signature = judge_routes._prompt_signature(prompt)
    match, correction = judge_routes._lookup_grounding_match(prompt, signature)
    grounded_prompt = judge_routes._prompt_with_grounding_context(prompt, correction, match)
    return (
        grounded_prompt,
        corpus,
        resolution.catalog_verified,
        resolution.catalog_source,
    )


def _answer_kwargs(
    *,
    prompt: str,
    req: PromptRequest,
    corpus: str,
    runtime: ServicePipelineCapabilities,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "prompt": prompt,
        "corpus": corpus,
        "judge": req.judge,
        "reground": True,
        "model": req.model,
    }
    if runtime.supports_top_k_answer:
        kwargs["top_k"] = req.top_k
    if runtime.supports_system and req.system:
        kwargs["system"] = req.system
    return kwargs


@router.post("/prompt", response_model=PromptResponse)
def prompt(req: PromptRequest, request: Request) -> PromptResponse:
    token = require_service_token(request, corpus=req.corpus, required_scope="prompt")
    pipeline = default_pipeline()
    try:
        runtime = classify_service_pipeline(pipeline)
        metadata_audit = validate_metadata(req.metadata)
        answer_prompt, corpus, catalog_verified, catalog_source = _answer_prompt(
            req,
            token=token,
            runtime=runtime,
        )
    except ServiceApiError as exc:
        audit.record_event("prompt.failed", token_id=token.get("id"), **exc.audit_fields)
        raise to_http_exception(exc) from exc
    judge_model = req.model or judge_routes._current_judge_model()[0]

    with stopwatch() as sw:
        try:
            citations, retrieval_status, hit_count = _retrieve_citations(
                pipeline=pipeline,
                prompt=req.prompt,
                corpus=corpus,
                top_k=req.top_k,
            )
            result = pipeline.answer(
                **_answer_kwargs(
                    prompt=answer_prompt,
                    req=req,
                    corpus=corpus,
                    runtime=runtime,
                )
            )
            result = validate_service_answer(result, judge=req.judge)
        except ServiceApiError as exc:
            audit.record_event(
                "prompt.failed",
                token_id=token.get("id"),
                app_slug=token.get("app_slug"),
                corpus=corpus,
                catalog_verified=catalog_verified,
                catalog_source=catalog_source,
                **exc.audit_fields,
            )
            raise to_http_exception(exc) from exc
        except Exception as exc:
            audit.record_event(
                "prompt.failed",
                token_id=token.get("id"),
                app_slug=token.get("app_slug"),
                corpus=corpus,
                error_class=exc.__class__.__name__,
            )
            raise HTTPException(status_code=503, detail="answer_unavailable") from exc

    latency_ms = round(sw.elapsed_ms, 2)
    audit.record_event(
        "prompt",
        token_id=token.get("id"),
        app_slug=token.get("app_slug"),
        corpus=corpus,
        latency_ms=latency_ms,
        retrieval_status=retrieval_status,
        citation_count=len(citations),
        retrieval_hit_count=hit_count,
        catalog_verified=catalog_verified,
        catalog_source=catalog_source,
        **metadata_audit,
    )
    return PromptResponse(
        id=_prompt_id(),
        answer=result.answer or "",
        verdict=_verdict_payload(result, judge=req.judge),
        citations=citations,
        judge_model=judge_model if req.judge else None,
        latency_ms=latency_ms,
    )


@router.get("/meta", response_model=ServicesMetaResponse)
def meta(request: Request) -> ServicesMetaResponse:
    token = require_service_token(request, required_scope="meta")
    pipeline = default_pipeline()
    try:
        runtime_caps = classify_service_pipeline(pipeline)
        catalog = resolve_service_catalog(list_all_corpora, runtime=runtime_caps)
        corpora, corpus_source, catalog_verified = filter_meta_corpora(
            token=token,
            catalog=catalog,
            runtime=runtime_caps,
        )
    except ServiceApiError as exc:
        raise to_http_exception(exc) from exc
    runtime = None
    try:
        from errorta_aiar_connection import resolve_aiar_runtime

        runtime = resolve_aiar_runtime()
    except Exception:
        runtime = None
    aiar_pin = check_aiar_pin()
    judge_model = judge_routes._current_judge_model()[0]
    return ServicesMetaResponse(
        errorta_version=ERRORTA_VERSION,
        aiar_version=aiar_pin.get("version") if isinstance(aiar_pin, dict) else None,
        sdk_contract_version=SDK_CONTRACT_VERSION,
        judge_available=bool(
            runtime.connected and runtime.capabilities.judge
            if runtime is not None
            else aiar_pin.get("available")
        ),
        default_model=runtime.active_model if runtime is not None else None,
        default_judge_model=judge_model,
        corpora=corpora,
        corpus_source=corpus_source,
        catalog_verified=catalog_verified,
    )
