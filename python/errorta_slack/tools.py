"""The bounded tool surface — the ONLY door from the concierge to the engine.

Two non-negotiable rules live here:

1. **Grant-or-refuse.** ``dispatch`` accepts only the verbs listed in
   ``TOOL_CATALOG``. An unknown/unlisted verb raises :class:`ToolError` with
   code ``"tool_not_allowed"`` and a message naming the real catalog — fail
   closed, mirroring ``errorta_council.coding.turn_controller.
   allowed_tools_for_role``'s rejection wording (name the allowed set, don't
   just say "no").
2. **Injection guard.** ``resolve_decision`` and every **C**-class verb
   (``spend_cloud``, ``publish_pr``) execute their real effect ONLY when
   ``dispatch`` is called with ``confirmed_via="block_actions"`` — the
   provenance marker set by the verified Slack interaction callback (Task 8),
   never by concierge text output. With ``confirmed_via=None`` (what the
   concierge always passes when acting on chat text) the call is staged via
   ``store.stage_confirmation`` and returns ``{"status": "needs_confirmation",
   "confirmation_id": ...}`` instead of running. This is what stops untrusted
   pasted Slack text ("approve the pending request") from spending money or
   opening a PR.

``TOOL_CATALOG`` is the single source of truth: the concierge system prompt
renders it, and the Task 11 anti-drift canary compares its verb set against
what ``dispatch`` actually accepts. ``resolve_decision`` is catalogued as
trust ``"C"`` (not a third value — ``ToolSpec`` only allows ``"R"``/``"C"``)
because it needs the identical confirmation gate as the two true C-class
verbs; the design doc's "R‡" notation collapses to plain "C" here so the
catalog and the enforcement switch never drift apart.

This module MUST NOT import ``slack_sdk`` (or anything else optional) at
module load, and MUST NOT execute real engine side effects at import time —
every engine seam is reached through the injectable :class:`ToolDeps`, whose
callable fields default to real (but lazily-imported) implementations.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from errorta_council.coding import attention, pm_changes, team_log
from errorta_council.coding.ledger import LedgerStore
from errorta_slack import store as _slack_store


class ToolError(Exception):
    """Raised when a verb cannot be executed. ``.code`` is a stable string."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


# --------------------------------------------------------------------------
# Catalog — the single source of truth for verbs, trust class, and prompt copy
# --------------------------------------------------------------------------

TOOL_CATALOG: dict[str, dict[str, str]] = {
    "list_projects": {
        "trust": "R",
        "summary": "List the coding projects this bridge knows about.",
    },
    "switch_project": {
        "trust": "R",
        "summary": "Rebind this channel to a different coding project.",
    },
    "project_status": {
        "trust": "R",
        "summary": "Show the bound project's task log and open blockers.",
    },
    "recent_activity": {
        "trust": "R",
        "summary": "Show what the team did recently.",
    },
    "launch_runtime": {
        "trust": "R",
        "summary": "Start the bound project's runtime preview (local URL only).",
    },
    "stop_runtime": {
        "trust": "R",
        "summary": "Stop the bound project's runtime preview.",
    },
    "queue_bugs": {
        "trust": "R",
        "summary": "File one or more bug reports as new dev tasks.",
    },
    "answer_question": {
        "trust": "R",
        "summary": "Answer a question from context already fetched (no side effect).",
    },
    "resolve_decision": {
        "trust": "C",
        "summary": "Accept or decline an already-surfaced PM change / gate decision.",
    },
    "spend_cloud": {
        "trust": "C",
        "summary": "Authorize an action that spends on cloud model calls.",
    },
    "publish_pr": {
        "trust": "C",
        "summary": "Open or update a public pull request.",
    },
    "start_run": {
        "trust": "C",
        "summary": (
            "Start the coding team working on the project (spends model "
            "calls up to the iteration cap) — NOT the runtime preview."
        ),
    },
    "stop_run": {
        "trust": "R",
        "summary": (
            "Gracefully stop the running coding team — NOT the runtime "
            "preview."
        ),
    },
}


# --------------------------------------------------------------------------
# Real (lazily-imported) defaults for the engine-side seams
# --------------------------------------------------------------------------


