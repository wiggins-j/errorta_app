"""F088-06 — update pipeline: continuous sync, staleness/supersession, rebuild.

All writes funnel through the existing F087 per-project lock
(``errorta_council.coding.locks.lock_for_dir``) and use the idempotent
``memory_id``s from ``source_refs`` so a background refresh can never race or
duplicate a live coding run. Nothing here mutates the F087 ledger or the
worktree; the whole index lives under ``<project-ledger>/grounding/`` and can be
deleted with zero impact on coding runs.
"""
from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from errorta_council.coding.locks import lock_for_dir

from .ingest import MemoryIngestor
from .memory_store import MemoryQuery, ProjectMemoryStore
from .paths import content_has_secret
from .source_refs import is_sensitive_path, memory_id


def memory_store_for(ledger: Any) -> ProjectMemoryStore:
    """The project's memory store, bound to the SAME ledger directory."""
    return ProjectMemoryStore(ledger.project_id, root=ledger.dir.parent)


def _advance_binding_freshness(ledger: Any) -> None:
    """Bump the corpus binding's index_version / last_refresh_at so a later
    PM-briefing staleness check (F088-08) has a signal. No-op when no corpus is
    bound or the binding can't be loaded."""
    try:
        from .corpus_binding import load_binding, save_binding
        from .memory_store import _now
        b = load_binding(ledger)
        if b.mode == "none" or not b.corpus_id:
            return
        save_binding(ledger, replace(b, index_version=b.index_version + 1,
                                     last_refresh_at=_now()))
    except Exception:
        pass


def supersede_changed_files(memory: ProjectMemoryStore) -> int:
    """Retire stale durable code-chunk anchors: for each path, keep the newest
    chunk active and supersede older-head chunks (``valid_until`` +
    ``superseded_by``). Default queries are active-only, so stale chunks drop out
    of retrieval while history is preserved (no destructive delete)."""
    chunks = memory.query(MemoryQuery(authorities=("durable_truth",),
                                      source_type="code_chunk", limit=500))
    by_path: dict[str, list[Any]] = {}
    for item in chunks:
        by_path.setdefault(item.source_ref.path or "", []).append(item)
    superseded = 0
    for path, items in by_path.items():
        if not path or len(items) < 2:
            continue
        items.sort(key=lambda it: it.created_at)
        newest = items[-1]
        for stale in items[:-1]:
            memory.supersede(stale.memory_id, superseded_by=newest.memory_id)
            superseded += 1
    return superseded


def sync_from_ledger(ledger: Any, *, workspace: Any = None) -> dict[str, int]:
    """Idempotent full projection of the ledger into the memory store. Safe to
    call after every merge and at run end; re-running replaces rows in place."""
    with lock_for_dir(ledger.dir):
        ing = MemoryIngestor(ledger, memory=memory_store_for(ledger),
                             workspace=workspace)
        counts = {
            "pm_working_memory": ing.admit_pm_working_memory(),
            "pm_decisions": ing.admit_pm_decisions(),
            "durable_promotions": ing.promote_merged_prs(),
            "wip": ing.index_wip(),
            "claims": ing.admit_claims(),
        }
        counts["superseded"] = supersede_changed_files(ing.memory)
        _advance_binding_freshness(ledger)
        return counts


def rebuild_from_ledger(ledger: Any, *, workspace: Any = None) -> dict[str, int]:
    """Re-derive every ledger-backed record idempotently — recovers a corrupt or
    empty index from the ledger alone (no repo needed)."""
    return sync_from_ledger(ledger, workspace=workspace)


def rebuild_from_repo(ledger: Any, workspace: Any, *, adapter: Any = None) -> dict[str, Any]:
    """Re-ingest merged ``master`` code/doc files into the bound corpus via the
    F088-01/03 adapter (best-effort: degrades to memory-only anchors when AIAR is
    absent), then supersede prior chunks. Reuses the ledger sync for everything
    else."""
    from errorta_extract.registry import supported_extensions

    from .adapter import default_project_grounding_adapter
    from .bootstrap import CODE_EXTENSIONS
    from .corpus_binding import load_binding

    binding = load_binding(ledger)
    _corpus_modes = ("existing", "build_from_repo", "build_from_project")
    corpus_id = binding.corpus_id if binding.mode in _corpus_modes else None
    adapter = adapter or default_project_grounding_adapter()
    # Index source code too (the extractor registry only lists document types).
    supported = set(supported_extensions()) | set(CODE_EXTENSIONS)
    ingested = 0
    anchored = 0
    with lock_for_dir(ledger.dir):
        memory = memory_store_for(ledger)
        head = ""
        try:
            head = str(workspace.head() or "")
        except Exception:
            head = ""
        with tempfile.TemporaryDirectory(prefix="f088-rebuild-") as tmp:
            try:
                workspace.export(tmp)
            except Exception:
                return {"status": "export_failed", "ingested": 0, "anchored": 0}
            root = Path(tmp)
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                rel = path.relative_to(root).as_posix()
                if is_sensitive_path(rel):
                    continue
                try:
                    sample = path.read_text("utf-8", errors="ignore")[:4096]
                except OSError:
                    continue
                if content_has_secret(sample):
                    continue
                # memory anchor (provenance + supersession) — always recorded
                mem = memory_id("chunk", corpus_id or "nocorpus", rel, head)
                from .memory_store import MemoryItem, MemorySourceRef
                from .source_refs import freshness
                try:
                    memory.put(MemoryItem(
                        project_id=ledger.project_id, authority="durable_truth",
                        source_type="code_chunk",
                        source_ref=MemorySourceRef(path=rel, commit=head, head=head,
                                                   corpus_id=corpus_id),
                        content=f"master file {rel} @ {head[:12]}", memory_id=mem,
                        freshness=freshness(head),
                        metadata={"rebuild": True},
                    ))
                    anchored += 1
                except Exception:
                    pass
                # AIAR corpus ingest — only when a corpus is bound + adapter supports it
                if corpus_id and path.suffix.lower() in supported:
                    try:
                        adapter.ingest_file(corpus_id=corpus_id, path=path,
                                            metadata={"path": rel, "head": head})
                        ingested += 1
                    except Exception:
                        # No AIAR / unsupported op / ingest failure: the memory
                        # anchor above already preserves provenance.
                        pass
        superseded = supersede_changed_files(memory)
        try:
            from .pm_working_memory import mirror_pm_working_memory_to_aiar
            mirror = mirror_pm_working_memory_to_aiar(ledger, adapter=adapter)
            mirror_status = mirror.status
        except Exception:
            mirror_status = "skipped"
        _advance_binding_freshness(ledger)
    return {"status": "ok", "ingested": ingested, "anchored": anchored,
            "superseded": superseded, "head": head[:12],
            "pm_working_memory_mirror": mirror_status}


__all__ = [
    "memory_store_for", "sync_from_ledger", "supersede_changed_files",
    "rebuild_from_ledger", "rebuild_from_repo",
]
