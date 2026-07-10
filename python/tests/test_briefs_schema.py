"""Tests for errorta_briefs.schema."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from errorta_briefs import BriefConfig, SourceSpec


def _minimal_valid_payload() -> dict:
    return {
        "project": "Aerospace Mini",
        "corpus": "aerospace-mini",
        "sensitivity": "Public",
        "refresh": "manual",
        "sources": [{"name": "arxiv", "config": {"categories": ["cs.RO"]}}],
    }


def test_minimal_valid_payload_parses() -> None:
    cfg = BriefConfig.model_validate(_minimal_valid_payload())
    assert cfg.project == "Aerospace Mini"
    assert cfg.corpus == "aerospace-mini"
    assert cfg.sensitivity == "Public"
    assert cfg.refresh == "manual"
    assert len(cfg.sources) == 1
    assert isinstance(cfg.sources[0], SourceSpec)
    assert cfg.sources[0].name == "arxiv"
    # Optional defaults
    assert cfg.per_doc_max_pages is None
    assert cfg.target_doc_count is None
    assert cfg.target_total_pages is None
    assert cfg.description is None
    assert cfg.tags == []


@pytest.mark.parametrize("bad_slug", ["Aerospace", "aero_space", "-aero", "aero-", "aero--space", ""])
def test_corpus_slug_rejected(bad_slug: str) -> None:
    payload = _minimal_valid_payload()
    payload["corpus"] = bad_slug
    with pytest.raises(ValidationError):
        BriefConfig.model_validate(payload)


def test_empty_sources_rejected() -> None:
    payload = _minimal_valid_payload()
    payload["sources"] = []
    with pytest.raises(ValidationError):
        BriefConfig.model_validate(payload)


def test_sensitivity_only_public_in_v03() -> None:
    payload = _minimal_valid_payload()
    payload["sensitivity"] = "Private"
    with pytest.raises(ValidationError):
        BriefConfig.model_validate(payload)


def test_refresh_enum_enforced() -> None:
    payload = _minimal_valid_payload()
    payload["refresh"] = "hourly"
    with pytest.raises(ValidationError):
        BriefConfig.model_validate(payload)


def test_per_doc_max_pages_must_be_positive() -> None:
    payload = _minimal_valid_payload()
    payload["per_doc_max_pages"] = 0
    with pytest.raises(ValidationError):
        BriefConfig.model_validate(payload)


def test_extra_fields_forbidden() -> None:
    payload = _minimal_valid_payload()
    payload["random_extra"] = True
    with pytest.raises(ValidationError):
        BriefConfig.model_validate(payload)


def test_json_schema_has_defs_and_required() -> None:
    schema = BriefConfig.json_schema()
    assert isinstance(schema, dict)
    assert "$defs" in schema
    # SourceSpec is a nested model and should appear under $defs.
    assert "SourceSpec" in schema["$defs"]
    required = set(schema.get("required", []))
    assert {"project", "corpus", "sources", "sensitivity", "refresh"}.issubset(required)
    # Optional fields should NOT be required but must be present in properties.
    props = schema["properties"]
    for opt in (
        "per_doc_max_pages",
        "target_doc_count",
        "target_total_pages",
        "description",
        "tags",
    ):
        assert opt in props, f"missing optional field {opt} in schema properties"
    # SourceSpec required fields
    src_required = set(schema["$defs"]["SourceSpec"].get("required", []))
    assert "name" in src_required
