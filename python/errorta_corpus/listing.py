"""List corpora on disk for onboarding + judge feature."""
from __future__ import annotations

from dataclasses import dataclass

from . import corpus_root
from .manifest import load_manifest


@dataclass
class CorpusSummary:
    name: str
    file_count: int
    ready_count: int


def list_corpora() -> list[CorpusSummary]:
    """Return one summary per directory under ~/.errorta/corpora/."""
    out: list[CorpusSummary] = []
    root = corpus_root()
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        try:
            files = load_manifest(child.name)
        except Exception:
            continue
        ready = sum(1 for e in files.values() if e.status == "ready")
        out.append(
            CorpusSummary(
                name=child.name,
                file_count=len(files),
                ready_count=ready,
            )
        )
    return out