def _default_launch_fn(project_id: str) -> dict[str, Any] | None:
    """Start the project's runtime and report its loopback host/port.

    Lazily imports ``runtime_process``/``ledger`` so this module never pulls
    in that heavy process-management machinery at import time. Returns a
    result dict shaped as one of:

    * ``None`` — no runtime configured (kept for callers/tests that pass a
      trivial ``lambda project_id: None`` fake).
    * ``{"status": "empty", "reason": ...}`` — no ledger project, no
      worktree, or no ``RuntimeProfile`` declared; nothing was started.
    * ``{"status": "error", "detail": ...}`` — the manager raised trying to
      start the profile (e.g. no start command, setup required).
    * ``{"host": ..., "port": ...}`` — genuinely running.

    ``RuntimeSession`` (``errorta_council.coding.runtime``) has no ``.port``
    attribute — the allocated port(s) live in ``allocated_ports: list[int]``;
    the first entry is used as the preview port.
    """
    from errorta_council.coding.ledger import LedgerError
    from errorta_council.coding.runtime_process import (
        RuntimeProcessError,
        RuntimeProcessManager,
    )

    try:
        mgr = RuntimeProcessManager.for_project(project_id)
        profiles = mgr.rstore.list_profiles()
        if not profiles:
            return {"status": "empty"}
        session = mgr.start(profiles[0].profile_id, auto_setup=False)
    except LedgerError as exc:
        # e.g. ProjectNotFound — no ledger record for this project yet.
        return {"status": "empty", "reason": str(exc)}
    except RuntimeProcessError as exc:
        # e.g. no_worktree, profile_not_found, no start command configured.
        return {"status": "error", "detail": str(exc)}
    ports = list(session.allocated_ports or [])
    if not ports:
        return {"status": "empty"}
    return {"host": "127.0.0.1", "port": ports[0]}


def _default_publish_fn(args: dict[str, Any]) -> dict[str, Any]:
    """No real GitHub-publish wiring ships in this task.

    ``publish_pr``'s actual call needs a resolved ``CodingWorkspace`` + task
    context that ``tools.py`` does not have from ``channel_id``/``args``
    alone (see ``errorta_council.coding.publish_github``). Rather than
    silently no-op an irreversible action, the real default fails loud;
    production wiring supplies a concrete ``deps.publish_fn`` once the bot
    loop assembles that context.
    """
    raise ToolError(
        "publish_pr_not_configured",
        "publish_pr has no engine wiring configured (deps.publish_fn) — "
        "refusing to no-op an irreversible action",
    )


def _default_start_run(
    project_id: str, *, resume: bool = False, continue_: bool = False,
) -> dict[str, Any]:
    """Start the coding team's run via the app's real start-run route
    function — ``_start_run(project_id, {}, resume=resume,
    continue_=continue_)``, the identical call the app's own PM path makes
    (``routes/coding.py:1846``) once the caller (``start_run`` below) has
    picked the right mode from the project's current run state.

    Lazily imports ``errorta_app.routes.coding`` so this module never pulls
    in the FastAPI route layer at import time (optionality — ``tools.py``
    must load with only ``errorta_council`` installed). Raises straight
    through on failure (an ``HTTPException`` with ``status_code`` 409 for
    "already in progress" / member-health preflight / run-setup-required, or
    any other exception) — the caller is responsible for turning that into a
    clean result.
    """
    from errorta_app.routes.coding import _start_run

    return _start_run(project_id, {}, resume=resume, continue_=continue_)


