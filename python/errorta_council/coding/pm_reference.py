"""F145 Slice 1 — the PM reference context: operator manual + live-state snapshot.

``build_pm_reference_context`` assembles the two halves the conversational PM (AI
Wizard + control plane) reads:

1. **The static reference** — ``docs/coding/PM_REFERENCE.md`` (the operator's
   manual). Its embedded machine-readable contract is held honest by the F145
   anti-drift canary (``tests/coding/test_f145_pm_reference.py``).
2. **The live-state snapshot** — a *redacted, allowlisted* projection of what is
   actually available right now: the model routes the gateway reports available,
   and (for an existing project) its current autonomy / governance / guardrail
   config, its runtime profile shape, and its team. This is what lets the PM obey
   the grounded-or-refuse rule — it never proposes a model the live state does
   not show.

Read-only + allowlisted: the snapshot is built field-by-field from safe values
(route ids, enums, ints, roles) — never full config objects — so no API keys,
tokens, filesystem paths, or free-form member prompts can leak into the PM prompt.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# Soft caps so the injected snapshot can't bloat the PM prompt (no secret risk —
# purely token economy). Truncation is flagged in the snapshot.
_MAX_ROUTES = 200
_MAX_MEMBERS = 64

# Candidate locations for the reference doc, most-specific first. In the frozen
# sidecar the doc ships under ``_MEIPASS/docs/coding`` (see ``sidecar.spec``); in
# dev it lives in the repo. An env override wins for tests / relocation.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_REL = Path("docs") / "coding" / "PM_REFERENCE.md"


def _reference_candidates() -> list[Path]:
    cands: list[Path] = []
    env = os.environ.get("ERRORTA_PM_REFERENCE_PATH")
    if env:
        cands.append(Path(env))
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cands.append(Path(meipass) / _REL)
    cands.append(_REPO_ROOT / _REL)
    return cands


def reference_path() -> Path:
    """The resolved path to the PM Reference Document, or raise ``FileNotFoundError``
    listing everywhere we looked."""
    for cand in _reference_candidates():
        if cand.is_file():
            return cand
    searched = ", ".join(str(c) for c in _reference_candidates())
    raise FileNotFoundError(f"PM_REFERENCE.md not found (looked in: {searched})")


def load_reference_text() -> str:
    """The full operator's manual as text (prose + the embedded contract block)."""
    return reference_path().read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Live-state snapshot — allowlisted, redacted-by-construction.
# --------------------------------------------------------------------------- #
def list_available_routes() -> list[dict[str, str]]:
    """The model routes the gateway currently reports **available**, as
    ``[{route_id, family, provider_class}]``. Fail-open-empty: a gateway error
    yields an empty list (the PM then honestly has nothing to offer) rather than
    raising into the prompt build."""
    try:
        from errorta_app.routes.gateway import list_routes
        from errorta_council.coding.model_availability import (
            available_route_ids,
            resolve_route_availability,
        )

        routes = list_routes(None).get("routes", [])
        by_id = {
            str(r.get("route_id")): r
            for r in routes
            if r.get("route_id")
        }
        projection = resolve_route_availability(list(by_id))
        available = available_route_ids(projection)
        out: list[dict[str, str]] = []
        for route_id in sorted(available)[:_MAX_ROUTES]:
            r = by_id.get(route_id, {})
            out.append({
                "route_id": route_id,
                "family": str(r.get("family") or ""),
                "provider_class": str(r.get("provider_class") or ""),
            })
        return out
    except Exception:
        _log.debug("pm_reference: available-route listing failed", exc_info=True)
        return []


def _member_snapshot(member: dict[str, Any]) -> dict[str, Any]:
    """Safe fields of one room member — id, role, mode, and route(s) only. Never
    the system prompt, metadata blob, or any free-form/path field."""
    from errorta_council.coding.topology import coding_role_of

    mode = str(member.get("model_mode") or "single")
    snap: dict[str, Any] = {
        "id": str(member.get("id") or ""),
        "coding_role": coding_role_of(member),
        "model_mode": mode,
    }
    if mode == "multi":
        pool = member.get("model_pool")
        snap["model_pool"] = [str(r) for r in pool] if isinstance(pool, list) else []
    else:
        snap["gateway_route_id"] = str(member.get("gateway_route_id") or "") or None
    return snap


