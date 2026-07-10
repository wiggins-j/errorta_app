"""F099 - PM working memory snapshot and AIAR mirror helpers.

The ledger remains the source of truth. This module derives a compact,
PM-scoped working-memory document from the ledger, stores it through the
project-memory layer, and optionally mirrors/retrieves it from the bound AIAR
corpus.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from . import paths as _paths
from .adapter import GroundingHit

SCHEMA_VERSION = "pm_working_memory.v1"
SOURCE_TYPE = "pm_working_memory"
MIRROR_SOURCE_PREFIX = "errorta://project"
_TASK_LIMIT = 12
_DECISION_LIMIT = 8
_EPISODE_LIMIT = 6
_INTERJECTION_LIMIT = 5
_TEXT_CAP = 500
_CONTENT_SOFT_CAP = max(4096, _paths.MAX_MEMORY_CONTENT_BYTES - 1024)


@dataclass(frozen=True)
class PMWorkingMemoryMirrorResult:
    status: str
    corpus_id: str | None = None
    record_id: str | None = None
    error: str | None = None
    mirrored_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PMWorkingMemoryCorpusEvidence:
    status: str
    hits: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()
    corpus_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "hits": list(self.hits),
            "warnings": list(self.warnings),
            "corpus_id": self.corpus_id,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cap(value: object, limit: int = _TEXT_CAP) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _task_view(task: Any) -> dict[str, Any]:
    return {
        "task_id": getattr(task, "task_id", ""),
        "title": _cap(getattr(task, "title", "")),
        "role": getattr(task, "role", ""),
        "state": getattr(task, "state", ""),
        "assignee_member_id": getattr(task, "assignee_member_id", None),
        "pr_id": getattr(task, "pr_id", None),
    }


def _decision_view(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "decision_id": str(decision.get("decision_id") or ""),
        "title": _cap(decision.get("title", "")),
        "choice": str(decision.get("choice") or ""),
        "rationale": _cap(decision.get("rationale", "")),
        "related_task_ids": list(decision.get("related_task_ids") or []),
        "at": decision.get("at"),
    }


def _episode_view(episode: dict[str, Any]) -> dict[str, Any]:
    return {
        "episode_id": str(episode.get("episode_id") or ""),
        "title": _cap(episode.get("title", "")),
        "summary": _cap(episode.get("summary", "")),
        "head": str(episode.get("head") or ""),
        "related_task_ids": list(episode.get("related_task_ids") or []),
        "at": episode.get("at"),
    }


def _interjection_view(raw: dict[str, Any], index: int) -> dict[str, Any]:
    summary = _cap(raw.get("message", ""))
    out: dict[str, Any] = {
        "ref": f"ledger:interjection:{index}",
        "summary": summary,
        "at": raw.get("at"),
    }
    pm_reply = raw.get("pm_reply")
    if isinstance(pm_reply, dict):
        out["pm_reply_kind"] = str(pm_reply.get("kind") or "")
    return out


def _binding_info(store: Any) -> tuple[str | None, int, str]:
    try:
        from .corpus_binding import load_binding
        binding = load_binding(store)
        return binding.corpus_id, int(binding.index_version or 0), binding.health_state
    except Exception:
        return None, 0, "unknown"


def _latest_test_head(store: Any) -> str:
    try:
        for run in reversed(store.list_test_runs()):
            if run.get("passed") and run.get("head"):
                return str(run.get("head") or "")[:12]
    except Exception:
        return ""
    return ""


def _safe_section(name: str, warnings: list[str], fn, default):
    try:
        return fn()
    except Exception:
        warnings.append(f"{name}_unavailable")
        return default


def _trim_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Keep the JSON under the memory content cap by trimming oldest optional
    records. Required project/focus fields are never removed."""
    def size() -> int:
        return len(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8"))

    optional_paths = (
        ("decisions",),
        ("integration", "recent_episodes"),
        ("interjections",),
        ("focus", "next_tasks"),
        ("focus", "doing_tasks"),
        ("focus", "blockers"),
    )
    while size() > _CONTENT_SOFT_CAP:
        changed = False
        for path in optional_paths:
            target: Any = snapshot
            for key in path[:-1]:
                target = target.get(key, {})
            key = path[-1]
            values = target.get(key) if isinstance(target, dict) else None
            if isinstance(values, list) and values:
                values.pop(0)
                changed = True
                break
        if not changed:
            break
    if size() > _CONTENT_SOFT_CAP:
        snapshot.setdefault("warnings", []).append("pm_working_memory_truncated")
    return snapshot


def build_pm_working_memory_snapshot(store: Any) -> dict[str, Any]:
    warnings: list[str] = []
    project = store.get_project()
    tasks = _safe_section("tasks", warnings, lambda: store.list_tasks(), [])
    doing = [t for t in tasks if getattr(t, "state", "") == "doing"][:_TASK_LIMIT]
    todo = [t for t in tasks if getattr(t, "state", "") == "todo"][:_TASK_LIMIT]
    blocked = [t for t in tasks if getattr(t, "state", "") == "blocked"][:_TASK_LIMIT]
    open_task_count = sum(
        1 for t in tasks if getattr(t, "state", "") in {"todo", "doing", "blocked"}
    )
    decisions = _safe_section(
        "decisions", warnings, lambda: store.list_decisions()[-_DECISION_LIMIT:], []
    )
    episodes = _safe_section(
        "episodes", warnings, lambda: store.list_episodes(limit=_EPISODE_LIMIT), []
    )
    interjections = _safe_section(
        "interjections",
        warnings,
        lambda: store.list_unconsumed_interjections()[-_INTERJECTION_LIMIT:],
        [],
    )
    pr_state = _safe_section("pr_state", warnings, store.pr_state_summary, {})
    corpus_id, memory_index_version, binding_health = _binding_info(store)
    latest_green_head = _latest_test_head(store) or str(
        (pr_state or {}).get("latest_green_head") or ""
    )

    snapshot: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "project_id": store.project_id,
        "generated_at": _now(),
        "project": {
            "north_star": _cap(project.north_star, 1200),
            "definition_of_done": _cap(project.definition_of_done, 1200),
            "status": project.status,
            "revision": int(project.revision),
        },
        "focus": {
            "current_focus": _cap(doing[0].title) if doing else None,
            "open_task_count": open_task_count,
            "doing_tasks": [_task_view(t) for t in doing],
            "next_tasks": [_task_view(t) for t in todo],
            "blockers": [_task_view(t) for t in blocked],
        },
        "integration": {
            "pr_state": pr_state,
            "recent_episodes": [_episode_view(ep) for ep in episodes],
            "latest_green_head": latest_green_head[:12],
        },
        "decisions": [_decision_view(d) for d in decisions],
        "interjections": [
            _interjection_view(raw, i)
            for i, raw in enumerate(interjections, start=1)
            if isinstance(raw, dict)
        ],
        "memory_refs": [],
        "freshness": {
            "ledger_updated_at": getattr(project, "updated_at", ""),
            "memory_index_version": memory_index_version,
            "bound_corpus_id": corpus_id,
            "bound_corpus_health": binding_health,
            "aiar_mirror_status": "not_attempted",
        },
        "warnings": warnings,
    }
    return _trim_snapshot(snapshot)


