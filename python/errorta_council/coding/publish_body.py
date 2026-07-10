"""F102 Slice E — build the PR / initial-commit body for a published project.

PURE + COUNCIL-SIDE (D-OQ3): composes a concise, REDACTED summary from the
project ledger (North Star, what was built, tests-passed) plus the latest F101
runtime verdict when present. NO egress (no subprocess / network) — it reads the
ledger + runtime evidence stores only; the no-egress guard test (RC8) enforces
this. Every emitted line is redacted (tokens / home path / username) and the
whole body is capped, so a stray path or token can never reach a PR description.
"""
from __future__ import annotations

from typing import Any

from errorta_diagnostics.redact import (
    redact_home_path,
    redact_tokens,
    redact_username,
)

_BODY_CAP = 4000
_NORTH_STAR_CAP = 600
_TITLES_CAP = 12


def _redact(text: str) -> str:
    out, _ = redact_tokens(text or "")
    out, _ = redact_home_path(out)
    out, _ = redact_username(out)
    return out


def _runtime_line(store: Any) -> str | None:
    """The latest fresh F101 runtime verdict as one line, or None when there is
    no runtime evidence. Best-effort: any failure to read evidence omits the
    line (never raises into the body builder)."""
    try:
        from errorta_council.coding.runtime import (
            RuntimeProfileStore,
            latest_runtime_evidence,
        )
        rstore = RuntimeProfileStore.for_ledger(store)
        try:
            head = store_head(store)
        except Exception:
            head = ""
        evidence = latest_runtime_evidence(rstore, current_head=head)
    except Exception:
        return None
    results = evidence.get("results") or []
    if not results:
        return None
    passed = sum(1 for r in results if r.get("passed"))
    fresh = sum(1 for r in results if r.get("fresh"))
    total = len(results)
    return (f"Runtime checks (F101): {passed}/{total} passed"
            + (f", {fresh} fresh against current head" if fresh else ""))


def store_head(store: Any) -> str:
    """Resolve the project's current workspace head for runtime-evidence freshness
    WITHOUT egress here — delegate to the workspace (which owns the git seam).
    Returns '' when unavailable."""
    try:
        from errorta_council.coding.workspace import CodingWorkspace
        proj = store.get_project()
        ws = CodingWorkspace(store.project_id, store)
        ws.set_target(proj.target)
        if not ws.exists():
            return ""
        return ws.head()
    except Exception:
        return ""


def build_publish_body(store: Any) -> str:
    """Compose the redacted publish body for ``store``'s project.

    Sections (each optional / omitted when empty):
    * North Star
    * "What was built" — the done dev-task titles
    * Tests-passed summary (task board: done / total)
    * Latest F101 runtime verdict (only when evidence exists)

    Returns a capped, redacted string. Pure."""
    lines: list[str] = []

    proj = store.get_project()
    north_star = (proj.north_star or "").strip()
    if north_star:
        lines.append("## North Star")
        lines.append(_redact(north_star)[:_NORTH_STAR_CAP])
        lines.append("")

    tasks = [t for t in store.list_tasks() if t.state != "dropped"]
    done_dev_titles = [
        str(t.title) for t in tasks
        if t.state == "done" and getattr(t, "role", "") in ("dev", "")
    ]
    if not done_dev_titles:
        done_dev_titles = [str(t.title) for t in tasks if t.state == "done"]
    if done_dev_titles:
        lines.append("## What was built")
        for title in done_dev_titles[:_TITLES_CAP]:
            lines.append(f"- {_redact(title)}")
        if len(done_dev_titles) > _TITLES_CAP:
            lines.append(f"- ... and {len(done_dev_titles) - _TITLES_CAP} more")
        lines.append("")

    total = len(tasks)
    done = sum(1 for t in tasks if t.state == "done")
    if total:
        lines.append("## Status")
        lines.append(f"{done}/{total} planned tasks complete.")
        lines.append("")

    runtime = _runtime_line(store)
    if runtime:
        lines.append(_redact(runtime))
        lines.append("")

    lines.append("Published by Errorta Coding Team.")
    body = "\n".join(lines).strip()
    return _redact(body)[:_BODY_CAP]


__all__ = ["build_publish_body"]
