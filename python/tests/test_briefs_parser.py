"""Tests for errorta_briefs.parser."""
from __future__ import annotations

from pathlib import Path

import pytest

from errorta_briefs import BriefConfig, BriefParseError, parse_brief_markdown

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_BRIEF = REPO_ROOT / "docs" / "examples" / "briefs" / "aerospace-mini.md"


def _brief(front_matter: str, body: str = "Body text.\n") -> str:
    return f"---\n{front_matter}\n---\n\n{body}"


def test_parse_example_brief() -> None:
    text = EXAMPLE_BRIEF.read_text(encoding="utf-8")
    config, body = parse_brief_markdown(text)
    assert isinstance(config, BriefConfig)
    assert config.project == "Aerospace Mini"
    assert config.corpus == "aerospace-mini"
    assert config.sensitivity == "Public"
    assert config.refresh == "manual"
    assert config.per_doc_max_pages == 40
    assert config.target_doc_count == 25
    assert config.target_total_pages == 800
    assert "aerospace" in config.tags
    assert len(config.sources) == 1
    assert config.sources[0].name == "arxiv"
    assert "cs.RO" in config.sources[0].config["categories"]
    assert "Aerospace Mini" in body
    assert "F008" in body


def test_reject_missing_required_project() -> None:
    text = _brief(
        "corpus: aerospace-mini\n"
        "sensitivity: Public\n"
        "refresh: manual\n"
        "sources:\n"
        "  - name: arxiv\n"
        "    config: {}\n"
    )
    with pytest.raises(BriefParseError) as exc_info:
        parse_brief_markdown(text)
    err = exc_info.value
    assert err.errors, "expected field-level diagnostics"
    locs = [tuple(e["loc"]) for e in err.errors]
    assert ("project",) in locs


def test_reject_empty_sources() -> None:
    text = _brief(
        "project: X\n"
        "corpus: x\n"
        "sensitivity: Public\n"
        "refresh: manual\n"
        "sources: []\n"
    )
    with pytest.raises(BriefParseError) as exc_info:
        parse_brief_markdown(text)
    assert any(e["loc"][0] == "sources" for e in exc_info.value.errors)


def test_reject_sensitivity_private() -> None:
    text = _brief(
        "project: X\n"
        "corpus: x\n"
        "sensitivity: Private\n"
        "refresh: manual\n"
        "sources:\n"
        "  - name: arxiv\n"
        "    config: {}\n"
    )
    with pytest.raises(BriefParseError) as exc_info:
        parse_brief_markdown(text)
    assert any(e["loc"][0] == "sensitivity" for e in exc_info.value.errors)


def test_reject_invalid_refresh() -> None:
    text = _brief(
        "project: X\n"
        "corpus: x\n"
        "sensitivity: Public\n"
        "refresh: hourly\n"
        "sources:\n"
        "  - name: arxiv\n"
        "    config: {}\n"
    )
    with pytest.raises(BriefParseError) as exc_info:
        parse_brief_markdown(text)
    assert any(e["loc"][0] == "refresh" for e in exc_info.value.errors)


def test_reject_malformed_yaml() -> None:
    # Unclosed bracket inside the front-matter.
    text = _brief("project: X\ncorpus: x\nsources: [unterminated\n")
    with pytest.raises(BriefParseError) as exc_info:
        parse_brief_markdown(text)
    assert any(e["type"] == "front_matter.yaml_error" for e in exc_info.value.errors)


def test_reject_missing_opening_delimiter() -> None:
    text = "project: X\ncorpus: x\n---\n\nBody.\n"
    with pytest.raises(BriefParseError) as exc_info:
        parse_brief_markdown(text)
    assert any(e["type"] == "front_matter.missing_open" for e in exc_info.value.errors)


def test_reject_missing_closing_delimiter() -> None:
    text = "---\nproject: X\ncorpus: x\nsensitivity: Public\nrefresh: manual\n\nBody.\n"
    with pytest.raises(BriefParseError) as exc_info:
        parse_brief_markdown(text)
    assert any(e["type"] == "front_matter.missing_close" for e in exc_info.value.errors)


def test_reject_empty_front_matter() -> None:
    text = "---\n---\n\nBody.\n"
    with pytest.raises(BriefParseError) as exc_info:
        parse_brief_markdown(text)
    assert any(e["type"] == "front_matter.empty" for e in exc_info.value.errors)


def test_reject_non_mapping_front_matter() -> None:
    text = "---\n- just\n- a\n- list\n---\n\nBody.\n"
    with pytest.raises(BriefParseError) as exc_info:
        parse_brief_markdown(text)
    assert any(e["type"] == "front_matter.not_mapping" for e in exc_info.value.errors)


def test_body_preserved_separately() -> None:
    text = _brief(
        "project: X\n"
        "corpus: x\n"
        "sensitivity: Public\n"
        "refresh: manual\n"
        "sources:\n"
        "  - name: arxiv\n"
        "    config: {}\n",
        body="# Heading\n\nParagraph one.\n",
    )
    _, body = parse_brief_markdown(text)
    assert body.startswith("# Heading")
    assert "Paragraph one." in body
