"""Pydantic schema for brief front-matter.

A "brief" is a markdown file whose YAML front-matter declares the intent of a
corpus collection run: which sources to crawl, how much to fetch, the
sensitivity class, and the refresh cadence. The markdown body is a human-readable
description of the corpus intent (used by the collection agent as additional
context, not for validation).

v0.3 scope: only `sensitivity: Public` is allowed. Private/Restricted classes
are reserved for a later milestone once the policy story is locked.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class SourceSpec(BaseModel):
    """A single source declaration inside a brief.

    `name` identifies the collector plugin (e.g. "arxiv", "nasa_ntrs"). `config`
    is an arbitrary collector-specific dict — schema validation of the inner
    config is the collector's responsibility, not ours.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, description="Collector plugin name (e.g. 'arxiv').")
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="Collector-specific configuration. Opaque to the brief schema.",
    )


class BriefConfig(BaseModel):
    """Validated brief front-matter.

    Required fields define what a brief *must* declare to be runnable. Optional
    fields tune collection budgets and surface metadata to the UI.
    """

    model_config = ConfigDict(extra="forbid")

    # --- required ---
    project: str = Field(..., min_length=1, description="Human-readable project name.")
    corpus: str = Field(
        ...,
        min_length=1,
        description="Corpus slug. Lowercase letters, digits, and hyphens; must match the on-disk corpus directory.",
    )
    sources: list[SourceSpec] = Field(
        ..., min_length=1, description="At least one source must be declared."
    )
    sensitivity: Literal["Public"] = Field(
        ..., description="Sensitivity class. Only 'Public' is allowed in v0.3."
    )
    refresh: Literal["manual", "daily", "weekly", "monthly"] = Field(
        ..., description="Refresh cadence for the collection agent."
    )

    # --- optional ---
    per_doc_max_pages: int | None = Field(
        default=None, gt=0, description="Cap on pages per fetched document."
    )
    target_doc_count: int | None = Field(
        default=None, gt=0, description="Soft target for total documents collected."
    )
    target_total_pages: int | None = Field(
        default=None, gt=0, description="Soft target for total pages across the corpus."
    )
    description: str | None = Field(
        default=None, description="Short human-readable description of the corpus intent."
    )
    tags: list[str] = Field(
        default_factory=list, description="Free-form tags for UI/search filtering."
    )

    @field_validator("corpus")
    @classmethod
    def _validate_corpus_slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError(
                "corpus must be a slug: lowercase letters, digits, and hyphens "
                "(no leading/trailing/double hyphens)."
            )
        return v

    @classmethod
    def json_schema(cls) -> dict[str, Any]:
        """Return the JSON Schema dict for frontend linting.

        Pydantic v2 generates a schema with `$defs` for nested models (SourceSpec)
        and an explicit `required` array enumerating the required top-level fields.
        """
        return cls.model_json_schema()
