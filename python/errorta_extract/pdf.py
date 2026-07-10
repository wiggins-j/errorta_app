"""PDF text-layer extraction via PyMuPDF (fitz).

Rejects PDFs with no text layer (scanned). OCR fallback is F012, deferred.
"""
from __future__ import annotations

from pathlib import Path

from . import Chunk, ExtractError


def extract(path: Path) -> list[Chunk]:
    try:
        import fitz  # type: ignore
    except Exception as e:  # pragma: no cover
        raise ExtractError(f"PyMuPDF not available: {e}") from e

    try:
        doc = fitz.open(path)
    except Exception as e:
        raise ExtractError(f"could not open PDF: {e}") from e

    if doc.is_encrypted:
        # Password-protected; v0.1 surfaces the error, password UI in F004.
        doc.close()
        raise ExtractError("PDF is password-protected")

    chunks: list[Chunk] = []
    total_chars = 0
    for i, page in enumerate(doc):
        text = page.get_text("text") or ""
        text = text.strip()
        if not text:
            continue
        total_chars += len(text)
        chunks.append(
            {"text": text, "meta": {"page_number": i + 1, "source_type": "pdf"}}
        )
    doc.close()

    if total_chars == 0:
        raise ExtractError(
            "PDF has no text layer (likely scanned). OCR support is planned for F012."
        )
    return chunks
