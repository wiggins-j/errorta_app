"""F036 citation registry and marker helpers."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .transforms.redaction import RedactionPipeline

_MARKER_RE = re.compile(r"\[c:([A-Za-z0-9_-]+)\]")


@dataclass(frozen=True)
class CitationEntry:
    citation_id: str
    corpus_id: str | None
    chunk_id: str | None
    content_sha256: str
    tokens: int
    title_hint: str


class CitationRegistry:
    def __init__(self, *, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._entries: dict[str, CitationEntry] = {}
        self._hash_to_id: dict[str, str] = {}
        self._load()

    def register(
        self,
        *,
        corpus_id: str | None,
        chunk_id: str | None,
        content_sha256: str,
        tokens: int,
        title_hint: str = "",
    ) -> CitationEntry:
        existing = self._hash_to_id.get(content_sha256)
        if existing:
            return self._entries[existing]
        cid = f"c{len(self._entries) + 1}"
        entry = CitationEntry(
            citation_id=cid,
            corpus_id=corpus_id,
            chunk_id=chunk_id,
            content_sha256=content_sha256,
            tokens=int(tokens or 0),
            title_hint=_redacted_hint(title_hint or chunk_id or corpus_id or content_sha256[:12]),
        )
        self._entries[cid] = entry
        self._hash_to_id[content_sha256] = cid
        self._write()
        return entry

    def get(self, citation_id: str) -> CitationEntry | None:
        return self._entries.get(citation_id)

    def list(self) -> list[CitationEntry]:
        return list(self._entries.values())

    def aliases_in_text(self, text: str) -> list[str]:
        return [m.group(1) for m in _MARKER_RE.finditer(text or "")]

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        for item in raw.get("entries") or []:
            try:
                entry = CitationEntry(**item)
            except TypeError:
                continue
            self._entries[entry.citation_id] = entry
            self._hash_to_id[entry.content_sha256] = entry.citation_id

    def _write(self) -> None:
        payload = {
            "format": "errorta.citation_registry.v1",
            "entries": [asdict(e) for e in self.list()],
        }
        tmp = self._path.with_suffix(f"{self._path.suffix}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        os.replace(tmp, self._path)


def citation_registry_path(run_id: str, *, council_root: Path) -> Path:
    return council_root / "runs" / run_id / "citations.json"


def citation_index_block(registry: CitationRegistry, text: str) -> dict[str, Any] | None:
    aliases = []
    for cid in registry.aliases_in_text(text):
        if cid not in aliases and registry.get(cid) is not None:
            aliases.append(cid)
    if not aliases:
        return None
    lines = ["Citations referenced in this discussion (not inlined):"]
    for cid in aliases:
        entry = registry.get(cid)
        if entry:
            lines.append(f"{cid}: \"{entry.title_hint}\" ({entry.tokens} tok)")
    content = "\n".join(lines)
    return {
        "class_": "citation_index",
        "content": content,
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
    }


def _redacted_hint(value: str) -> str:
    text = value[:120]
    import hashlib as _hashlib
    from .transforms.schema import SourceEnvelope

    env = SourceEnvelope(
        class_="metadata",
        corpus_id=None,
        chunk_id=None,
        citation_id=None,
        content=text,
        content_sha256=_hashlib.sha256(text.encode()).hexdigest(),
        tokens=None,
        sensitivity="may_contain_corpus",
    )
    try:
        redacted, _ = RedactionPipeline().redact_envelopes([env], destination_scope="remote")
        text = redacted[0].content
    except Exception:
        # Fail closed: expose only a sha256 prefix so raw corpus content
        # can't escape via the hint when the redaction pipeline errors.
        text = _hashlib.sha256(value.encode()).hexdigest()[:12]
    return text[:60]


__all__ = [
    "CitationEntry",
    "CitationRegistry",
    "citation_index_block",
    "citation_registry_path",
]
