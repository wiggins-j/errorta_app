"""JSON / CSV / TSV extractors."""
from __future__ import annotations

import csv as _csv
import io
import json as _json
from pathlib import Path

from . import Chunk, ExtractError

_CSV_ROWS_PER_CHUNK = 50


def extract_json(path: Path) -> list[Chunk]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        data = _json.loads(raw)
    except Exception as e:
        raise ExtractError(f"could not parse JSON: {e}") from e

    chunks: list[Chunk] = []
    if isinstance(data, dict):
        for k, v in data.items():
            text = f"## {k}\n\n{_json.dumps(v, indent=2, ensure_ascii=False)}"
            chunks.append({"text": text, "meta": {"key": str(k), "source_type": "json"}})
    elif isinstance(data, list):
        # Chunk list items in groups of 20
        group = 20
        for i in range(0, len(data), group):
            text = _json.dumps(data[i : i + group], indent=2, ensure_ascii=False)
            chunks.append(
                {"text": text, "meta": {"offset": i, "source_type": "json"}}
            )
    else:
        chunks.append(
            {
                "text": _json.dumps(data, indent=2, ensure_ascii=False),
                "meta": {"source_type": "json"},
            }
        )
    if not chunks:
        raise ExtractError("JSON contains no extractable content")
    return chunks


def _extract_delimited(path: Path, delimiter: str, source_type: str) -> list[Chunk]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        raise ExtractError(f"could not read {source_type.upper()}: {e}") from e

    reader = _csv.reader(io.StringIO(raw), delimiter=delimiter)
    rows = list(reader)
    if not rows:
        raise ExtractError(f"{source_type.upper()} is empty")
    header = rows[0]
    body = rows[1:]
    chunks: list[Chunk] = []
    for i in range(0, len(body), _CSV_ROWS_PER_CHUNK):
        slab = body[i : i + _CSV_ROWS_PER_CHUNK]
        lines = [delimiter.join(header)]
        for row in slab:
            lines.append(delimiter.join(row))
        chunks.append(
            {
                "text": "\n".join(lines),
                "meta": {
                    "row_start": i + 1,
                    "row_end": i + len(slab),
                    "source_type": source_type,
                },
            }
        )
    if not chunks:
        # header-only file
        chunks.append(
            {"text": delimiter.join(header), "meta": {"source_type": source_type}}
        )
    return chunks


def extract_csv(path: Path) -> list[Chunk]:
    return _extract_delimited(path, ",", "csv")


def extract_tsv(path: Path) -> list[Chunk]:
    return _extract_delimited(path, "\t", "tsv")
