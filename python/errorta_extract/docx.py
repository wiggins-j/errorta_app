"""DOCX extraction via python-docx. Merges short paragraphs into ~1k char chunks."""
from __future__ import annotations

from pathlib import Path

from . import Chunk, ExtractError

_TARGET = 1000


def extract(path: Path) -> list[Chunk]:
    try:
        import docx  # type: ignore
    except Exception as e:  # pragma: no cover
        raise ExtractError(f"python-docx not available: {e}") from e

    try:
        d = docx.Document(str(path))
    except Exception as e:
        raise ExtractError(f"could not open DOCX: {e}") from e

    chunks: list[Chunk] = []
    buf: list[str] = []
    buf_len = 0
    for p in d.paragraphs:
        t = (p.text or "").strip()
        if not t:
            continue
        buf.append(t)
        buf_len += len(t) + 1
        if buf_len >= _TARGET:
            chunks.append(
                {"text": "\n".join(buf), "meta": {"source_type": "docx"}}
            )
            buf = []
            buf_len = 0
    if buf:
        chunks.append({"text": "\n".join(buf), "meta": {"source_type": "docx"}})
    if not chunks:
        raise ExtractError("DOCX contains no text")
    return chunks
