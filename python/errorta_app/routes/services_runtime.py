"""Fail-closed helpers for the F009 Service API surface."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import HTTPException

from errorta_judge import schema_guard
from errorta_query.models import AnswerResult
from errorta_query.pipeline import StubPipeline, UnavailablePipeline

METADATA_MAX_BYTES = 2048
METADATA_AUDIT_ALLOWLIST = {"request_source", "request_id", "integration_name"}
RUNTIME_FAILURE_TAGS = {
    "aiar_pipeline_error": "answer_unavailable",
    "aiar_unavailable": "aiar_unavailable",
    "aiar_disconnected": "aiar_unavailable",
    "aiar_capability_missing": "service_pipeline_contract_mismatch",
    "judge_unavailable": "service_pipeline_contract_mismatch",
}


class ServiceApiError(RuntimeError):
    """Stable Service API error code plus HTTP status."""

    def __init__(
        self,
        code: str,
        *,
        status_code: int = 503,
        audit_fields: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.audit_fields = dict(audit_fields or {})


@dataclass(frozen=True)
class ServicePipelineCapabilities:
    runtime_kind: str
    answer_available: bool
    retrieval_available: bool
    supports_system: bool = False
    supports_top_k_answer: bool = False
    backend_id: str | None = None


@dataclass(frozen=True)
class ServiceCatalogResult:
    corpora: list[dict[str, Any]]
    source: Literal["local", "remote", "remote_unverified", "unavailable"]
    verified: bool
    backend_id: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class ServiceCorpusResolution:
    corpus: str
    catalog_verified: bool
    catalog_source: str
    catalog_item: dict[str, Any] | None


def to_http_exception(exc: ServiceApiError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.code)


def classify_service_pipeline(pipeline: Any) -> ServicePipelineCapabilities:
    """Return Service API capabilities or raise when the pipeline is not real."""

    inner = getattr(pipeline, "_inner", None)
    if inner is not None and pipeline.__class__.__name__ == "_RemoteRetrievalPipeline":
        inner_caps = classify_service_pipeline(inner)
        return ServicePipelineCapabilities(
            runtime_kind=inner_caps.runtime_kind,
            answer_available=inner_caps.answer_available,
            retrieval_available=True,
            supports_system=inner_caps.supports_system,
            supports_top_k_answer=inner_caps.supports_top_k_answer,
            backend_id=inner_caps.backend_id,
        )

    if isinstance(pipeline, StubPipeline):
        raise ServiceApiError("aiar_unavailable", audit_fields={"runtime_kind": "dev-stub"})
    if isinstance(pipeline, UnavailablePipeline):
        raise ServiceApiError(
            "aiar_unavailable",
            audit_fields={"runtime_kind": "unavailable", "reason": pipeline.tag},
        )

    cls_name = pipeline.__class__.__name__
    module = pipeline.__class__.__module__
    if cls_name == "AiarServicePipeline":
        return ServicePipelineCapabilities(
            runtime_kind="aiar-service",
            answer_available=True,
            retrieval_available=True,
            supports_top_k_answer=True,
            backend_id=getattr(pipeline, "base_url", None),
        )
    if cls_name == "RemoteHttpPipeline":
        return ServicePipelineCapabilities(
            runtime_kind="errorta-sidecar-remote",
            answer_available=True,
            retrieval_available=True,
            backend_id=getattr(pipeline, "base_url", None),
        )
    if cls_name == "AiarPipeline":
        return ServicePipelineCapabilities(
            runtime_kind="local-aiar",
            answer_available=True,
            retrieval_available=True,
        )

    return ServicePipelineCapabilities(
        runtime_kind=f"{module}.{cls_name}",
        answer_available=True,
        retrieval_available=True,
        supports_top_k_answer=True,
    )


def validate_service_answer(result: AnswerResult, *, judge: bool) -> AnswerResult:
    if not result.aiar:
        raise ServiceApiError("aiar_unavailable", audit_fields={"result_aiar": False})
    raw = getattr(result, "raw_verdict", None)
    if raw is None and result.verdict is not None:
        raw = result.verdict.to_dict()
    normalized = schema_guard.normalize_verdict(raw if isinstance(raw, dict) else None)
    tags = {str(item) for item in normalized.get("failure_tags") or []}
    for tag, code in RUNTIME_FAILURE_TAGS.items():
        if tag in tags and (judge or tag != "judge_unavailable"):
            raise ServiceApiError(code, audit_fields={"runtime_failure_tag": tag})
    return result


def validate_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    try:
        encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ServiceApiError("metadata_unsupported", status_code=400) from exc
    if len(encoded.encode("utf-8")) > METADATA_MAX_BYTES:
        raise ServiceApiError("metadata_too_large", status_code=400)

    for value in metadata.values():
        if not _metadata_value_supported(value):
            raise ServiceApiError("metadata_unsupported", status_code=400)

    audit_fields: dict[str, Any] = {"metadata_keys": sorted(str(k) for k in metadata)}
    for key in METADATA_AUDIT_ALLOWLIST:
        if key in metadata and _metadata_primitive(metadata[key]):
            audit_fields[f"metadata_{key}"] = _truncate_metadata_value(metadata[key])
    return audit_fields


def resolve_service_catalog(
    list_all_corpora_func: Any,
    *,
    runtime: ServicePipelineCapabilities,
) -> ServiceCatalogResult:
    try:
        raw = list_all_corpora_func()
    except HTTPException:
        raise
    except Exception as exc:
        raise ServiceApiError(
            "corpus_catalog_unavailable",
            audit_fields={"catalog_error_class": exc.__class__.__name__},
        ) from exc

    source = str(raw.get("source") or "local")
    verified = bool(raw.get("verified", source != "remote_unverified"))
    corpora = [dict(item) for item in raw.get("corpora") or [] if isinstance(item, dict)]
    backend_id = raw.get("backend_id")
    if backend_id is not None:
        backend_id = str(backend_id)

    if source == "remote" and not verified:
        source = "remote_unverified"
    if source == "remote_unverified":
        return ServiceCatalogResult(
            corpora=corpora,
            source="remote_unverified",
            verified=False,
            backend_id=backend_id or runtime.backend_id,
            error_code=str(raw.get("error_code") or "remote_catalog_unavailable"),
        )
    if source in {"local", "remote"}:
        return ServiceCatalogResult(
            corpora=corpora,
            source=source,  # type: ignore[arg-type]
            verified=True,
            backend_id=backend_id or (runtime.backend_id if source == "remote" else None),
        )
    return ServiceCatalogResult(
        corpora=[],
        source="unavailable",
        verified=False,
        backend_id=backend_id,
        error_code=str(raw.get("error_code") or "corpus_catalog_unavailable"),
    )


def resolve_service_corpus(
    *,
    corpus: str,
    token: dict[str, Any],
    catalog: ServiceCatalogResult,
    runtime: ServicePipelineCapabilities,
) -> ServiceCorpusResolution:
    names = {
        str(item.get("name") or "").strip(): item
        for item in catalog.corpora
        if str(item.get("name") or "").strip()
    }
    if catalog.source == "unavailable":
        raise ServiceApiError("corpus_catalog_unavailable")
    if catalog.verified:
        if corpus not in names:
            raise ServiceApiError("corpus_not_found", status_code=400)
        return ServiceCorpusResolution(
            corpus=corpus,
            catalog_verified=True,
            catalog_source=catalog.source,
            catalog_item=names[corpus],
        )

    allowed = {str(item) for item in token.get("corpora") or []}
    same_backend = (
        not catalog.backend_id
        or not runtime.backend_id
        or catalog.backend_id == runtime.backend_id
    )
    if catalog.source == "remote_unverified" and runtime.runtime_kind == "aiar-service":
        if corpus in allowed and same_backend:
            return ServiceCorpusResolution(
                corpus=corpus,
                catalog_verified=False,
                catalog_source="remote_unverified",
                catalog_item=None,
            )
    raise ServiceApiError("corpus_catalog_unavailable")


def filter_meta_corpora(
    *,
    token: dict[str, Any],
    catalog: ServiceCatalogResult,
    runtime: ServicePipelineCapabilities,
) -> tuple[list[dict[str, Any]], str, bool]:
    allowed = {str(item) for item in token.get("corpora") or []}
    if catalog.source == "unavailable":
        raise ServiceApiError("corpus_catalog_unavailable")
    if catalog.verified:
        return (
            [
                item
                for item in catalog.corpora
                if isinstance(item, dict) and str(item.get("name") or "") in allowed
            ],
            catalog.source,
            True,
        )
    same_backend = (
        not catalog.backend_id
        or not runtime.backend_id
        or catalog.backend_id == runtime.backend_id
    )
    if (
        catalog.source == "remote_unverified"
        and runtime.runtime_kind == "aiar-service"
        and same_backend
    ):
        return (
            [
                {
                    "name": corpus,
                    "status": "unknown",
                    "source": "remote_unverified",
                    "unit": "unknown",
                }
                for corpus in sorted(allowed)
            ],
            "remote_unverified",
            False,
        )
    raise ServiceApiError("corpus_catalog_unavailable")


def _metadata_value_supported(value: Any) -> bool:
    if _metadata_primitive(value):
        return True
    if isinstance(value, list):
        return all(_metadata_primitive(item) for item in value)
    return False


def _metadata_primitive(value: Any) -> bool:
    if value is None:
        return True
    return isinstance(value, (str, int, float, bool))


def _truncate_metadata_value(value: Any) -> Any:
    if isinstance(value, str):
        return value[:128]
    return value
