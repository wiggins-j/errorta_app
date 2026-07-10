"""Runtime probes for the active AIAR connection."""

from __future__ import annotations

import importlib
from dataclasses import replace
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import AiarConnectionConfig
from .models import AiarCapabilities, AiarRuntime, disconnected
from .redaction import redact_text


def _display_name(config: AiarConnectionConfig) -> str:
    if config.display_name:
        return config.display_name
    if config.kind == "local-aiar":
        return "This Mac"
    if config.kind == "errorta-sidecar-remote":
        return "Remote Errorta sidecar"
    parsed = urlparse(config.base_url or "")
    host = parsed.hostname or "AIAR service"
    if host in {"127.0.0.1", "localhost", "::1"}:
        return f"AIAR service on {host}:{parsed.port}"
    return host


def _headers(token: str | None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get_json(client: httpx.Client, path: str) -> dict[str, Any] | None:
    try:
        resp = client.get(path)
    except (httpx.HTTPError, OSError):
        return None
    if not 200 <= resp.status_code < 300:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _models_from_meta(meta: dict[str, Any] | None) -> list[str]:
    if not isinstance(meta, dict):
        return []
    raw = meta.get("available_models") or meta.get("models") or []
    out: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                name = item.get("name") or item.get("model")
                if isinstance(name, str):
                    out.append(name)
    return out


def _corpus_count(health: dict[str, Any] | None, instances: dict[str, Any] | None) -> int | None:
    if isinstance(health, dict):
        rag = health.get("rag") if isinstance(health.get("rag"), dict) else {}
        for value in (rag.get("instance_count"), health.get("instance_count")):
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    if isinstance(instances, dict):
        raw = instances.get("instances")
        if isinstance(raw, list):
            return len(raw)
    return None


def _backend_id(
    *,
    base_url: str | None,
    health: dict[str, Any] | None,
    caps: dict[str, Any] | None,
) -> str | None:
    for data in (caps, health):
        if isinstance(data, dict):
            value = data.get("backend_id")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return base_url


def _capabilities(
    *,
    health: dict[str, Any] | None,
    meta: dict[str, Any] | None,
    caps: dict[str, Any] | None,
) -> AiarCapabilities:
    features = caps.get("features") if isinstance(caps, dict) else None
    features = features if isinstance(features, dict) else {}
    rag = (
        health.get("rag")
        if isinstance(health, dict) and isinstance(health.get("rag"), dict)
        else {}
    )

    active_ready = _active_model_ready(health, meta)
    generation = bool(
        features.get("generation")
        or features.get("answer")
        or features.get("services_prompt")
        or active_ready is True
    )
    judge = bool(
        features.get("judge")
        or features.get("eval")
        or features.get("judge_eval")
        or generation
    )
    pure_retrieve = bool(
        features.get("pure_retrieve")
        or (isinstance(health, dict) and health.get("pure_retrieve"))
    )
    remote_ingest = bool(
        features.get("remote_ingest")
        or (isinstance(health, dict) and health.get("remote_ingest"))
        or rag.get("remote_ingest")
    )
    corpus_list = bool(features.get("corpus_list") or rag or pure_retrieve)
    model_catalog = bool(features.get("model_catalog") or _models_from_meta(meta))
    return AiarCapabilities(
        answer=generation,
        judge=judge,
        model_catalog=model_catalog,
        model_active_status=active_ready is not None,
        model_set_active=bool(features.get("model_set_active")),
        ollama_pull=False,
        corpus_list=corpus_list,
        corpus_upload=remote_ingest,
        folder_watch=False,
        pure_retrieve=pure_retrieve,
        grounding_record=bool(features.get("grounding_record")),
        grounding_lookup=bool(features.get("grounding_lookup")),
        remote_ingest=remote_ingest,
    )


def _active_model(
    health: dict[str, Any] | None,
    meta: dict[str, Any] | None,
) -> str | None:
    for data in (meta, health):
        if isinstance(data, dict):
            value = data.get("active_model") or data.get("model")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _active_model_ready(
    health: dict[str, Any] | None,
    meta: dict[str, Any] | None,
) -> bool | None:
    for data in (meta, health):
        if isinstance(data, dict) and isinstance(data.get("active_model_ready"), bool):
            return bool(data.get("active_model_ready"))
    return None


def probe_aiar_service(config: AiarConnectionConfig, *, config_source: str) -> AiarRuntime:
    base = (config.base_url or "").rstrip("/")
    display = _display_name(config)
    if not base:
        return disconnected(
            display_name=display,
            config_source=config_source,
            error_code="missing_base_url",
            error_message="AIAR service URL is missing.",
        )
    try:
        with httpx.Client(
            timeout=httpx.Timeout(config.timeout_s),
            headers=_headers(config.token),
            verify=config.verify_tls,
        ) as client:
            health = _get_json(client, f"{base}/healthz")
            caps = _get_json(client, f"{base}/capabilities")
            meta = _get_json(client, f"{base}/services/meta")
            instances = _get_json(client, f"{base}/instances")
    except Exception as exc:  # pragma: no cover - defensive around client init
        return disconnected(
            display_name=display,
            config_source=config_source,
            error_code="probe_failed",
            error_message=redact_text(str(exc), config.token),
        )
    if health is None and caps is None and meta is None:
        return AiarRuntime(
            kind="aiar-service",
            display_name=display,
            base_url=base,
            token=config.token,
            verify_tls=config.verify_tls,
            timeout_s=config.timeout_s,
            connected=False,
            config_source=config_source,
            error_code="unreachable",
            error_message="AIAR service did not respond to health/meta probes.",
        )
    active_model = _active_model(health, meta)
    return AiarRuntime(
        kind="aiar-service",
        display_name=display,
        base_url=base,
        token=config.token,
        verify_tls=config.verify_tls,
        timeout_s=config.timeout_s,
        connected=True,
        backend_id=_backend_id(base_url=base, health=health, caps=caps),
        capabilities=_capabilities(health=health, meta=meta, caps=caps),
        active_model=active_model,
        active_model_ready=_active_model_ready(health, meta),
        available_models=_models_from_meta(meta),
        corpus_count=_corpus_count(health, instances),
        config_source=config_source,
        status_source="capabilities" if caps else ("services_meta" if meta else "healthz"),
    )


def probe_local_aiar(config_source: str = "local_probe") -> AiarRuntime:
    try:
        importlib.import_module("aiar")
    except Exception as exc:
        return AiarRuntime(
            kind="local-aiar",
            display_name="This Mac",
            connected=False,
            config_source=config_source,
            error_code="local_aiar_missing",
            error_message=str(exc),
        )
    return AiarRuntime(
        kind="local-aiar",
        display_name="This Mac",
        connected=True,
        backend_id="local",
        active_model=None,
        active_model_ready=None,
        capabilities=AiarCapabilities(
            answer=True,
            judge=True,
            model_catalog=True,
            model_active_status=False,
            ollama_pull=True,
            corpus_list=True,
            corpus_upload=True,
            folder_watch=True,
            pure_retrieve=True,
            grounding_record=True,
            grounding_lookup=True,
        ),
        config_source=config_source,
        status_source="local_import",
    )


def probe_remote_sidecar(config: AiarConnectionConfig, *, config_source: str) -> AiarRuntime:
    base = (config.base_url or "").rstrip("/")
    display = config.display_name or "Remote Errorta sidecar"
    if not base:
        return disconnected(
            display_name=display,
            config_source=config_source,
            error_code="missing_remote_sidecar_url",
            error_message="Remote sidecar tunnel URL is missing.",
        )
    try:
        with httpx.Client(
            timeout=httpx.Timeout(config.timeout_s),
            headers=_headers(config.token),
            verify=config.verify_tls,
        ) as client:
            aiar_status = _get_json(client, f"{base}/aiar/status")
            health = _get_json(client, f"{base}/healthz")
    except Exception as exc:  # pragma: no cover - defensive
        return AiarRuntime(
            kind="errorta-sidecar-remote",
            display_name=display,
            base_url=base,
            token=config.token,
            verify_tls=config.verify_tls,
            timeout_s=config.timeout_s,
            connected=False,
            config_source=config_source,
            error_code="remote_sidecar_probe_failed",
            error_message=redact_text(str(exc), config.token),
        )
    if isinstance(aiar_status, dict):
        caps_raw = (
            aiar_status.get("capabilities")
            if isinstance(aiar_status.get("capabilities"), dict)
            else {}
        )
        caps = AiarCapabilities(
            **{
                k: v
                for k, v in caps_raw.items()
                if k in AiarCapabilities.__dataclass_fields__
            }
        )
        return AiarRuntime(
            kind="errorta-sidecar-remote",
            display_name=str(aiar_status.get("display_name") or display),
            base_url=base,
            token=config.token,
            verify_tls=config.verify_tls,
            timeout_s=config.timeout_s,
            connected=bool(aiar_status.get("connected")),
            backend_id=aiar_status.get("backend_id"),
            capabilities=caps,
            active_model=aiar_status.get("active_model"),
            active_model_ready=aiar_status.get("active_model_ready"),
            available_models=list(aiar_status.get("available_models") or []),
            corpus_count=aiar_status.get("corpus_count"),
            config_source=config_source,
            status_source="remote_sidecar_aiar_status",
            error_code=aiar_status.get("error_code"),
            # Redact our own token from any upstream message before it can reach
            # the unauthenticated /aiar/status (parity with the exception path).
            error_message=redact_text(
                str(aiar_status.get("error_message") or ""), config.token
            )
            or None,
        )
    if not isinstance(health, dict):
        return AiarRuntime(
            kind="errorta-sidecar-remote",
            display_name=display,
            base_url=base,
            token=config.token,
            verify_tls=config.verify_tls,
            timeout_s=config.timeout_s,
            connected=False,
            config_source=config_source,
            error_code="remote_sidecar_unreachable",
            error_message="Remote Errorta sidecar did not respond.",
        )
    pin = health.get("aiar_pin") if isinstance(health.get("aiar_pin"), dict) else {}
    available = bool(pin.get("available") or health.get("aiar_available"))
    return AiarRuntime(
        kind="errorta-sidecar-remote",
        display_name=display,
        base_url=base,
        token=config.token,
        verify_tls=config.verify_tls,
        timeout_s=config.timeout_s,
        connected=True,
        backend_id=base,
        capabilities=AiarCapabilities(
            answer=available,
            judge=available,
            model_catalog=True,
            model_active_status=False,
            corpus_list=True,
            pure_retrieve=True,
            grounding_record=available,
            grounding_lookup=available,
        ),
        config_source=config_source,
        status_source="remote_sidecar_healthz",
    )


def with_error(runtime: AiarRuntime, *, code: str, message: str) -> AiarRuntime:
    return replace(runtime, connected=False, error_code=code, error_message=message)