@dataclass
class ToolDeps:
    """Every engine seam ``dispatch`` reaches through — all injectable so
    tests run egress-free with fakes."""

    store: Any = _slack_store
    ledger_factory: Callable[[str], Any] = LedgerStore
    launch_fn: Callable[[str], dict[str, Any] | None] = _default_launch_fn
    publish_fn: Callable[[dict[str, Any]], dict[str, Any]] = _default_publish_fn
    # None (not a bound default) so the lazy `_default_start_run` import of
    # `errorta_app.routes.coding` only happens on first real use, never at
    # ToolDeps-construction time — mirrored by `deps.start_run_fn or
    # _default_start_run` at the call site in `start_run` below. Called as
    # `start_run_fn(project_id, resume=<bool>, continue_=<bool>)` — the mode
    # is picked by `start_run` from the project's current run_state, not
    # hardcoded here.
    start_run_fn: Callable[..., dict[str, Any]] | None = None
    pm_changes_mod: Any = pm_changes
    # Task 9's outbound.py stages `attention_signal`-class confirmations
    # (not a tools.TOOL_CATALOG verb) whose Approve/Decline click is resolved
    # by connection.handle_interaction through THIS seam, not tools.dispatch
    # — see outbound.attention_decision_action for the decision -> action
    # mapping. Defaults to the real attention.resolve so production wiring
    # needs no extra plumbing; tests inject a spy/fake.
    attention_resolve_fn: Callable[..., Any] = attention.resolve


# --------------------------------------------------------------------------
# Verb implementations — each takes (args, *, channel_id, thread_ts, deps)
# --------------------------------------------------------------------------


def _bound_project_id(deps: "ToolDeps", channel_id: str) -> str:
    binding = deps.store.binding_for(channel_id)
    if binding is None:
        raise ToolError("no_project_bound", "this channel is not bound to a project")
    return str(binding["project_id"])


def list_projects(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                   deps: "ToolDeps") -> dict[str, Any]:
    seen: list[str] = []
    for binding in deps.store.list_bindings():
        pid = binding.get("project_id")
        if pid and pid not in seen:
            seen.append(pid)
    return {"projects": seen}


def switch_project(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                    deps: "ToolDeps") -> dict[str, Any]:
    project_id = args.get("project_id")
    if not project_id:
        raise ToolError("missing_project_id", "switch_project requires args.project_id")
    deps.store.bind_channel(channel_id, str(project_id))
    return {"status": "switched", "project_id": str(project_id)}


def project_status(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                    deps: "ToolDeps") -> dict[str, Any]:
    project_id = _bound_project_id(deps, channel_id)
    ledger_store = deps.ledger_factory(project_id)
    tasks = team_log.build_team_log(ledger_store)
    blockers = attention.list_open(project_id, store=ledger_store)
    run_status = ledger_store.get_run_state().get("status") or "idle"
    return {"tasks": tasks, "blockers": blockers, "run_status": run_status}


def recent_activity(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                     deps: "ToolDeps") -> dict[str, Any]:
    project_id = _bound_project_id(deps, channel_id)
    ledger_store = deps.ledger_factory(project_id)
    entries = team_log.build_team_log(ledger_store)
    limit = int(args.get("limit", 10) or 10)
    return {"activity": entries[-limit:]}


def launch_runtime(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                    deps: "ToolDeps") -> dict[str, Any]:
    project_id = _bound_project_id(deps, channel_id)
    result = deps.launch_fn(project_id)
    if not result:
        return {"status": "empty"}
    status = result.get("status")
    if status == "empty":
        out: dict[str, Any] = {"status": "empty"}
        if result.get("reason"):
            out["reason"] = str(result["reason"])
        return out
    if status == "error":
        out = {"status": "error"}
        if result.get("detail"):
            out["detail"] = str(result["detail"])
        return out
    host = result.get("host", "127.0.0.1")
    port = result.get("port")
    if port is None:
        return {"status": "empty"}
    return {
        "status": "running",
        "url": f"http://{host}:{port}",
        "note": "local URL only — no public URL in v1",
    }


def stop_runtime(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                  deps: "ToolDeps") -> dict[str, Any]:
    project_id = _bound_project_id(deps, channel_id)
    from errorta_council.coding.ledger import LedgerError
    from errorta_council.coding.runtime_process import (
        RuntimeProcessError,
        RuntimeProcessManager,
    )

    try:
        mgr = RuntimeProcessManager.for_project(project_id)
        profiles = mgr.rstore.list_profiles()
        if not profiles:
            return {"status": "empty"}
        mgr.stop(profiles[0].profile_id)
    except LedgerError as exc:
        return {"status": "empty", "reason": str(exc)}
    except RuntimeProcessError as exc:
        return {"status": "error", "detail": str(exc)}
    return {"status": "stopped"}


