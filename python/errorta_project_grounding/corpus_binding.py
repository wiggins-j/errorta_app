"""Project-to-corpus binding persistence for F088."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from errorta_corpus import corpus_root, validate_corpus_name
from errorta_corpus.manifest import load_manifest, manifest_path
from errorta_council.coding.ledger import LedgerStore

from .memory_store import _now


class CorpusBindingError(Exception):
    """Raised for invalid project corpus binding operations."""


VALID_BINDING_MODES = ("none", "existing", "build_from_repo", "build_from_project")
VALID_HEALTH_STATES = ("missing", "ready", "indexing", "stale", "failed")


@dataclass(frozen=True)
class ProjectCorpusBinding:
    project_id: str
    mode: str = "none"
    corpus_id: str | None = None
    source_root: str | None = None
    index_version: int = 0
    last_refresh_at: str | None = None
    health_state: str = "missing"
    health_reason: str = "no corpus bound"
    bootstrap_job_id: str | None = None
    # "local" = corpus lives in the in-process AIAR store; "remote" = it lives on
    # a remote AIAR (watchdog), so health is derived from the remote instance,
    # not a local manifest.
    adapter_source: str = "local"
    created_at: str = ""
    updated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def is_remote(self) -> bool:
        return self.adapter_source == "remote"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ProjectCorpusBinding":
        return cls(
            project_id=str(raw.get("project_id") or ""),
            mode=str(raw.get("mode") or "none"),
            corpus_id=raw.get("corpus_id"),
            source_root=raw.get("source_root"),
            index_version=int(raw.get("index_version") or 0),
            last_refresh_at=raw.get("last_refresh_at"),
            health_state=str(raw.get("health_state") or "missing"),
            health_reason=str(raw.get("health_reason") or ""),
            bootstrap_job_id=raw.get("bootstrap_job_id"),
            adapter_source=str(raw.get("adapter_source") or "local"),
            created_at=str(raw.get("created_at") or ""),
            updated_at=str(raw.get("updated_at") or ""),
            metadata=dict(raw.get("metadata") or {}),
        )


def binding_path(store: LedgerStore) -> Path:
    return store.dir / "grounding" / "corpus-binding.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, sort_keys=True, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def default_binding(project_id: str) -> ProjectCorpusBinding:
    ts = _now()
    return ProjectCorpusBinding(
        project_id=project_id,
        created_at=ts,
        updated_at=ts,
    )


def load_binding(store: LedgerStore) -> ProjectCorpusBinding:
    path = binding_path(store)
    if not path.exists():
        return default_binding(store.project_id)
    try:
        raw = json.loads(path.read_text("utf-8"))
    except Exception:
        return replace(default_binding(store.project_id), health_state="failed", health_reason="binding unreadable")
    binding = ProjectCorpusBinding.from_dict(raw)
    if binding.project_id != store.project_id:
        return replace(default_binding(store.project_id), health_state="failed", health_reason="project mismatch")
    return binding


def save_binding(store: LedgerStore, binding: ProjectCorpusBinding) -> ProjectCorpusBinding:
    if binding.project_id != store.project_id:
        raise CorpusBindingError("binding project_id does not match store")
    if binding.mode not in VALID_BINDING_MODES:
        raise CorpusBindingError(f"invalid binding mode: {binding.mode}")
    if binding.health_state not in VALID_HEALTH_STATES:
        raise CorpusBindingError(f"invalid health state: {binding.health_state}")
    if binding.mode == "none":
        normalized = replace(
            binding,
            corpus_id=None,
            source_root=None,
            adapter_source="local",
            health_state="missing",
            health_reason="no corpus bound",
        )
    elif binding.mode == "existing":
        if not binding.corpus_id:
            raise CorpusBindingError("existing corpus binding requires corpus_id")
        validate_corpus_name(binding.corpus_id)
        candidate = replace(binding, source_root=None)
        # A remote binding's health is owned by the remote instance / bootstrap —
        # do NOT run the local-manifest probe (it would mark a healthy remote
        # corpus "missing"). Preserve the caller/bootstrap-set state on save.
        normalized = candidate if candidate.is_remote else binding_status(candidate)
    elif binding.mode == "build_from_project":
        # F088-03: the corpus is built from the project's OWN coding workspace
        # (the team's merged master tree), so there is no external source_root —
        # the source is the project itself.
        if not binding.corpus_id:
            raise CorpusBindingError("build_from_project binding requires corpus_id")
        validate_corpus_name(binding.corpus_id)
        normalized = binding if binding.is_remote else binding_status(binding)
    else:
        if not binding.corpus_id:
            raise CorpusBindingError("build_from_repo binding requires corpus_id")
        validate_corpus_name(binding.corpus_id)
        if not binding.source_root:
            raise CorpusBindingError("build_from_repo binding requires source_root")
        normalized = binding if binding.is_remote else binding_status(binding)
    if not normalized.created_at:
        normalized = replace(normalized, created_at=_now())
    normalized = replace(normalized, updated_at=_now())
    _atomic_write_json(binding_path(store), normalized.to_dict())
    return normalized


def binding_status(binding: ProjectCorpusBinding, *, adapter: Any = None) -> ProjectCorpusBinding:
    if binding.mode == "none" or not binding.corpus_id:
        return replace(binding, health_state="missing", health_reason="no corpus bound")
    try:
        validate_corpus_name(binding.corpus_id)
    except Exception as exc:
        return replace(binding, health_state="failed", health_reason=f"invalid corpus id: {exc}")
    if binding.is_remote:
        return _remote_binding_status(binding, adapter)
    corpus_dir = corpus_root() / binding.corpus_id
    if not corpus_dir.exists() or not manifest_path(binding.corpus_id).exists():
        return replace(binding, health_state="missing", health_reason="corpus manifest missing")
    files = load_manifest(binding.corpus_id)
    failed = sum(1 for f in files.values() if f.status == "failed")
    queued = sum(1 for f in files.values() if f.status in {"queued", "extracting", "chunking", "embedding"})
    ready = sum(1 for f in files.values() if f.status == "ready")
    if failed and not ready:
        return replace(binding, health_state="failed", health_reason=f"{failed} files failed")
    if queued:
        return replace(binding, health_state="indexing", health_reason=f"{queued} files still indexing")
    return replace(binding, health_state="ready", health_reason=f"{ready} ready files")


def _remote_binding_status(binding: ProjectCorpusBinding, adapter: Any) -> ProjectCorpusBinding:
    """Health for a corpus that lives on a remote AIAR. Derived from the remote
    instance, NOT a local manifest (a healthy watchdog corpus would otherwise be
    falsely 'missing'). Per the enablement plan:
      * indexing/failed are owned by the bootstrap (it alone sees ingest errors)
        — preserve them; instance_health cannot contradict them.
      * ready only when the remote instance reports published content.
      * missing only when the instance can't be found / the lookup fails.
    Fail-safe: if no remote adapter is reachable in this process, preserve the
    stored (bootstrap-set) state rather than falsely downgrading it."""
    if adapter is None:
        try:
            from .adapter import default_project_grounding_adapter
            adapter = default_project_grounding_adapter()
        except Exception:
            adapter = None
    health_fn = getattr(adapter, "instance_health", None)
    if not callable(health_fn):
        return binding  # can't probe the remote here — trust the stored state
    try:
        health = health_fn(binding.corpus_id)
    except Exception as exc:
        # A 404 / unknown_instance is the normal "not built yet" case for a
        # build_from_project corpus — don't surface a scary raw-HTTP error.
        text = str(exc).lower()
        if "404" in text or "unknown_instance" in text or "not found" in text:
            reason = (
                "corpus not built on the remote AIAR yet — use "
                "'Build a corpus from this project' once the team has merged code"
                if binding.mode == "build_from_project"
                else "corpus not found on the remote AIAR"
            )
            return replace(binding, health_state="missing", health_reason=reason)
        return replace(binding, health_state="missing",
                       health_reason=f"remote instance lookup failed: {exc}")
    # The bootstrap is the authority on in-progress / ingest-error states.
    if binding.health_state in ("indexing", "failed"):
        return binding
    try:
        chunk_count = int(health.get("chunk_count") or 0)
    except (TypeError, ValueError):
        chunk_count = 0
    published = health.get("published")
    if published is True and chunk_count > 0:
        return replace(binding, health_state="ready",
                       health_reason=f"remote instance ready ({chunk_count} chunks)")
    return replace(binding, health_state="missing",
                   health_reason="remote instance has no published content")
