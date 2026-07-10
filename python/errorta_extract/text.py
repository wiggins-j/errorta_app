"""Plain text / Markdown extraction with chardet-based encoding detection."""
from __future__ import annotations

from pathlib import Path

from . import Chunk, ExtractError

_TARGET = 1200


def _read(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    encoding = "utf-8"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        import chardet  # type: ignore

        detected = chardet.detect(raw)
        encoding = detected.get("encoding") or "latin-1"
    except Exception:
        encoding = "latin-1"
    try:
        return raw.decode(encoding, errors="replace"), encoding
    except Exception:
        return raw.decode("latin-1", errors="replace"), "latin-1"


def extract(path: Path) -> list[Chunk]:
    try:
        text, encoding = _read(path)
    except Exception as e:
        raise ExtractError(f"could not read text file: {e}") from e

    text = text.strip()
    if not text:
        raise ExtractError("file is empty")

    # Chunk by paragraph, merging small ones to ~_TARGET chars.
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[Chunk] = []
    buf: list[str] = []
    buf_len = 0
    for p in paras:
        if buf_len + len(p) >= _TARGET and buf:
            chunks.append(
                {
                    "text": "\n\n".join(buf),
                    "meta": {"source_type": path.suffix.lstrip(".") or "txt", "encoding": encoding},
                }
            )
            buf = []
            buf_len = 0
        buf.append(p)
        buf_len += len(p) + 2
    if buf:
        chunks.append(
            {
                "text": "\n\n".join(buf),
                "meta": {"source_type": path.suffix.lstrip(".") or "txt", "encoding": encoding},
            }
        )
    return chunks
