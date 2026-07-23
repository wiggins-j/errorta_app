"""F145 Slice 4 — typed control-actions ("change the team by talking").

Each action is a bounded mutation the PM (or the control-plane UI) may request; it
maps to exactly one existing config store, is grounded against the live catalog,
and is wrapped in a PM Changes record (S3) so it is announced + reversible.

Actions:
- ``assign_models_by_role`` — set the route of every member of a role, resolving
  a human name against the live catalog (honest refusal if absent/ambiguous);
  PM-single-only enforced.
- ``set_autonomy`` — CodingAutonomyPolicy knobs (real levers only).
- ``set_governance`` — governance mode / block_on_problems.
- ``create_task`` — add a task to the backlog (reversible: decline drops it).
- ``start_run`` — kick off the team. NOT handled here (the run machinery lives in
  the routes layer): it is a recognized type that the pm-ask/interject routes pop
  and execute; ``apply_action`` refuses it so it can never run via this seam.

None of these touch ``human_code_approval`` enforcement or gateway budget (not
wired — see PM_REFERENCE). Callers pass ``surface="log"`` for a PM-initiated change
during an accepted autonomous run; the default ``"pop"`` surfaces a review.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from ..validation import PM_MODEL_MODES
from . import paths as paths_mod
from . import pm_changes, task_dedupe
from .topology import coding_role_of

# CodingAutonomyPolicy knobs a control-action may set (the real levers).
_AUTONOMY_KNOBS = frozenset({
    "checkpoint_cadence", "checkpoint_n", "max_iterations", "max_model_calls",
    "max_parallel_workers",
})
_GOVERNANCE_FIELDS = frozenset({"mode", "block_on_problems", "max_review_rounds"})
_TASK_ROLES = frozenset({"pm", "dev", "reviewer", "tester"})
# Every action ``type`` the PM may legitimately emit. ``start_run`` is route-handled
# (see module docstring). Used to validate a fenced envelope before executing it.
KNOWN_ACTION_TYPES = frozenset({
    "assign_models", "set_autonomy", "set_governance", "create_task", "start_run",
})


class ControlActionError(Exception):
    """A control-action could not be applied (bad target, unresolved model, …)."""

    def __init__(self, code: str, message: str = "", **extra: Any) -> None:
        super().__init__(message or code)
        self.code = code
        self.extra = extra


# --------------------------------------------------------------------------- #
# Grounded name resolution.
# --------------------------------------------------------------------------- #
def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", name.lower()) if t]


def resolve_route(name: str, available: list[dict[str, Any]]) -> str:
    """Resolve a human model name to an available ``route_id`` — grounded-or-refuse.

    Exact ``route_id`` wins. Otherwise every token of ``name`` must appear in a
    route's ``route_id``/``family``/``provider_class``; a unique match wins, else
    we refuse (``model_ambiguous`` / ``model_not_found``) with the candidates —
    never a guess."""
    ids = {str(r.get("route_id")) for r in available if r.get("route_id")}
    if name in ids:
        return name
    toks = _tokens(name)
    if not toks:
        raise ControlActionError("model_not_found", f"no model matches {name!r}",
                                 available=sorted(ids))
    matches: list[str] = []
    for r in available:
        # Exact-token (not substring) match against the route's tokens, so a query
        # fragment like "4" can't spuriously match "4.6" into a confident-but-wrong
        # unique resolution — a partial name simply refuses.
        hay_tokens = set(_tokens(" ".join(
            str(r.get(k) or "") for k in ("route_id", "family", "provider_class"))))
        if all(t in hay_tokens for t in toks):
            matches.append(str(r.get("route_id")))
    matches = sorted(set(matches))
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ControlActionError("model_not_found", f"no available model matches {name!r}",
                                 requested=name, available=sorted(ids))
    raise ControlActionError("model_ambiguous", f"{name!r} matches several models",
                             requested=name, candidates=matches)


# --------------------------------------------------------------------------- #
# Actions.
# --------------------------------------------------------------------------- #
def assign_models_by_role(
    store: Any, role_routes: dict[str, str], *,
    available: list[dict[str, Any]], surface: str = "pop",
) -> pm_changes.PmChange:
    """Set the (single) route of every member of each named role. ``role_routes``
    maps a coding role (dev/reviewer/tester/pm) to a human model name."""
    cfg = store.get_run_config()
    members = cfg.get("members")
    members = [dict(m) for m in members] if isinstance(members, list) else []
    if not members:
        raise ControlActionError("no_team", "this project has no team to reassign")

    resolved = {role: resolve_route(name, available) for role, name in role_routes.items()}
    details: list[dict[str, Any]] = []
    before_members = [dict(m) for m in members]
    changed = 0
    for m in members:
        role = coding_role_of(m)
        if role not in resolved:
            continue
        route = resolved[role]
        # PM is single-only (validation.PM_MODEL_MODES): the assignment below
        # always writes model_mode="single", so a PM can never end up multi here.
        assert "single" in PM_MODEL_MODES
        prior = m.get("gateway_route_id")
        if prior == route and m.get("model_mode") == "single":
            continue
        m["model_mode"] = "single"
        m["gateway_route_id"] = route
        m.pop("model_pool", None)
        details.append({"field": f"{m.get('id')}.gateway_route_id",
                        "before": prior, "after": route})
        changed += 1

    if changed == 0:
        raise ControlActionError("no_matching_members",
                                 "no members matched the requested roles")

    store.set_run_config(members=members)
    summary = "Reassigned models: " + ", ".join(
        f"{role} → {route}" for role, route in resolved.items())
    return pm_changes.record_change(
        store, summary=summary, details=details,
        restore_target="run_config",
        restore_value={"room_id": cfg.get("room_id"), "members": before_members},
        surface=surface)


def set_autonomy(
    store: Any, knobs: dict[str, Any], *, surface: str = "pop",
    autonomy_warning: bool = False, suggested_cap: int | None = None,
) -> pm_changes.PmChange:
    """Set CodingAutonomyPolicy knobs (real levers only). Unknown keys are refused."""
    from .autonomy import load_policy, policy_from_dict, policy_to_dict, save_policy

    bad = set(knobs) - _AUTONOMY_KNOBS
    if bad:
        raise ControlActionError("unknown_autonomy_knob",
                                 f"not settable: {sorted(bad)}", knobs=sorted(bad))
    current = policy_to_dict(load_policy(store))
    before = {k: current.get(k) for k in knobs}
    save_policy(store, policy_from_dict({**current, **knobs}))
    details = [{"field": k, "before": before[k], "after": knobs[k]} for k in knobs]
    autonomous = knobs.get("checkpoint_cadence") == "off"
    autonomy_meta = None
    if autonomous or autonomy_warning:
        autonomy_meta = {"warning": True, "suggested_cap": suggested_cap}
    return pm_changes.record_change(
        store, summary="Updated autonomy", details=details,
        restore_target="autonomy", restore_value=before,
        surface=surface, autonomy=autonomy_meta)


def set_governance(
    store: Any, fields: dict[str, Any], *, surface: str = "pop",
) -> pm_changes.PmChange:
    """Set governance mode / block_on_problems / max_review_rounds. Never
    human_code_approval (not enforced by the runner — see PM_REFERENCE)."""
    from .governance import GovernanceStore

    bad = set(fields) - _GOVERNANCE_FIELDS
    if bad:
        raise ControlActionError("unsettable_governance_field",
                                 f"not settable here: {sorted(bad)}", fields=sorted(bad))
    gov = GovernanceStore.for_ledger(store)
    state = gov.load_state().to_dict()
    before = {k: state.get(k) for k in fields}
    gov.update_state(**fields)
    details = [{"field": k, "before": before[k], "after": fields[k]} for k in fields]
    return pm_changes.record_change(
        store, summary="Updated governance", details=details,
        restore_target="governance", restore_value=before, surface=surface)


# --------------------------------------------------------------------------- #
# Natural-language directive -> structured actions (grounded-or-refuse).
# --------------------------------------------------------------------------- #
_INTERPRET_INSTRUCTIONS = """
Translate the user's directive into control-actions for the Coding Team, grounded
in the LIVE STATE above. Respond with a SINGLE JSON object and nothing else:
{"actions": [
  {"type": "create_task", "title": "<short title>", "detail": "<what + acceptance>", "role": "dev"},
  {"type": "start_run"},
  {"type": "assign_models", "role_routes": {"dev": "<model name>", "reviewer": "<model name>"}},
  {"type": "set_autonomy", "knobs": {"checkpoint_cadence": "off"}},
  {"type": "set_governance", "fields": {"block_on_problems": false}}
]}
Emit only the actions the directive calls for. "Fix X" / "add Y" -> a create_task
(and start_run if they want it worked now). "Put the devs on <model>" -> assign_models.
Use only model names present in `available_routes`. If the directive can't be met,
return {"actions": [], "refusal": "<why + what is available>"}.
"""


def interpret_directive(directive: str, *, context: str,
                        complete: Callable[[str], str]) -> dict[str, Any]:
    """Ask the PM model to turn a directive into a structured actions list. Returns
    ``{actions: [...], refusal: str|None}``; a parse failure yields an empty list +
    a refusal (never a guessed action)."""
    import json
    import re as _re

    prompt = f"{context}\n{_INTERPRET_INSTRUCTIONS}\nDirective: {directive}\nJSON:"
    raw = complete(prompt) or ""
    m = _re.search(r"\{.*\}", raw, _re.DOTALL)
    if not m:
        return {"actions": [], "refusal": "could not interpret the directive"}
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return {"actions": [], "refusal": "could not interpret the directive"}
    actions = obj.get("actions")
    return {
        "actions": [a for a in actions if isinstance(a, dict)] if isinstance(actions, list) else [],
        "refusal": obj.get("refusal"),
    }


def _envelope_actions(obj: Any) -> list[dict[str, Any]] | None:
    """Return the actions of a ``{"reply","actions"}`` envelope, or None if ``obj``
    isn't a recognizable envelope. Every action MUST carry a KNOWN ``type`` — so a
    reply that quotes a REST-shaped or made-up action is not executed (it becomes a
    plain refusal-free chat instead of running something unintended)."""
    if not isinstance(obj, dict) or ("reply" not in obj and "actions" not in obj):
        return None
    raw = obj.get("actions")
    acts = [a for a in raw if isinstance(a, dict)] if isinstance(raw, list) else []
    if acts and not all(str(a.get("type") or "") in KNOWN_ACTION_TYPES for a in acts):
        return None
    return acts


def parse_pm_reply(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Split a PM reply into ``(display_reply, actions)``. The PM answers in prose
    for a plain question, or — when it wants to DO something (create a task, start
    the team, change a setting) — includes a ``{"reply": "...", "actions": [...]}``
    envelope whose every action has a KNOWN ``type``.

    Two envelope shapes are accepted: (1) the entire trimmed body IS the JSON
    object; (2) prose followed by a single trailing ```json { … } ``` fenced block
    (the shape a model naturally produces — "I'll do X:" + the JSON). Only real
    known-type actions execute, so a prose answer that merely quotes/explains the
    schema, or a made-up REST-shaped call, stays plain chat and runs nothing."""
    import json

    def _load(s: str) -> Any:
        try:
            return json.loads(s)
        except ValueError:
            return None

    stripped = text.strip()
    # (1) entire body is the envelope.
    if stripped.startswith("{") and stripped.endswith("}"):
        acts = _envelope_actions(_load(stripped))
        if acts is not None:
            obj = _load(stripped)
            reply = str((obj or {}).get("reply") or "").strip()
            return (reply or "Done."), acts
    # (2) prose + a single trailing ```json …``` fenced envelope.
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```\s*\Z", stripped, re.DOTALL)
    if m:
        obj = _load(m.group(1))
        acts = _envelope_actions(obj)
        if acts:  # only when there are real known-type actions to run
            prose = stripped[: m.start()].strip()
            reply = str((obj or {}).get("reply") or "").strip() or prose or "Done."
            return reply, acts
    return stripped, []


def split_run_actions(
    actions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Separate ``start_run`` (route-handled — the run machinery lives in the routes
    layer) from the config-mutation actions ``apply_actions`` handles. Returns
    ``(config_actions, wants_start_run)``."""
    config = [a for a in actions
              if isinstance(a, dict) and str(a.get("type") or "") != "start_run"]
    wants_start = any(
        isinstance(a, dict) and str(a.get("type") or "") == "start_run"
        for a in actions)
    return config, wants_start


def apply_actions(store: Any, actions: list[dict[str, Any]], *,
                  available: list[dict[str, Any]], surface: str = "pop",
                  ) -> tuple[list[pm_changes.PmChange], list[dict[str, Any]]]:
    """Apply a list of structured actions; return ``(applied, refusals)``. A single
    bad action becomes a grounded refusal, never aborting the rest."""
    applied: list[pm_changes.PmChange] = []
    refusals: list[dict[str, Any]] = []
    for action in actions:
        try:
            applied.append(apply_action(store, action, available=available, surface=surface))
        except ControlActionError as exc:
            refusals.append({"code": exc.code, "reason": str(exc), **exc.extra})
        except Exception as exc:  # noqa: BLE001 — one action's failure (e.g. a
            # disk error while recording the change) must never 500 the chat route
            # or abort the remaining actions; report it as a refusal.
            refusals.append({"code": "action_failed", "reason": str(exc)})
    return applied, refusals


def _duplicate_of_open_task(
    store: Any, *, title: str, detail: str, role: str,
    target_files: list[str] | None,
) -> task_dedupe.DuplicateMatch | None:
    """Spec 08 dedupe against the live backlog, defensively: ``store`` is ``Any``
    here (the chat surface passes several shapes), so a store without
    ``list_tasks`` simply skips the gate rather than breaking task creation."""
    list_tasks = getattr(store, "list_tasks", None)
    if not callable(list_tasks):
        return None
    try:
        index = task_dedupe.build_open_index(list_tasks())
    except Exception:  # noqa: BLE001 — never block a create on a read failure
        return None
    paths = set(task_dedupe.normalized_target_paths(target_files))
    paths |= paths_mod.declared_target_paths(title, detail)
    return task_dedupe.find_duplicate(index, title=title, role=role, paths=paths)


def create_task(
    store: Any, *, title: str, detail: str = "", role: str = "dev",
    surface: str = "pop", target_files: list[str] | None = None,
) -> pm_changes.PmChange:
    """Add a task to the backlog (state ``todo``). Reversible: declining the PM
    Change drops the task off the board. A missing title is refused; an unknown
    role falls back to ``dev`` (grounded, never invents a role). F159: an optional
    ``target_files`` list lets the PM declare which files the task will touch, so
    the hot-file serializer doesn't have to infer them from prose."""
    title = str(title or "").strip()
    if not title:
        raise ControlActionError("task_title_required", "a task needs a title")
    role = str(role or "dev").strip().lower()
    if role not in _TASK_ROLES:
        role = "dev"
    detail = str(detail or "")
    # Spec 08: the second task-creation choke point gets the same dedupe gate as
    # the PM plan path. A silent no-op would be worse than the duplicate — the
    # operator would never learn why the task didn't appear — so this refuses
    # with the matched task id and the rule that fired.
    duplicate = _duplicate_of_open_task(store, title=title, detail=detail,
                                        role=role, target_files=target_files)
    if duplicate is not None:
        raise ControlActionError(
            "duplicate_task",
            f"already open as {duplicate.task_id}: {duplicate.title!r} — "
            "execute or re-scope that task instead of creating another",
            matched_task_id=duplicate.task_id,
            matched_title=duplicate.title,
            rule=duplicate.rule,
        )
    task = store.add_task(title=title, role=role, detail=detail,
                          target_files=target_files)
    return pm_changes.record_change(
        store, summary=f"Created task: {title}",
        details=[{"field": "task", "before": None, "after": task.task_id}],
        restore_target="task", restore_value={"task_id": task.task_id},
        surface=surface)


def apply_action(store: Any, action: dict[str, Any], *,
                 available: list[dict[str, Any]], surface: str = "pop") -> pm_changes.PmChange:
    """Dispatch one structured action to its typed handler."""
    kind = str(action.get("type") or "")
    if kind == "assign_models":
        rr = action.get("role_routes") or {}
        return assign_models_by_role(
            store, {str(k): str(v) for k, v in rr.items()},
            available=available, surface=surface)
    if kind == "set_autonomy":
        return set_autonomy(store, dict(action.get("knobs") or {}), surface=surface,
                            suggested_cap=action.get("suggested_cap"))
    if kind == "set_governance":
        return set_governance(store, dict(action.get("fields") or {}), surface=surface)
    if kind == "create_task":
        raw_files = action.get("target_files")
        target_files = ([str(p) for p in raw_files if p]
                        if isinstance(raw_files, (list, tuple)) else None)
        return create_task(
            store, title=str(action.get("title") or ""),
            detail=str(action.get("detail") or action.get("description") or ""),
            role=str(action.get("role") or "dev"), surface=surface,
            target_files=target_files)
    if kind == "start_run":
        # Route-handled — must never reach the config-mutation seam.
        raise ControlActionError(
            "start_run_route_only", "start_run is handled by the run route")
    raise ControlActionError("unknown_action", f"unknown action type: {kind!r}")


__all__ = [
    "ControlActionError", "resolve_route", "interpret_directive", "apply_action",
    "assign_models_by_role", "set_autonomy", "set_governance", "create_task",
    "apply_actions", "parse_pm_reply", "split_run_actions", "KNOWN_ACTION_TYPES",
]