def summarize_pm_working_memory(snapshot: dict[str, Any]) -> str:
    focus = snapshot.get("focus") or {}
    integration = snapshot.get("integration") or {}
    pr_state = integration.get("pr_state") or {}
    counts = pr_state.get("counts") or {}
    current_focus = focus.get("current_focus") or "none"
    open_tasks = int(focus.get("open_task_count") or 0)
    blockers = len(focus.get("blockers") or [])
    open_prs = int(counts.get("open") or 0) + int(counts.get("changes_requested") or 0)
    latest_green = integration.get("latest_green_head") or pr_state.get("latest_green_head") or ""
    tail = f" Latest green head: {latest_green}." if latest_green else ""
    return (
        f"Focus: {current_focus}. Open tasks: {open_tasks}. "
        f"Open PRs: {open_prs}. Blockers: {blockers}.{tail}"
    )[:240]


def render_pm_working_memory_document(snapshot: dict[str, Any]) -> str:
    summary = summarize_pm_working_memory(snapshot)
    body = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2)
    return (
        "PM working memory for this Errorta Coding project.\n"
        f"{summary}\n\n"
        f"Schema: {SCHEMA_VERSION}\n"
        "JSON:\n"
        f"{body}\n"
    )


def mirror_source(project_id: str) -> str:
    return f"{MIRROR_SOURCE_PREFIX}/{project_id}/pm-working-memory"


