"""F095 — the unified corpus catalog endpoint.

One residency-aware ``GET /corpora`` consumed by the Knowledge -> Corpus panel,
the Council room editor, and the Coding Team grounding picker so all three show
the same corpora.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from errorta_app.corpus_catalog import list_all_corpora

router = APIRouter(tags=["corpus"])


@router.get("/corpora")
def get_corpora() -> dict[str, Any]:
    """Residency-aware corpus catalog: ``{corpora: [...], source}``.

    Remote AIAR configured -> its instances; otherwise local on-disk corpora
    (fail-closed under remote residency). See ``errorta_app.corpus_catalog``.
    """
    return list_all_corpora()
