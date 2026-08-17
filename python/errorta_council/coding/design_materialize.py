"""Slice 1 §4 — spawn the one "materialize design system" DEV task on approval.

On ``design_spec`` approval a single DEV task is created that (per §4) generates
``tokens.css`` + ``base.css`` from the artifact's ``body_json``, copies the chosen
font/icon files from the host asset library into the project repo, and wires
``@font-face``. It must be spawned EXACTLY ONCE — the idempotency is a
title+provenance check on the backlog, so a re-scheduled approval turn never
duplicates it.
"""
from __future__ import annotations

from typing import Any

from .design_scheduler import MATERIALIZE_TITLE
from .topology import DEV


def _existing_materialize_task(store: Any) -> bool:
    """True if the materialize task already exists (fixed title — the idempotency
    key). One design_spec => one materialize task."""
    try:
        tasks = store.list_tasks()
    except Exception:  # noqa: BLE001
        return False
    return any(
        str(getattr(task, "title", "")).strip().lower() == MATERIALIZE_TITLE
        for task in tasks)


def _materialize_detail(chosen_font_ids: list[str], chosen_icon_id: str) -> str:
    from . import design_library
    root = design_library.library_root()
    lines = [
        "Generate the design system from the approved design_spec artifact:",
        "1. Write tokens.css from body_json.tokens (palette, type scale, spacing, "
        "radii, shadows) as CSS custom properties. Consume these tokens everywhere; "
        "never invent raw values.",
        "2. Write base.css: element resets + the @font-face rules wiring the chosen "
        "font families, and base typographic styles from the tokens.",
        "3. Copy the chosen font + icon files from the host asset library into the "
        f"project (e.g. assets/fonts/, assets/icons/). Library root: {root}.",
    ]
    if chosen_font_ids:
        lines.append("Chosen font family ids: " + ", ".join(chosen_font_ids) + ".")
    if chosen_icon_id:
        lines.append(f"Chosen icon set id: {chosen_icon_id}.")
    lines.append("This task precedes all other UI dev tasks (spec §4).")
    return "\n".join(lines)


def spawn_materialize_task_if_needed(store: Any, governance: Any) -> bool:
    """Create the single materialize DEV task if the design_spec is approved and the
    task does not already exist. Returns True iff it created the task (so a caller
    can record the decision). Idempotent: a second call after approval creates
    nothing and returns False."""
    artifact = governance.latest_approved_artifact("design_spec")
    if artifact is None:
        return False
    if _existing_materialize_task(store):
        return False
    body = artifact.body_json if isinstance(artifact.body_json, dict) else {}
    assets = body.get("assets") if isinstance(body.get("assets"), dict) else {}
    font_ids = [str(x) for x in (assets.get("font_family_ids") or []) if x]
    icon_id = str(assets.get("icon_set_id") or "")
    store.add_task(
        title=MATERIALIZE_TITLE,
        role=DEV,
        detail=_materialize_detail(font_ids, icon_id),
        task_type="implementation",
        target_files=["tokens.css", "base.css"],
    )
    return True


__all__ = ["spawn_materialize_task_if_needed"]
