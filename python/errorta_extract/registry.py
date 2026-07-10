"""Map file extension → extractor function. Central source of truth for v0.1 formats."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from . import Chunk, ExtractError
from . import docx as _docx
from . import html as _html
from . import pdf as _pdf
from . import pptx as _pptx
from . import structured as _struct
from . import text as _text
from . import xlsx as _xlsx

Extractor = Callable[[Path], list[Chunk]]

SUPPORTED: dict[str, tuple[str, Extractor]] = {
    ".pdf": ("PDF", _pdf.extract),
    ".docx": ("Word", _docx.extract),
    ".xlsx": ("Excel", _xlsx.extract),
    ".pptx": ("PowerPoint", _pptx.extract),
    ".txt": ("plain text", _text.extract),
    ".md": ("Markdown", _text.extract),
    ".markdown": ("Markdown", _text.extract),
    ".html": ("HTML", _html.extract),
    ".htm": ("HTML", _html.extract),
    ".json": ("JSON", _struct.extract_json),
    ".csv": ("CSV", _struct.extract_csv),
    ".tsv": ("TSV", _struct.extract_tsv),
}

TEXT_SOURCE_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".kt", ".kts", ".scala", ".rb", ".php", ".swift",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".cs", ".m", ".mm",
    ".sh", ".bash", ".zsh", ".sql", ".css", ".scss", ".sass", ".less",
    ".vue", ".svelte", ".lua", ".r", ".jl", ".dart", ".ex", ".exs", ".erl",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".xml",
    ".proto", ".graphql", ".gql",
})


def supported_extensions() -> list[str]:
    return sorted(SUPPORTED.keys())


def text_source_extensions() -> list[str]:
    return sorted(TEXT_SOURCE_EXTENSIONS)


def format_label(ext: str) -> str:
    info = SUPPORTED.get(ext.lower())
    return info[0] if info else ext.lstrip(".").upper() or "unknown"


def get_extractor(ext: str) -> Extractor:
    normalized = ext.lower()
    info = SUPPORTED.get(normalized)
    if info is None and normalized in TEXT_SOURCE_EXTENSIONS:
        return _text.extract
    if info is None:
        raise ExtractError(f"unsupported format: {ext or 'unknown'}")
    return info[1]