def _room_snapshot(store: Any) -> dict[str, Any] | None:
    """The project's current team: room id + an allowlisted member projection."""
    try:
        run_config = store.get_run_config()
    except Exception:
        return None
    if not isinstance(run_config, dict):
        return None
    members = run_config.get("members")
    members = members if isinstance(members, list) else []
    dicts = [m for m in members if isinstance(m, dict)]
    return {
        "room_id": run_config.get("room_id"),
        "members": [_member_snapshot(m) for m in dicts[:_MAX_MEMBERS]],
        "members_truncated": len(dicts) > _MAX_MEMBERS,
    }


def _runtime_snapshot(store: Any) -> dict[str, Any] | None:
    """The default runtime profile's shape (kind / mode / sandbox) — no commands,
    paths, or env."""
    try:
        from errorta_council.coding.runtime import RuntimeProfileStore

        rstore = RuntimeProfileStore.for_ledger(store)
        profile = rstore.get_profile("default")
    except Exception:
        return None
    if profile is None:
        return None
    return {
        "profile_id": profile.profile_id,
        "kind": profile.kind,
        "runtime_mode": profile.runtime_mode,
        "sandbox": profile.sandbox,
    }


def build_live_state(project_id: str | None = None, *, store: Any = None) -> dict[str, Any]:
    """The dynamic half of the PM's context. Always includes the available model
    routes; for an existing project also includes its current config + team.

    ``store`` may be passed to avoid re-opening the ledger; otherwise it is opened
    from ``project_id``. With neither (the pre-project AI Wizard case) only the
    global availability + the config defaults are returned."""
    state: dict[str, Any] = {
        "available_routes": list_available_routes(),
    }

    if store is None and project_id:
        try:
            from errorta_council.coding.ledger import LedgerStore

            store = LedgerStore(project_id)
        except Exception:
            store = None

    if store is None:
        # Pre-project (Wizard): advertise the config defaults so the PM can reason
        # about what it will set.
        from errorta_council.coding.autonomy import (
            CodingAutonomyPolicy,
            policy_to_dict,
        )

        state["project"] = None
        state["autonomy_defaults"] = policy_to_dict(CodingAutonomyPolicy())
        return state

    try:
        from errorta_council.coding.autonomy import load_policy, policy_to_dict
        from errorta_council.coding.governance import GovernanceStore
        from errorta_council.coding.skills import load_guardrail

        state["project"] = {
            "autonomy": policy_to_dict(load_policy(store)),
            "governance": GovernanceStore.for_ledger(store).load_state().to_dict(),
            "guardrail_enabled": bool(load_guardrail(store).enabled),
            "runtime": _runtime_snapshot(store),
            "room": _room_snapshot(store),
        }
    except Exception:
        # A partially-set-up project still yields availability; never raise into
        # the prompt.
        _log.debug("pm_reference: project live-state build failed for %s",
                   project_id, exc_info=True)
        state["project"] = None

    return state


def build_pm_reference_context(
    project_id: str | None = None, *, store: Any = None,
) -> str:
    """The assembled PM system-prompt block: the operator's manual followed by a
    compact JSON live-state snapshot. Deterministic given the same inputs."""
    manual = load_reference_text()
    live = build_live_state(project_id, store=store)
    snapshot = json.dumps(live, sort_keys=True, indent=2)
    return (
        f"{manual}\n\n"
        "## LIVE STATE (authoritative — overrides the manual on availability)\n\n"
        "Only offer models present in `available_routes`. If a requested "
        "capability is absent here, refuse and say what is available.\n\n"
        "```json\n"
        f"{snapshot}\n"
        "```\n"
    )


__all__ = [
    "reference_path",
    "load_reference_text",
    "list_available_routes",
    "build_live_state",
    "build_pm_reference_context",
]