def queue_bugs(args: dict[str, Any], *, channel_id: str, thread_ts: str,
               deps: "ToolDeps") -> dict[str, Any]:
    bug_texts = args.get("bugs") or args.get("bug_texts") or []
    project_id = _bound_project_id(deps, channel_id)
    ledger_store = deps.ledger_factory(project_id)
    task_ids: list[str] = []
    for bug in bug_texts:
        task = ledger_store.add_task(
            title=str(bug), role="dev", detail=str(bug), task_type="implementation",
        )
        task_ids.append(task.task_id)
    return {"task_ids": task_ids}


def answer_question(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                     deps: "ToolDeps") -> dict[str, Any]:
    # Pure Q&A grounded in context already fetched by an earlier hop — no
    # side effect, no engine call.
    return {"status": "ok", "question": str(args.get("question", ""))}


def resolve_decision(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                      deps: "ToolDeps") -> dict[str, Any]:
    change_id = args.get("change_id")
    if not change_id:
        raise ToolError("missing_change_id", "resolve_decision requires args.change_id")
    raw_decision = args.get("decision")
    decision = "accept" if raw_decision in (None, "") else str(raw_decision)
    if decision not in ("accept", "decline"):
        raise ToolError(
            "invalid_decision",
            f"resolve_decision.decision must be 'accept' or 'decline', got {decision!r}",
        )
    project_id = _bound_project_id(deps, channel_id)
    ledger_store = deps.ledger_factory(project_id)
    if decision == "decline":
        deps.pm_changes_mod.decline(ledger_store, str(change_id))
    else:
        deps.pm_changes_mod.accept(ledger_store, str(change_id))
    return {"status": "resolved", "decision": decision, "change_id": str(change_id)}


def spend_cloud(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                 deps: "ToolDeps") -> dict[str, Any]:
    # v1: no cloud-spend gateway/budget engine is wired yet (not a named seam
    # for this task — see Task 4 brief). Once confirmed via block_actions this
    # acknowledges the authorized spend; real gateway integration is later work.
    return {
        "status": "authorized",
        "amount": args.get("amount"),
        "reason": str(args.get("reason", "")),
    }


def publish_pr(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                deps: "ToolDeps") -> dict[str, Any]:
    result = deps.publish_fn(dict(args))
    out: dict[str, Any] = {"status": "published"}
    if isinstance(result, dict):
        out.update(result)
    return out


def _classify_start_exception(exc: Exception) -> dict[str, Any]:
    """Turn a ``start_run_fn`` failure into a clean, redacted result.

    Mirrors the app's own real path (``routes/coding.py:1854``), which
    string-matches ``"already in progress" in detail`` rather than treating
    every 409 as benign — a 409 can also mean ``run_setup_required`` (fresh
    start, team not confirmed), ``member_health_preflight_failed`` (a
    provider is logged out — only reachable on a FRESH start, since
    resume/continue skip the preflight per ``routes/coding.py:2586``), or
    ``run is not continuable`` / ``run is not recoverable`` (a stale/wrong
    mode was picked). Only the first of these is safe to swallow as
    "already running"; the rest are real "can't start" outcomes and must
    say so, not lie that the team is already working.
    """
    status_code = getattr(exc, "status_code", None)
    if status_code != 409:
        # Redacted: only the exception TYPE is surfaced, never its message
        # (which may carry tokens/paths/internal detail) — metadata-only log.
        return {"status": "error", "detail": f"couldn't start the run ({type(exc).__name__})"}
    detail = getattr(exc, "detail", None)
    code = detail.get("code") if isinstance(detail, dict) else None
    if code == "member_health_preflight_failed":
        reason = str((detail or {}).get("message") or "a provider looks logged out")
        return {
            "status": "error",
            "detail": f"can't start — a model/CLI provider looks logged out: {reason}",
        }
    if code == "run_setup_required":
        return {"status": "error", "detail": "the team isn't configured yet"}
    detail_text = detail if isinstance(detail, str) else str(detail or "")
    if "already in progress" in detail_text:
        return {"status": "already_running"}
    return {"status": "error", "detail": f"couldn't start the run ({type(exc).__name__})"}


