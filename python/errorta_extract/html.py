"""HTML extraction via BeautifulSoup. Strips nav/footer/script/style."""
from __future__ import annotations

from pathlib import Path

from . import Chunk, ExtractError

_TARGET = 1200


def extract(path: Path) -> list[Chunk]:
    try:
        from bs4 import BeautifulSoup  # type: ignore
    except Exception as e:  # pragma: no cover
        raise ExtractError(f"beautifulsoup4 not available: {e}") from e

    try:
        raw = path.read_bytes()
    except Exception as e:
        raise ExtractError(f"could not read HTML: {e}") from e

    try:
        soup = BeautifulSoup(raw, "html.parser")
    except Exception as e:
        raise ExtractError(f"could not parse HTML: {e}") from e

    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()

    text = soup.get_text("\n").strip()
    if not text:
        raise ExtractError("HTML has no text content")

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    chunks: list[Chunk] = []
    buf: list[str] = []
    buf_len = 0
    for line in lines:
        if buf_len + len(line) >= _TARGET and buf:
            chunks.append({"text": "\n".join(buf), "meta": {"source_type": "html"}})
            buf = []
            buf_len = 0
        buf.append(line)
        buf_len += len(line) + 1
    if buf:
        chunks.append({"text": "\n".join(buf), "meta": {"source_type": "html"}})
    return chunks
