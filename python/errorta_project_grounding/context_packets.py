"""F088-07 — role-scoped context packets.

Build a typed, token-bounded ``project_context_packet.v1`` to inject into a
coding member's prompt: durable project truth first, then relevant WIP, with
``claim`` excluded, role visibility honored, and every item carrying a memory
ref + source provenance. No free-form "AI language" — compact JSON + ``mem:``
refs only. This module owns ALL ``ProjectMemoryStore`` queries; the runner only
stringifies the returned packet.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from . import paths as _paths
from .memory_store import (
    _AUTHORITY_RANK,
    MemoryItem,
    MemoryQuery,
    ProjectMemoryStore,
)

# Per-role token budgets (spec §"Token budgets").
ROLE_TOKEN_BUDGET = {"pm": 900, "dev": 700, "reviewer": 800, "tester": 600}
# Per-role query limits (durable, wip).
_ROLE_LIMITS = {"pm": (40, 20), "dev": (20, 12), "reviewer": (20, 12), "tester": (15, 10)}
_SUMMARY_CAP = 240
_PM_MEMORY_SOURCE_TYPE = "pm_working_memory"


def role_token_budget(role: str) -> int:
    return ROLE_TOKEN_BUDGET.get(role, 700)


# F104 S2: implementer-facing corpus evidence may carry a multi-line rule block
# (e.g. a full tier table) that the 240-char memory-summary cap would truncate
# mid-list, so it gets a larger per-hit cap.
_CORPUS_SUMMARY_CAP = 700
# Roles whose packet should carry a TASK-KEYED corpus retrieval (the
# implementer + the reviewer that must check the diff against the spec). The PM
# already receives corpus evidence via the boot briefing + pm_working_memory.
_CORPUS_RETRIEVAL_ROLES = ("dev", "reviewer", "tester")


def _corpus_is_bound(store: Any) -> bool:
    """Whether the project has a non-``none`` corpus binding (F104 S2: role
    grounding must not depend on a LOCAL memory index when a corpus is bound)."""
    try:
        from .corpus_binding import load_binding
        b = load_binding(store)
        return bool(b.corpus_id) and b.mode != "none"
    except Exception:
        return False


def _role_corpus_query(store: Any, task: Any) -> str:
    """A task-keyed retrieval query for an implementer/reviewer packet — the
    task's own title/detail (so a 'implement the tier discounts' task pulls the
    tiers chunk), with the project goal as a floor."""
    parts: list[str] = []
    for attr in ("title", "detail"):
        val = getattr(task, attr, None)
        if val:
            parts.append(str(val))
    try:
        proj = store.get_project()
        parts.append(f"{proj.north_star} {proj.definition_of_done}")
    except Exception:
        pass
    return " ".join(p.strip() for p in parts if p and p.strip()).strip()


def _memory_store_if_present(store: Any) -> ProjectMemoryStore | None:
    """Return the project's memory store, or None if this project has NO
    grounding index yet (so the runner emits a byte-identical prompt). The
    sqlite file exists iff a grounding sync has run for the project."""
    try:
        db = store.dir / "grounding" / "memory.sqlite3"
        if not db.exists():
            return None
        return ProjectMemoryStore(store.project_id, root=store.dir.parent)
    except Exception:
        return None


def _est_tokens(obj: Any) -> int:
    return max(1, len(json.dumps(obj, sort_keys=True)) // 4)


def _summary_for(item: MemoryItem) -> str | None:
    """Injection summary: prefer the item's own summary, else a bounded extract
    of content (meaning-preserving, not model-generated). Drop secret-bearing
    text entirely (defense-in-depth over admission screening)."""
    text = (item.summary or item.content or "").strip()
    if not text:
        return None
    if _paths.content_has_secret(text):
        return None
    return text[:_SUMMARY_CAP]


def ensure_pm_working_memory(store: Any) -> MemoryItem | None:
    """Refresh the PM working-memory row on demand and return it.

    This is intentionally PM-facing glue: failures degrade to no row, not a
    broken planning turn.
    """
    try:
        from .ingest import MemoryIngestor
        from .memory_store import MemoryQuery

        mem = ProjectMemoryStore(store.project_id, root=store.dir.parent)
        MemoryIngestor(store, memory=mem).admit_pm_working_memory()
        rows = mem.query(
            MemoryQuery(
                authorities=("durable_truth",),
                source_type=_PM_MEMORY_SOURCE_TYPE,
                role="pm",
                limit=1,
            )
        )
        return rows[0] if rows else None
    except Exception:
        return None


def _conflict_key(item: MemoryItem) -> str | None:
    cg = item.metadata.get("conflict_group")
    if cg:
        return str(cg)
    path = item.source_ref.path
    symbol = item.metadata.get("symbol")
    if path and symbol:
        return f"{path}:{symbol}"
    return None


def _packet_item(item: MemoryItem, *, open_overlay: bool, why: str) -> dict[str, Any]:
    fr = (item.freshness.to_dict() if item.freshness else None)
    out: dict[str, Any] = {
        "ref": f"mem:{item.memory_id}",
        "authority": item.authority,
        "source_type": item.source_type,
        "source_ref": item.source_ref.to_dict(),
        "summary": _summary_for(item),
        "path": item.source_ref.path,
        "symbol": item.metadata.get("symbol"),
        "freshness": fr,
        "source_ids": list(item.source_ids),
        "why_included": why,
        "conflict_group": _conflict_key(item),
    }
    if open_overlay:
        out["open_overlay"] = True
    return out


def _query_for_role(mem: ProjectMemoryStore, role: str, task: Any, pr: Any) -> list[MemoryItem]:
    d_limit, w_limit = _ROLE_LIMITS.get(role, (20, 12))
    # claim is excluded by the explicit authorities allowlist; role applies
    # MemoryVisibility.visible_to(role) inside the store.
    durable = mem.query(MemoryQuery(authorities=("durable_truth",), role=role, limit=d_limit))
    wip = mem.query(MemoryQuery(authorities=("wip",), role=role, limit=w_limit))
    return list(durable) + list(wip)


def _sorted(items: list[MemoryItem]) -> list[MemoryItem]:
    # Deterministic order (spec): authority_rank, path, symbol, source_type,
    # created_at desc, memory_id. Build with stable sorts, least-significant first.
    items = sorted(items, key=lambda it: it.memory_id)
    items = sorted(items, key=lambda it: it.created_at, reverse=True)
    items = sorted(items, key=lambda it: (
        _AUTHORITY_RANK.get(it.authority, 99),
        it.source_ref.path or "",
        str(it.metadata.get("symbol") or ""),
        it.source_type or "",
    ))
    return items


def _trim(items: list[MemoryItem], budget: int) -> tuple[list[MemoryItem], int, bool]:
    """Keep durable first (always >=1), fill remaining with WIP. Returns
    (kept, dropped_count, durable_truncated). Trims WIP before durable."""
    durable = [i for i in items if i.authority == "durable_truth"]
    wip = [i for i in items if i.authority != "durable_truth"]
    durable.sort(key=lambda item: 0 if item.source_type == _PM_MEMORY_SOURCE_TYPE else 1)
    kept: list[MemoryItem] = []
    used = 0
    dropped = 0
    for it in durable:
        cost = _est_tokens(_packet_item(it, open_overlay=False, why=""))
        if not kept or used + cost <= budget:  # never trim ALL durable
            kept.append(it)
            used += cost
        else:
            dropped += 1
    durable_truncated = len(kept) < len(durable)
    for it in wip:
        cost = _est_tokens(_packet_item(it, open_overlay=False, why=""))
        if used + cost <= budget:
            kept.append(it)
            used += cost
        else:
            dropped += 1
    return kept, dropped, durable_truncated


def build_role_context_packet(*, store: Any, role: str, task: Any = None,
                              pr: Any = None, token_budget: int | None = None) -> dict | None:
    """Build a role-scoped context packet, or None when the project has NO
    grounding at all (no local memory index AND no bound corpus → the runner
    emits an unchanged prompt). F104 S2: when a corpus is bound but the local
    memory index is absent (e.g. a remote AIAR adapter), still build a
    corpus-only packet so the implementer/reviewer get grounded."""
    mem = _memory_store_if_present(store)
    corpus_bound = _corpus_is_bound(store)
    if mem is None and not corpus_bound:
        return None
    budget = int(token_budget if token_budget is not None else role_token_budget(role))

    warnings: list[str] = []
    pkt_items: list[dict[str, Any]] = []
    dropped_over = 0
    claims_excluded = 0
    full_count = visible_count = 0
    if mem is not None:
        raw = _query_for_role(mem, role, task, pr)
        # safe-path re-screen (defense-in-depth over store admission)
        raw = [it for it in raw if not _paths.is_sensitive_path(it.source_ref.path)]
        raw = _sorted(raw)
        kept, dropped_over, durable_truncated = _trim(raw, budget)

        # mark WIP that overlays durable truth (same key) — never as truth.
        durable_keys = {_conflict_key(i) for i in kept if i.authority == "durable_truth"}
        durable_keys.discard(None)
        for it in kept:
            overlay = it.authority != "durable_truth" and _conflict_key(it) in durable_keys
            if it.source_type == _PM_MEMORY_SOURCE_TYPE:
                why = "pm working memory"
            else:
                why = "overlays durable truth" if overlay else f"{role} scope"
            pkt_items.append(_packet_item(it, open_overlay=overlay, why=why))
        if durable_truncated:
            warnings.append("durable_context_truncated")
        claims_excluded = len(mem.query(MemoryQuery(authorities=("claim",), role=role, limit=200)))
        full_count = len(mem.query(MemoryQuery(authorities=("durable_truth", "wip"), limit=200)))
        visible_count = len(mem.query(MemoryQuery(authorities=("durable_truth", "wip"), role=role, limit=200)))
    if not pkt_items:
        warnings.append("no_project_memory_for_scope")

    # --- corpus evidence -----------------------------------------------------
    corpus_evidence: list[dict[str, Any]] = []
    if role == "pm":
        try:
            from .pm_working_memory import retrieve_pm_working_memory_from_aiar
            ev = retrieve_pm_working_memory_from_aiar(store, top_k=3)
            corpus_evidence = list(ev.hits)
            if ev.status not in ("available", "no_corpus"):
                warnings.extend(ev.warnings or [f"pm_working_memory_{ev.status}"])
        except Exception:
            warnings.append("pm_working_memory_retrieval_unavailable")
    elif role in _CORPUS_RETRIEVAL_ROLES and corpus_bound:
        # F104 S2: the implementer + reviewer must SEE the corpus facts relevant
        # to their task (the bug this fixes: they coded the spec values blind).
        ev, status, _cid = _corpus_evidence(
            store, query=_role_corpus_query(store, task), top_k=4,
            cap=_CORPUS_SUMMARY_CAP)
        corpus_evidence = ev
        if status not in ("available", "empty", "no_corpus"):
            warnings.append(f"corpus_evidence_{status}")

    packet = {
        "schema_version": "project_context_packet.v1",
        "project_id": store.project_id,
        "role": role,
        "task_id": getattr(task, "task_id", None),
        "pr_id": (pr or {}).get("pr_id") if isinstance(pr, dict) else None,
        "budget": {
            "max_tokens": budget,
            "estimated_tokens": _est_tokens(pkt_items),
            "truncated": dropped_over > 0,
        },
        "items": pkt_items,
        "corpus_evidence": corpus_evidence,
        "omitted": {
            "over_budget": dropped_over,
            "not_visible_to_role": max(0, full_count - visible_count),
            "claims_excluded": claims_excluded,
            "external_excluded": 0,
        },
        "warnings": warnings,
    }
    packet["packet_id"] = "ctxpkt_" + hashlib.sha256(
        json.dumps(packet, sort_keys=True).encode()).hexdigest()[:16]
    return packet


def format_packet(packet: dict | None) -> str:
    """Render the packet for prompt injection — compact JSON behind a stable,
    short instruction. Empty/None packets render to ''. F104 S2: render when
    EITHER memory items OR corpus evidence is present (previously an empty
    ``items`` suppressed corpus evidence the implementer needs)."""
    if not packet or (not packet.get("items") and not packet.get("corpus_evidence")):
        return ""
    body = json.dumps(packet, ensure_ascii=False, sort_keys=True)
    return ("\nProject grounding context packet (cite mem: refs only; do NOT "
            "treat WIP/open_overlay as durable truth; corpus_evidence entries "
            "are AUTHORITATIVE retrieved spec facts — implement to them "
            "exactly):\n```json\n" + body + "\n```\n")


# --- F088-08: PM boot briefing ----------------------------------------------
PM_BOOT_BUDGET = 1800


def _briefing_item(item: MemoryItem) -> dict[str, Any]:
    # Every item carries a source id (spec: no summary without a source id).
    src = list(item.source_ids) or [f"mem:{item.memory_id}"]
    return {
        "ref": f"mem:{item.memory_id}",
        "summary": _summary_for(item),
        "source_type": item.source_type,
        "source_ref": item.source_ref.to_dict(),
        "source_ids": src,
        "why_included": "pm boot grounding",
    }


def _corpus_evidence(
    store: Any, *, query: str | None = None, top_k: int = 4,
    cap: int = _SUMMARY_CAP,
) -> tuple[list[dict[str, Any]], str, str | None]:
    """Retrieved corpus hits for ``query`` (default: project goal + DoD). Returns
    (evidence, status, corpus_id). status distinguishes no-evidence from failure:
    available | empty | unavailable | no_corpus. F104 S2: ``query`` lets an
    implementer/reviewer packet retrieve TASK-relevant facts (not just the
    project-level goal), and ``cap`` lets a whole rule block survive."""
    try:
        from .corpus_binding import load_binding
        binding = load_binding(store)
    except Exception:
        return [], "no_corpus", None
    corpus_id = binding.corpus_id
    if not corpus_id or binding.mode == "none":
        return [], "no_corpus", None
    if query is None:
        try:
            proj = store.get_project()
            query = f"{proj.north_star} {proj.definition_of_done}".strip()
        except Exception:
            query = ""
    from .retrieval import retrieve_with_status
    hits, status = retrieve_with_status(store, query=query, top_k=top_k)
    if status != "ok":
        # empty_query / unavailable surfaced honestly, NOT collapsed to "empty"
        mapped = {"empty_query": "empty", "unavailable": "unavailable"}.get(status, status)
        return [], mapped, corpus_id
    evidence = []
    for h in hits:
        summary = (h.content or "").strip()
        if not summary or _paths.content_has_secret(summary):
            continue
        evidence.append({
            "ref": f"hit:{h.corpus_id}:{h.chunk_id}",
            "corpus_id": h.corpus_id,
            "chunk_id": h.chunk_id,
            "score": h.score,
            "summary": summary[:cap],
            "source_ids": [f"corpus:{h.corpus_id}:{h.chunk_id}"],
            "source": (h.metadata or {}).get("source"),
            "why_included": "retrieved for project goal/API behavior",
        })
    return evidence, ("available" if evidence else "empty"), corpus_id


def _boot_corpus_evidence(store: Any) -> tuple[list[dict[str, Any]], str, str | None]:
    """PM-boot corpus evidence (project goal + DoD query). Thin wrapper over
    :func:`_corpus_evidence` kept for the boot-briefing call site."""
    return _corpus_evidence(store)


def build_pm_boot_briefing(*, store: Any, token_budget: int | None = None) -> dict | None:
    """First-PM-turn grounded briefing (durable truth + corpus evidence + open
    WIP/blockers), each fact source-cited. None when no grounding index."""
    mem = _memory_store_if_present(store)
    corpus_evidence, corpus_status, corpus_id = _boot_corpus_evidence(store)
    # No grounding AT ALL (no memory index AND no corpus) -> None so the runner
    # emits a byte-identical prompt. But memory-absent WITH a bound corpus still
    # yields a briefing from corpus evidence (spec) + a memory_unavailable warning.
    if mem is None and corpus_status == "no_corpus":
        return None
    budget = int(token_budget if token_budget is not None else PM_BOOT_BUDGET)
    warnings: list[str] = []

    if mem is None:
        warnings.append("memory_unavailable")
        durable = []
        wip = []
    else:
        durable = _sorted([it for it in mem.query(
            MemoryQuery(authorities=("durable_truth",), role="pm", limit=30))
            if not _paths.is_sensitive_path(it.source_ref.path)])
        wip = _sorted([it for it in mem.query(
            MemoryQuery(authorities=("wip",), role="pm", limit=20))
            if not _paths.is_sensitive_path(it.source_ref.path)])
    if corpus_status == "unavailable":
        warnings.append("corpus_unavailable")
    try:
        from .pm_working_memory import retrieve_pm_working_memory_from_aiar
        pm_ev = retrieve_pm_working_memory_from_aiar(store, top_k=3)
        if pm_ev.hits:
            seen_refs = {item.get("ref") for item in corpus_evidence}
            for hit in pm_ev.hits:
                if hit.get("ref") not in seen_refs:
                    corpus_evidence.append(hit)
        if pm_ev.status not in ("available", "no_corpus"):
            warnings.extend(pm_ev.warnings or [f"pm_working_memory_{pm_ev.status}"])
    except Exception:
        warnings.append("pm_working_memory_retrieval_unavailable")

    blockers = []
    try:
        for t in store.list_tasks(state="blocked"):
            blockers.append({"ref": f"ledger:task:{t.task_id}",
                             "summary": (t.title or "")[:_SUMMARY_CAP],
                             "source_ids": [f"ledger:task:{t.task_id}"]})
    except Exception:
        pass

    context_requests = [_briefing_item(it) for it in wip if it.source_type == "context_request"]
    open_wip = [_briefing_item(it) for it in wip if it.source_type != "context_request"]

    briefing: dict[str, Any] = {
        "schema_version": "pm_boot_briefing.v1",
        "project_id": store.project_id,
        "orientation_ref": "orientation:inline",
        "durable_truth": [_briefing_item(it) for it in durable],
        "corpus_evidence": corpus_evidence,
        "open_wip": open_wip,
        "blockers": blockers,
        "context_requests": context_requests,
        "freshness": {"corpus_id": corpus_id, "corpus_retrieval": corpus_status},
        "warnings": warnings,
    }
    # Budget: trim open_wip first, then corpus_evidence; never durable/blockers.
    while _est_tokens(briefing) > budget and (briefing["open_wip"] or briefing["corpus_evidence"]):
        if briefing["open_wip"]:
            briefing["open_wip"].pop()
        else:
            briefing["corpus_evidence"].pop()
        if "boot_briefing_truncated" not in briefing["warnings"]:
            briefing["warnings"].append("boot_briefing_truncated")
    briefing["budget"] = {"max_tokens": budget, "estimated_tokens": _est_tokens(briefing),
                          "truncated": "boot_briefing_truncated" in briefing["warnings"]}
    briefing["briefing_id"] = "pmboot_" + hashlib.sha256(
        json.dumps(briefing, sort_keys=True).encode()).hexdigest()[:16]
    return briefing


def format_pm_boot_briefing(briefing: dict | None) -> str:
    if not briefing:
        return ""
    body = json.dumps(briefing, ensure_ascii=False, sort_keys=True)
    return ("\nPM boot briefing (grounded project picture; evidence INFORMS "
            "planning but is NOT auto-approved truth — cite refs):\n```json\n"
            + body + "\n```\n")


__all__ = ["build_role_context_packet", "format_packet", "role_token_budget",
           "ROLE_TOKEN_BUDGET", "build_pm_boot_briefing", "format_pm_boot_briefing",
           "PM_BOOT_BUDGET", "ensure_pm_working_memory"]