def _metadata_for_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    project_id = str(snapshot.get("project_id") or "")
    return {
        "source": mirror_source(project_id),
        "title": "PM working memory",
        "category": SOURCE_TYPE,
        "project_id": project_id,
        "schema_version": SCHEMA_VERSION,
        "visibility": "pm",
        "generated_at": snapshot.get("generated_at"),
    }


def _load_local_snapshot(store: Any) -> dict[str, Any]:
    try:
        from .memory_store import MemoryQuery
        from .update_pipeline import memory_store_for

        memory = memory_store_for(store)
        rows = memory.query(
            MemoryQuery(
                authorities=("durable_truth",),
                source_type=SOURCE_TYPE,
                role="pm",
                limit=1,
            )
        )
        if rows:
            try:
                data = json.loads(rows[0].content)
                if isinstance(data, dict) and data.get("schema_version") == SCHEMA_VERSION:
                    return data
            except (TypeError, ValueError):
                pass
    except Exception:
        pass
    return build_pm_working_memory_snapshot(store)


def update_local_mirror_metadata(
    store: Any,
    *,
    status: str,
    record_id: str | None = None,
    error: str | None = None,
) -> None:
    try:
        from dataclasses import replace

        from .memory_store import MemoryQuery
        from .update_pipeline import memory_store_for

        memory = memory_store_for(store)
        rows = memory.query(
            MemoryQuery(
                authorities=("durable_truth",),
                source_type=SOURCE_TYPE,
                role="pm",
                limit=1,
            )
        )
        if not rows:
            return
        item = rows[0]
        metadata = dict(item.metadata)
        metadata.update({
            "aiar_mirror_status": status,
            "aiar_mirror_error": (error or "")[:300],
            "last_mirror_attempt_at": _now(),
        })
        if record_id is not None:
            metadata["aiar_record_id"] = record_id
        memory.put(replace(item, metadata=metadata))
    except Exception:
        return


def update_local_retrieval_metadata(
    store: Any,
    *,
    status: str,
    warnings: tuple[str, ...] = (),
) -> None:
    try:
        from dataclasses import replace

        from .memory_store import MemoryQuery
        from .update_pipeline import memory_store_for

        memory = memory_store_for(store)
        rows = memory.query(
            MemoryQuery(
                authorities=("durable_truth",),
                source_type=SOURCE_TYPE,
                role="pm",
                limit=1,
            )
        )
        if not rows:
            return
        item = rows[0]
        metadata = dict(item.metadata)
        metadata.update({
            "aiar_retrieval_status": status,
            "aiar_retrieval_warnings": list(warnings),
            "last_retrieval_attempt_at": _now(),
        })
        memory.put(replace(item, metadata=metadata))
    except Exception:
        return


