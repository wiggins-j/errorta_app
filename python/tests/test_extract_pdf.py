"""Tests for errorta_extract.pdf using a synthesized PyMuPDF document."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from errorta_extract import ExtractError


class _FakePage:
    def __init__(self, text: str) -> None:
        self._text = text

    def get_text(self, _kind: str = "text") -> str:
        return self._text


class _FakeDoc:
    def __init__(self, pages, encrypted: bool = False) -> None:
        self._pages = pages
        self.is_encrypted = encrypted
        self.closed = False

    def __iter__(self):
        return iter(self._pages)

    def close(self) -> None:
        self.closed = True


def _install_fake_fitz(monkeypatch: pytest.MonkeyPatch, doc: _FakeDoc) -> MagicMock:
    fake = MagicMock()
    fake.open = MagicMock(return_value=doc)
    monkeypatch.setitem(sys.modules, "fitz", fake)
    return fake


def test_extract_pdf_pages(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    doc = _FakeDoc([_FakePage("page one text"), _FakePage("page two text"), _FakePage("")])
    _install_fake_fitz(monkeypatch, doc)

    from errorta_extract import pdf as pdf_mod

    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    chunks = pdf_mod.extract(p)
    assert len(chunks) == 2
    assert chunks[0]["meta"]["page_number"] == 1
    assert chunks[1]["meta"]["page_number"] == 2
    assert all(c["meta"]["source_type"] == "pdf" for c in chunks)
    assert doc.closed


def test_extract_pdf_encrypted_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    doc = _FakeDoc([_FakePage("ignored")], encrypted=True)
    _install_fake_fitz(monkeypatch, doc)

    from errorta_extract import pdf as pdf_mod

    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    with pytest.raises(ExtractError) as exc:
        pdf_mod.extract(p)
    assert "password" in str(exc.value).lower() or "encrypted" in str(exc.value).lower()


def test_extract_pdf_no_text_layer_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    doc = _FakeDoc([_FakePage(""), _FakePage("   ")])
    _install_fake_fitz(monkeypatch, doc)

    from errorta_extract import pdf as pdf_mod

    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    with pytest.raises(ExtractError) as exc:
        pdf_mod.extract(p)
    assert "text layer" in str(exc.value).lower() or "OCR" in str(exc.value)


def test_extract_pdf_open_failure_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = MagicMock()
    fake.open = MagicMock(side_effect=RuntimeError("malformed PDF"))
    monkeypatch.setitem(sys.modules, "fitz", fake)

    from errorta_extract import pdf as pdf_mod

    p = tmp_path / "bad.pdf"
    p.write_bytes(b"not pdf")
    with pytest.raises(ExtractError) as exc:
        pdf_mod.extract(p)
    assert "could not open" in str(exc.value).lower()
