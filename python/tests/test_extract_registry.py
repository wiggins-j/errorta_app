"""Tests for errorta_extract.registry."""
from __future__ import annotations

import pytest

from errorta_extract import ExtractError
from errorta_extract import pdf as _pdf
from errorta_extract import text as _text
from errorta_extract.registry import (
    SUPPORTED,
    format_label,
    get_extractor,
    supported_extensions,
    text_source_extensions,
)


def test_get_extractor_pdf() -> None:
    assert get_extractor(".pdf") is _pdf.extract


def test_get_extractor_txt() -> None:
    assert get_extractor(".txt") is _text.extract


def test_get_extractor_unknown_raises() -> None:
    with pytest.raises(ExtractError):
        get_extractor(".unknown")


def test_get_extractor_empty_raises() -> None:
    with pytest.raises(ExtractError):
        get_extractor("")


def test_get_extractor_case_insensitive() -> None:
    assert get_extractor(".PDF") is _pdf.extract
    assert get_extractor(".Md") is _text.extract


def test_source_extensions_are_extractable_without_widening_public_supported_list() -> None:
    assert ".py" in text_source_extensions()
    assert ".py" not in supported_extensions()
    assert get_extractor(".py") is _text.extract


def test_supported_extensions_sorted_and_complete() -> None:
    exts = supported_extensions()
    assert exts == sorted(exts)
    # Spot-check a handful of v0.1 formats.
    for required in (".pdf", ".txt", ".md", ".html", ".csv", ".json", ".docx"):
        assert required in exts
    assert set(exts) == set(SUPPORTED.keys())


def test_format_label_known() -> None:
    assert format_label(".pdf") == "PDF"
    assert format_label(".MD") == "Markdown"


def test_format_label_unknown_falls_back() -> None:
    assert format_label(".xyz") == "XYZ"
    assert format_label("") == "unknown"
