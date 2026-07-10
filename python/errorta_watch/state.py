"""Persistent watch state — ``~/.errorta/corpora/{name}/watch.json``."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Literal

DeletionPolicy = Literal["remove", "mark_missing"]


@dataclass
class ManifestEntry:
    """One file the watcher knows about."""
    mtime: float
    size: int
    sha256: str = ""
    xxhash: str = ""
    file_id: str = ""
    chunk_ids: list[str] = field(default_factory=list)
    source_missing: bool = False


@dataclass
class WatchState:
    """Per-corpus watch state, serialized to ``watch.json``."""
    corpus: str
    watched_path: str
    started_at: str
    deletion_policy: DeletionPolicy = "remove"
    type_filter: list[str] = field(default_factory=list)
    extra_ignores: list[str] = field(default_factory=list)
    last_scan_at: str | None = None
    last_scan_ok: bool = True
    last_error: str | None = None
    last_heartbeat: str | None = None
    paused: bool = False
    manifest: dict[str, ManifestEntry] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        # asdict already serializes ManifestEntry as dicts.
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "WatchState":
        raw_manifest = d.get("manifest") or {}
        manifest: dict[str, ManifestEntry] = {}
        for path, entry in raw_manifest.items():
            if isinstance(entry, dict):
                manifest[path] = ManifestEntry(
                    mtime=float(entry.get("mtime") or 0.0),
                    size=int(entry.get("size") or 0),
                    sha256=str(entry.get("sha256") or ""),
                    xxhash=str(entry.get("xxhash") or ""),
                    file_id=str(entry.get("file_id") or ""),
                    chunk_ids=list(entry.get("chunk_ids") or []),
                    source_missing=bool(entry.get("source_missing") or False),
                )
        return cls(
            corpus=d["corpus"],
            watched_path=d["watched_path"],
            started_at=d["started_at"],
            deletion_policy=d.get("deletion_policy", "remove"),
            type_filter=list(d.get("type_filter") or []),
            extra_ignores=list(d.get("extra_ignores") or []),
            last_scan_at=d.get("last_scan_at"),
            last_scan_ok=bool(d.get("last_scan_ok", True)),
            last_error=d.get("last_error"),
            last_heartbeat=d.get("last_heartbeat"),
            paused=bool(d.get("paused", False)),
            manifest=manifest,
        )


def errorta_home() -> Path:
    """Compatibility shim. New code should import from ``errorta_app.paths``."""
    from errorta_app.paths import errorta_home as _h
    return _h()


def corpus_dir(corpus: str) -> Path:
    """Per-corpus directory under ``~/.errorta/corpora/{name}/``."""
    safe = corpus.strip().replace(os.sep, "_") or "default"
    d = errorta_home() / "corpora" / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_path(corpus: str) -> Path:
    return corpus_dir(corpus) / "watch.json"


def load_state(corpus: str) -> WatchState | None:
    p = state_path(corpus)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return WatchState.from_dict(d)
    except (KeyError, TypeError):
        return None


def save_state(state: WatchState) -> None:
    """Atomically persist the watch state for a corpus."""
    p = state_path(state.corpus)
    payload = json.dumps(state.to_dict(), indent=2, default=str)
    # Atomic write: tmp file then rename.
    fd, tmp = tempfile.mkstemp(prefix=".watch-", suffix=".json", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def list_persisted_corpora() -> list[str]:
    """Return corpora that have a ``watch.json`` on disk (used for restart)."""
    base = errorta_home() / "corpora"
    if not base.exists():
        return []
    out: list[str] = []
    for child in base.iterdir():
        if (child / "watch.json").exists():
            out.append(child.name)
    return out
