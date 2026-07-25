"""GL01 (Item 2) — test anchors: a mechanical anti-oscillation lock.

The revise spiral churned because the loop had no memory of "this used to work." A
dev could break a previously-green behavior and the only check was the reviewer's
unverifiable "did you break something?" — an execution-free judge that over-rejects
and makes iteration net-harmful. AlphaCodium's fix is mechanical: never accept a
revision that regresses a previously-passing check.

An **anchor** is that memory. Once a probe or gate command goes GREEN on the
integrated master head, it becomes an anchor keyed by its probe/command id. A later
head that flips a green anchor RED is a **regression**: it records an
``anchor_regressed`` decision (the signal GL04 keys on) and raises ONE deduped
non-blocking alert, so the operator sees the oscillation rather than inferring it.

Deliberately **satisfiable** (re-green clears it) and **master-scoped**: it blocks
at the integrated head, never per-branch — it does NOT touch ``_set_mergeable_if_ready``,
so a partial-branch merge is never wedged (Spec 12 Item 1's advisory design is
preserved). An anchor exists ONLY after a real green ``record_test_run`` — never
invented, the same "inventing a gate is fiction" discipline Spec 12 Item 1 applies.

Discipline: NO ``runner`` import (stored additively in ``run_state`` under
``test_anchors``, no migration — mirroring ``gate_due`` / ``unlocks_foundation``).
Fully guarded — a ledger hiccup degrades to "no anchors" rather than raising into a
turn.
"""
from __future__ import annotations

import time
from typing import Any

_RUN_STATE_KEY = "test_anchors"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load_anchors(store: Any) -> dict[str, Any]:
    try:
        raw = store.get_run_state().get(_RUN_STATE_KEY) or {}
    except Exception:  # noqa: BLE001
        return {}
    return dict(raw) if isinstance(raw, dict) else {}


def promote_anchor(store: Any, key: str, *, head: str) -> None:
    """Record (or re-affirm) that ``key`` is green at ``head``. Additive in
    ``run_state``; idempotent — a still-green anchor just re-stamps its head with
    no side effects. Best-effort: a write failure never fails the turn."""
    key = str(key or "")
    if not key:
        return
    anchors = _load_anchors(store)
    anchors[key] = {"head": str(head or ""), "at": _now()}
    try:
        store.set_run_state(**{_RUN_STATE_KEY: anchors})
    except Exception:  # noqa: BLE001
        pass


def broken_anchors(store: Any, run: dict[str, Any]) -> list[dict[str, Any]]:
    """Given a just-recorded test-run record (``{"results":[...], "head":...}``),
    return the anchors it REGRESSES: a key that was green (an existing anchor) and
    is now red in this run at a DIFFERENT head. A red result at the anchor's own
    head is not a regression (a flap on the same tree, not an oscillation across
    heads); a key with no anchor was never green, so it can't regress.

    Each entry is ``{"key", "anchor_head", "broken_head"}``. Pure read — never
    mutates, never raises."""
    anchors = _load_anchors(store)
    if not anchors:
        return []
    head = str(run.get("head") or "")
    breaks: list[dict[str, Any]] = []
    for r in run.get("results") or []:
        if not isinstance(r, dict):
            continue
        key = str(r.get("command_id") or "")
        if not key or key not in anchors:
            continue
        if r.get("passed"):
            continue  # still green — not a break
        anchor_head = str((anchors.get(key) or {}).get("head") or "")
        if anchor_head and head and anchor_head != head:
            breaks.append({"key": key, "anchor_head": anchor_head,
                           "broken_head": head})
    return breaks


def reconcile(store: Any, run: dict[str, Any], *,
              project_id: str = "") -> list[dict[str, Any]]:
    """The GateRun / delivery seam calls this after a probe or gate session lands.

    1. Detect regressions against the EXISTING anchors (before any promotion).
    2. Promote/re-affirm an anchor for every GREEN result in the run.
    3. For each regression: record an ``anchor_regressed`` decision (the GL04
       signal) and raise ONE deduped non-blocking alert.

    Returns the list of breaks (empty when none). Never touches merge state
    (``_set_mergeable_if_ready``) — the lock is master-scoped, not a per-branch
    veto. Fully guarded: any failure returns what it has and never raises."""
    try:
        head = str(run.get("head") or "")
        breaks = broken_anchors(store, run)
        for r in run.get("results") or []:
            if isinstance(r, dict) and r.get("passed"):
                promote_anchor(store, str(r.get("command_id") or ""), head=head)
        for b in breaks:
            _record_break(store, b, project_id=project_id)
        return breaks
    except Exception:  # noqa: BLE001 — the anchor lock is best-effort
        return []


def _record_break(store: Any, brk: dict[str, Any], *, project_id: str) -> None:
    key = str(brk.get("key") or "")
    anchor_head = str(brk.get("anchor_head") or "")
    broken_head = str(brk.get("broken_head") or "")
    rationale = (
        f"anchor {key!r} was green at head {anchor_head[:12]} and is now red at "
        f"the integrated head {broken_head[:12]} — a previously-passing check "
        "regressed (oscillation). Re-green it to clear this signal.")
    try:
        store.record_decision(
            title="test anchor regressed", context="anchor",
            choice="anchor_regressed", rationale=rationale)
    except Exception:  # noqa: BLE001
        pass
    pid = project_id or str(getattr(store, "project_id", "") or "")
    if not pid:
        return
    try:
        from . import attention
        attention.raise_anchor_regressed_alert(
            pid, key=key, summary=rationale, store=store)
    except Exception:  # noqa: BLE001 — an alert failure never fails the turn
        pass


__all__ = ["promote_anchor", "broken_anchors", "reconcile"]