def mirror_pm_working_memory_to_aiar(
    store: Any,
    *,
    adapter: Any | None = None,
    publish: bool = True,
) -> PMWorkingMemoryMirrorResult:
    try:
        from .corpus_binding import load_binding
        binding = load_binding(store)
    except Exception as exc:
        return PMWorkingMemoryMirrorResult(status="failed", error=str(exc)[:300])
    if not binding.corpus_id or binding.mode == "none":
        update_local_mirror_metadata(store, status="no_corpus")
        return PMWorkingMemoryMirrorResult(status="no_corpus")
    if adapter is None:
        try:
            from .retrieval import _adapter_for_project
            adapter = _adapter_for_project()
        except Exception:
            adapter = None
    ingest = getattr(adapter, "ingest_record", None)
    if not callable(ingest):
        update_local_mirror_metadata(store, status="unsupported")
        return PMWorkingMemoryMirrorResult(status="unsupported", corpus_id=binding.corpus_id)

    snapshot = _load_local_snapshot(store)
    content = render_pm_working_memory_document(snapshot)
    if _paths.content_has_secret(content):
        update_local_mirror_metadata(store, status="failed", error="secret-bearing content")
        return PMWorkingMemoryMirrorResult(
            status="failed", corpus_id=binding.corpus_id, error="secret-bearing content"
        )
    try:
        ref = ingest(
            corpus_id=binding.corpus_id,
            content=content,
            metadata=_metadata_for_snapshot(snapshot),
        )
    except Exception as exc:
        error = str(exc)[:300]
        update_local_mirror_metadata(store, status="failed", error=error)
        return PMWorkingMemoryMirrorResult(
            status="failed", corpus_id=binding.corpus_id, error=error
        )

    status = "mirrored"
    publish_fn = getattr(adapter, "publish", None)
    if publish and callable(publish_fn):
        try:
            publish_fn(binding.corpus_id)
        except Exception as exc:
            status = "ingested_unpublished"
            update_local_mirror_metadata(store, status=status, record_id=ref.record_id, error=str(exc)[:300])
            return PMWorkingMemoryMirrorResult(
                status=status,
                corpus_id=binding.corpus_id,
                record_id=ref.record_id,
                error=str(exc)[:300],
                mirrored_at=_now(),
            )

    update_local_mirror_metadata(store, status=status, record_id=ref.record_id)
    return PMWorkingMemoryMirrorResult(
        status=status,
        corpus_id=binding.corpus_id,
        record_id=ref.record_id,
        mirrored_at=_now(),
    )


def _hit_dict(hit: GroundingHit) -> dict[str, Any]:
    metadata = dict(hit.metadata or {})
    return {
        "ref": f"hit:{hit.corpus_id}:{hit.chunk_id}",
        "corpus_id": hit.corpus_id,
        "chunk_id": hit.chunk_id,
        "score": hit.score,
        "summary": _cap(hit.content, 240),
        "source_ids": [f"corpus:{hit.corpus_id}:{hit.chunk_id}"],
        "source": metadata.get("source"),
        "metadata": metadata,
        "why_included": "retrieved pm working memory",
    }


def _is_pm_memory_hit(hit: GroundingHit, project_id: str) -> bool:
    metadata = dict(hit.metadata or {})
    source = str(metadata.get("source") or "")
    category = str(metadata.get("category") or metadata.get("source_type") or "")
    schema = str(metadata.get("schema_version") or "")
    if schema == SCHEMA_VERSION:
        return True
    if category == SOURCE_TYPE:
        return True
    if source == mirror_source(project_id):
        return True
    text = (hit.content or "").lower()
    return SCHEMA_VERSION in text and "pm working memory" in text


