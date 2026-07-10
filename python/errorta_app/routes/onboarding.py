"""First-run onboarding aggregation router.

Reports readiness across the four onboarding steps:
  * residency_ready — the user has explicitly selected where data lives.
  * hardware_ready  — a hardware report exists for the active residency target.
  * ollama_ready    — the configured Ollama host is reachable.
  * corpora_present — at least one corpus directory exists with files.
  * judge_ready     — derived: ollama + at least one corpus present.

The frontend uses ``recommended_next_step`` to drive the OnboardingFlow's
step indicator. The user can skip onboarding at any point; this endpoint
never blocks navigation, it only advises.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/onboarding", tags=["onboarding"])
_LOG = logging.getLogger("errorta_app.routes.onboarding")


class CorpusListItem(BaseModel):
    name: str
    file_count: int
    ready_count: int


class CorpusList(BaseModel):
    corpora: list[CorpusListItem]


@router.get("/corpora", response_model=CorpusList)
def list_all_corpora() -> CorpusList:
    """Aggregated corpus list for the onboarding/judge UI.

    Residency-remote stays proxied to the active sidecar's ``/onboarding/corpora``
    (unchanged F086 contract). Otherwise F095 delegates to the unified
    ``corpus_catalog`` so onboarding shows the SAME corpora as Knowledge ->
    Corpus, the Council room editor, and the Coding Team grounding picker —
    including a configured remote AIAR (example-host). Catalog entries are mapped
    down to this endpoint's name/file_count/ready_count shape.
    """
    from errorta_app.routes._residency_proxy import proxy_json_if_remote

    proxied = proxy_json_if_remote("GET", "/onboarding/corpora")
    if proxied is not None:
        return CorpusList.model_validate(proxied)

    # An onboarding probe must never 500 — degrade to an empty list on any
    # listing failure (F063 B1).
    try:
        from errorta_app.corpus_catalog import list_all_corpora as catalog_list

        catalog = catalog_list()
        items = [
            CorpusListItem(
                name=str(c.get("name") or ""),
                file_count=int(c.get("file_count") or 0),
                ready_count=int(c.get("ready_count") or 0),
            )
            for c in catalog.get("corpora") or []
        ]
        return CorpusList(corpora=items)
    except Exception as exc:  # noqa: BLE001 - degrade, never 500
        _LOG.warning("onboarding corpora listing failed: %s", exc)
        return CorpusList(corpora=[])


class OnboardingState(BaseModel):
    residency_ready: bool
    residency_mode: str
    hardware_ready: bool
    ollama_ready: bool
    corpora_present: bool
    judge_ready: bool
    recommended_next_step: str  # "residency" | "hardware" | "ollama" | "welcome" | "judge" | "done"
    corpora: list[str] = []
    ollama_error: Optional[str] = None


def _residency_state() -> tuple[bool, str]:
    try:
        from errorta_app.paths import data_residency_path
        from errorta_residency import config as residency_config

        p = data_residency_path()
        state = residency_config.load()
        return p.exists(), state.mode
    except Exception:
        return False, "local"


def _hardware_ready() -> bool:
    try:
        from errorta_app.routes import hardware as hardware_routes

        hardware_routes._scan_or_proxy("GET", "/hardware/report")
        return True
    except Exception:
        return False


def _ollama_state() -> tuple[bool, Optional[str]]:
    try:
        from errorta_app.routes import ollama as ollama_routes

        r = ollama_routes.health()
        return bool(r.reachable), r.error
    except Exception as exc:
        return False, repr(exc)


def _corpora() -> list[str]:
    # Residency-remote stays proxied; otherwise read through the unified F095
    # catalog so onboarding readiness counts the same corpora the pickers show
    # (remote AIAR / local).
    try:
        from errorta_app.routes._residency_proxy import proxy_json_if_remote

        proxied = proxy_json_if_remote("GET", "/onboarding/corpora")
        if proxied is not None:
            parsed = CorpusList.model_validate(proxied)
            return [c.name for c in parsed.corpora if c.file_count > 0]

        from errorta_app.corpus_catalog import list_all_corpora as catalog_list

        catalog = catalog_list()
        return [
            str(c.get("name") or "")
            for c in catalog.get("corpora") or []
            if int(c.get("file_count") or 0) > 0
        ]
    except Exception:
        return []


@router.get("/state", response_model=OnboardingState)
def state() -> OnboardingState:
    residency_ready, residency_mode = _residency_state()
    hardware_ready = _hardware_ready()
    ollama_ready, ollama_error = _ollama_state()
    corpora = _corpora()
    corpora_present = len(corpora) > 0
    judge_ready = ollama_ready and corpora_present

    if not residency_ready:
        nxt = "residency"
    elif not hardware_ready:
        nxt = "hardware"
    elif not ollama_ready:
        nxt = "ollama"
    elif not corpora_present:
        nxt = "welcome"
    elif not judge_ready:
        nxt = "judge"
    else:
        nxt = "done"

    return OnboardingState(
        residency_ready=residency_ready,
        residency_mode=residency_mode,
        hardware_ready=hardware_ready,
        ollama_ready=ollama_ready,
        corpora_present=corpora_present,
        judge_ready=judge_ready,
        recommended_next_step=nxt,
        corpora=corpora,
        ollama_error=ollama_error,
    )
