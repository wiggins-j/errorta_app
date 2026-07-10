"""Tests for errorta_extract.text (plain text / markdown)."""
from __future__ import annotations

from pathlib import Path

import pytest

from errorta_extract import ExtractError
from errorta_extract.text import extract


def test_extract_simple_text(tmp_path: Path) -> None:
    p = tmp_path / "doc.txt"
    p.write_text("hello world\n\nsecond paragraph here")
    chunks = extract(p)
    assert len(chunks) >= 1
    joined = "\n".join(c["text"] for c in chunks)
    assert "hello world" in joined
    assert "second paragraph" in joined
    for c in chunks:
        assert "source_type" in c["meta"]


def test_extract_empty_raises(tmp_path: Path) -> None:
    p = tmp_path / "empty.txt"
    p.write_text("   \n\n  ")
    with pytest.raises(ExtractError):
        extract(p)


def test_extract_chunks_long_text(tmp_path: Path) -> None:
    """Many paragraphs should produce multiple chunks at the ~1200 char target."""
    para = "x" * 400
    body = "\n\n".join([para] * 10)
    p = tmp_path / "long.md"
    p.write_text(body)
    chunks = extract(p)
    assert len(chunks) >= 2
    for c in chunks:
        assert c["meta"]["source_type"] == "md"


def test_extract_handles_non_utf8(tmp_path: Path) -> None:
    p = tmp_path / "weird.txt"
    # latin-1 bytes that aren't valid utf-8.
    p.write_bytes(b"caf\xe9 noir\n\npremi\xe8re ligne")
    chunks = extract(p)
    assert chunks
    encodings = {c["meta"].get("encoding") for c in chunks}
    assert encodings  # must record some encoding


def test_extract_markdown_meta_source_type(tmp_path: Path) -> None:
    p = tmp_path / "doc.md"
    p.write_text("# Heading\n\nSome body text here.")
    chunks = extract(p)
    assert chunks
    assert chunks[0]["meta"]["source_type"] == "md"


def test_extract_preserves_utf8_content(tmp_path: Path) -> None:
    content = "Café résumé — naïve façade.\n\n日本語のテキスト non-ASCII line."
    p = tmp_path / "utf8.txt"
    p.write_bytes(content.encode("utf-8"))

    chunks = extract(p)
    combined = "\n\n".join(c["text"] for c in chunks)

    assert "Café résumé" in combined
    assert "日本語のテキスト" in combined
    assert "non-ASCII line" in combined
    assert chunks[0]["meta"]["encoding"] == "utf-8"


def test_extract_crlf_and_lf_yield_same_chunks(tmp_path: Path) -> None:
    body = "Para one line A.\nPara one line B.\n\nPara two line A.\nPara two line B."
    lf = tmp_path / "lf.txt"
    crlf = tmp_path / "crlf.txt"
    lf.write_bytes(body.encode("utf-8"))
    crlf.write_bytes(body.replace("\n", "\r\n").encode("utf-8"))

    lf_chunks = extract(lf)
    crlf_chunks = extract(crlf)

    assert len(lf_chunks) == len(crlf_chunks)

    def _norm(chunks: list) -> str:
        return "\n\n".join(c["text"] for c in chunks).replace("\r\n", "\n").strip()

    assert _norm(lf_chunks) == _norm(crlf_chunks)


def test_extract_missing_file_raises_extracterror(tmp_path: Path) -> None:
    with pytest.raises(ExtractError):
        extract(tmp_path / "does_not_exist.txt")


def test_extract_chunks_under_size_threshold(tmp_path: Path) -> None:
    """Large input produces multiple chunks; each chunk stays near the ~1200-char target."""
    para = "Lorem ipsum dolor sit amet. " * 12  # ~336 chars
    body = "\n\n".join(f"Paragraph {i}. {para}" for i in range(30))
    p = tmp_path / "big.txt"
    p.write_text(body)

    chunks = extract(p)
    assert len(chunks) > 1
    # Splitter checks before appending the next para, so chunks won't be
    # dramatically larger than the 1200-char target.
    for c in chunks:
        assert len(c["text"]) < 1200 * 3