def retrieve_pm_working_memory_from_aiar(
    store: Any,
    *,
    top_k: int = 3,
) -> PMWorkingMemoryCorpusEvidence:
    try:
        from .corpus_binding import load_binding
        binding = load_binding(store)
    except Exception:
        update_local_retrieval_metadata(store, status="unavailable")
        return PMWorkingMemoryCorpusEvidence(status="unavailable")
    if not binding.corpus_id or binding.mode == "none":
        update_local_retrieval_metadata(store, status="no_corpus")
        return PMWorkingMemoryCorpusEvidence(status="no_corpus")
    try:
        from .retrieval import retrieve_with_status
        hits, status = retrieve_with_status(
            store,
            query=(
                "pm working memory current focus blockers decisions next tasks "
                f"{SCHEMA_VERSION}"
            ),
            top_k=max(1, int(top_k)),
        )
    except Exception:
        update_local_retrieval_metadata(store, status="unavailable")
        return PMWorkingMemoryCorpusEvidence(status="unavailable", corpus_id=binding.corpus_id)
    if status != "ok":
        mapped = "miss" if status == "empty_query" else status
        update_local_retrieval_metadata(store, status=mapped)
        return PMWorkingMemoryCorpusEvidence(status=mapped, corpus_id=binding.corpus_id)
    matched = [_hit_dict(h) for h in hits if _is_pm_memory_hit(h, store.project_id)]
    if not matched:
        warnings = ("pm_working_memory_corpus_miss",)
        update_local_retrieval_metadata(store, status="miss", warnings=warnings)
        return PMWorkingMemoryCorpusEvidence(
            status="miss",
            corpus_id=binding.corpus_id,
            warnings=warnings,
        )
    update_local_retrieval_metadata(store, status="available")
    return PMWorkingMemoryCorpusEvidence(
        status="available",
        hits=tuple(matched[:top_k]),
        corpus_id=binding.corpus_id,
    )


def pm_working_memory_status(store: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "project_id": store.project_id,
        "status": "unavailable",
        "memory_ref": None,
        "corpus_id": None,
        "aiar_mirror_status": "unknown",
        "aiar_retrieval_status": "unknown",
        "last_generated_at": None,
        "last_mirrored_at": None,
        "warnings": [],
    }
    try:
        from .memory_store import MemoryQuery
        from .update_pipeline import memory_store_for
        memory = memory_store_for(store)
        rows = memory.query(
            MemoryQuery(
                authorities=("durable_truth",),
                source_type=SOURCE_TYPE,
                role="pm",
                limit=1,
            )
        )
    except Exception as exc:
        out["warnings"] = ["pm_working_memory_unavailable", str(exc)[:120]]
        return out
    if not rows:
        out["warnings"] = ["pm_working_memory_missing"]
        return out
    item = rows[0]
    metadata = dict(item.metadata or {})
    mirror_status = str(metadata.get("aiar_mirror_status") or "not_attempted")
    out.update({
        "status": "mirrored" if mirror_status == "mirrored" else "local",
        "memory_ref": f"mem:{item.memory_id}",
        "corpus_id": item.source_ref.corpus_id,
        "aiar_mirror_status": mirror_status,
        "aiar_retrieval_status": metadata.get("aiar_retrieval_status", "unknown"),
        "last_generated_at": metadata.get("generated_at") or item.freshness.indexed_at,
        "last_mirrored_at": metadata.get("last_mirror_attempt_at"),
        "warnings": [],
    })
    if mirror_status in {"failed", "unsupported", "no_corpus", "ingested_unpublished"}:
        out["warnings"].append(f"pm_working_memory_mirror_{mirror_status}")
        if mirror_status == "failed":
            out["status"] = "stale" if item.source_ref.corpus_id else "local"
    return out


__all__ = [
    "SCHEMA_VERSION",
    "SOURCE_TYPE",
    "PMWorkingMemoryMirrorResult",
    "PMWorkingMemoryCorpusEvidence",
    "build_pm_working_memory_snapshot",
    "summarize_pm_working_memory",
    "render_pm_working_memory_document",
    "mirror_pm_working_memory_to_aiar",
    "retrieve_pm_working_memory_from_aiar",
    "pm_working_memory_status",
    "update_local_mirror_metadata",
    "update_local_retrieval_metadata",
    "mirror_source",
]