def start_run(args: dict[str, Any], *, channel_id: str, thread_ts: str,
               deps: "ToolDeps") -> dict[str, Any]:
    """C-class — this impl only ever runs once ``dispatch`` has confirmed
    ``confirmed_via="block_actions"``; never reachable from concierge text.

    Picks the start mode from the project's CURRENT run state rather than
    always passing ``continue_=True`` — ``continue_`` only works on a run
    whose status is ``"stopped"`` (``routes/coding.py:2620`` raises 409
    "run is not continuable" otherwise). A fresh Slice-1 project has never
    run (status ``"idle"``), so its first "start building" needs a genuine
    fresh start (``resume=False, continue_=False``), not continue.

    ``deps.start_run_fn`` defaults to ``None`` on ``ToolDeps`` (not the lazy
    wrapper itself) precisely so the real engine import stays deferred to
    this call, not to ``ToolDeps()`` construction.
    """
    project_id = _bound_project_id(deps, channel_id)
    ledger_store = deps.ledger_factory(project_id)
    status = ledger_store.get_run_state().get("status") or "idle"
    if status == "running":
        # Check the ledger FIRST rather than relying on the route's 409 —
        # avoids a redundant call and reads the same status project_status
        # already surfaces.
        return {"status": "already_running"}
    resume = status == "interrupted"
    continue_ = status == "stopped"
    start_fn = deps.start_run_fn or _default_start_run
    try:
        start_fn(project_id, resume=resume, continue_=continue_)
    except Exception as exc:  # noqa: BLE001 - any engine failure -> clean result, never an uncaught raise
        return _classify_start_exception(exc)
    return {"status": "started"}


def stop_run(args: dict[str, Any], *, channel_id: str, thread_ts: str,
             deps: "ToolDeps") -> dict[str, Any]:
    project_id = _bound_project_id(deps, channel_id)
    ledger_store = deps.ledger_factory(project_id)
    current_status = ledger_store.get_run_state().get("status") or "idle"
    if current_status == "idle":
        return {"status": "not_running"}
    ledger_store.set_run_state(cancel_requested=True)
    return {"status": "stopping"}


_VERB_IMPLS: dict[str, Callable[..., dict[str, Any]]] = {
    "list_projects": list_projects,
    "switch_project": switch_project,
    "project_status": project_status,
    "recent_activity": recent_activity,
    "launch_runtime": launch_runtime,
    "stop_runtime": stop_runtime,
    "queue_bugs": queue_bugs,
    "answer_question": answer_question,
    "resolve_decision": resolve_decision,
    "spend_cloud": spend_cloud,
    "publish_pr": publish_pr,
    "start_run": start_run,
    "stop_run": stop_run,
}

assert set(_VERB_IMPLS) == set(TOOL_CATALOG), "TOOL_CATALOG and _VERB_IMPLS drifted"


# --------------------------------------------------------------------------
# dispatch — the ONLY entry point the concierge is allowed to call
# --------------------------------------------------------------------------


def dispatch(verb: str, args: dict[str, Any], *, channel_id: str, thread_ts: str,
             confirmed_via: str | None = None, deps: "ToolDeps") -> dict[str, Any]:
    spec = TOOL_CATALOG.get(verb)
    if spec is None:
        allowed = ", ".join(sorted(TOOL_CATALOG)) or "none"
        raise ToolError(
            "tool_not_allowed",
            f"tool_not_allowed: {verb!r} — this bridge executes only: {allowed}",
        )
    safe_args = dict(args or {})
    if spec["trust"] == "C" and confirmed_via != "block_actions":
        # channel_id is threaded onto the staged record (Task 9) so the
        # background timeout sweep -- which has no live Slack payload to read
        # a channel off of -- can still post its auto-decided outcome back to
        # the right channel.
        cid = deps.store.stage_confirmation(verb, safe_args, thread_ts, channel_id=channel_id)
        return {"status": "needs_confirmation", "confirmation_id": cid}
    impl = _VERB_IMPLS[verb]
    return impl(safe_args, channel_id=channel_id, thread_ts=thread_ts, deps=deps)


__all__ = ["ToolError", "ToolDeps", "TOOL_CATALOG", "dispatch"]
