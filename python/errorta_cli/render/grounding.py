"""Grounding views (F147 §8) — corpus binding + retrieval + memory.

Renderers select fields; the raw corpus text / memory document is never dumped
(invariant #4) — only refs, counts, statuses. ``--json`` is the explicit bypass.
"""
from __future__ import annotations

from typing import Any

from rich.table import Table
from rich.text import Text

from . import heading, muted, render, truncate


def _kv(key: str, value: Any) -> Text:
    t = Text()
    t.append(f"{key}: ", style="cli.key")
    t.append(str(value))
    return t


def render_binding(payload: Any) -> str:
    binding = (payload or {}).get("binding") or {}
    lines = [heading("Corpus binding")]
    for key in ("mode", "corpus_id", "status", "source"):
        if binding.get(key) not in (None, ""):
            lines.append(_kv(key, binding.get(key)))
    if len(lines) == 1:
        lines.append(muted("(no corpus bound)"))
    return render(*lines)


def render_corpora(payload: Any) -> str:
    corpora = (payload or {}).get("corpora") or []
    if not corpora:
        return render(heading("Corpora"), muted("(no corpora available)"))
    table = Table(show_edge=False, pad_edge=False, box=None)
    table.add_column("corpus", style="cli.key", no_wrap=True)
    table.add_column("docs", no_wrap=True)
    for c in corpora:
        if isinstance(c, dict):
            table.add_row(str(c.get("corpus_id") or c.get("id") or c.get("name") or ""),
                          str(c.get("doc_count") or c.get("docs") or ""))
        else:
            table.add_row(str(c), "")
    return render(heading("Corpora"), table)


def render_capabilities(payload: Any) -> str:
    caps = (payload or {}).get("capabilities") or {}
    lines = [heading("Grounding capabilities")]
    for key in ("source", "retrieve", "ingest", "remote_ingest", "available"):
        if key in caps:
            lines.append(_kv(key, caps.get(key)))
    if len(lines) == 1:
        lines.append(muted(truncate(str(caps), 120)))
    return render(*lines)


def render_retrieve(payload: Any) -> str:
    hits = (payload or {}).get("hits") or []
    status = (payload or {}).get("status")
    lines = [heading("Retrieval")]
    if status:
        lines.append(muted(f"status: {status}"))
    if not hits:
        lines.append(muted("(no matches)"))
        return render(*lines)
    for h in hits:
        cid = h.get("citation_id") or h.get("chunk_id") or h.get("corpus_id") or ""
        score = h.get("score")
        text = h.get("content") or h.get("text") or ""
        head = Text()
        head.append(f"[{cid}] ", style="cli.key")
        if score is not None:
            head.append(f"({score}) ", style="cli.muted")
        head.append(truncate(text, 100))
        lines.append(head)
    return render(*lines)


def render_bootstrap(payload: Any) -> str:
    job = (payload or {}).get("job") or {}
    status = str(job.get("status") or "")
    ok = status == "done"
    lines = [heading("Corpus bootstrap"),
             Text(status or "unknown", style="cli.ok" if ok else "cli.warn")]
    if job.get("corpus_id"):
        lines.append(_kv("corpus", job.get("corpus_id")))
    for key in ("ingested", "indexed", "included"):
        if job.get(key) not in (None, ""):
            lines.append(_kv(key, job.get(key)))
    errors = job.get("errors") or []
    if errors:
        lines.append(Text("errors: " + truncate("; ".join(str(e) for e in errors), 120),
                          style="cli.bad"))
    return render(*lines)


def render_memory(payload: Any) -> str:
    sub = str(payload.get("sub") or "") if isinstance(payload, dict) else ""
    counts = (payload or {}).get("counts") or (payload or {}).get("result") or {}
    lines = [heading(f"Project memory ({sub})")]
    if isinstance(counts, dict) and counts:
        for key, value in counts.items():
            lines.append(_kv(key, value))
    else:
        lines.append(muted("done"))
    return render(*lines)


def render_build(payload: Any) -> str:
    result = (payload or {}).get("result") or {}
    lines = [heading("Build corpus from project")]
    for key in ("corpus_id", "status", "ingested"):
        if result.get(key) not in (None, ""):
            lines.append(_kv(key, result.get(key)))
    if len(lines) == 1:
        lines.append(muted("done"))
    return render(*lines)


def render_working_memory(payload: Any) -> str:
    wm = (payload or {}).get("pm_working_memory") or {}
    lines = [heading("PM working memory")]
    # Redacted health only — refs/status, never the document body.
    for key in ("status", "exists", "ref", "updated_at", "sections"):
        if wm.get(key) not in (None, ""):
            lines.append(_kv(key, wm.get(key)))
    if len(lines) == 1:
        lines.append(muted(truncate(str(wm), 120)))
    return render(*lines)
