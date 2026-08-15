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

from dataclasses import dataclass, field
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


@dataclass
class ToolDeps:
    """Every engine seam ``dispatch`` reaches through — all injectable so
    tests run egress-free with fakes."""

    store: Any = _slack_store
    ledger_factory: Callable[[str], Any] = LedgerStore
    launch_fn: Callable[[str], dict[str, Any] | None] = _default_launch_fn
    publish_fn: Callable[[dict[str, Any]], dict[str, Any]] = _default_publish_fn
    pm_changes_mod: Any = pm_changes


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
    return {"tasks": tasks, "blockers": blockers}


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
        cid = deps.store.stage_confirmation(verb, safe_args, thread_ts)
        return {"status": "needs_confirmation", "confirmation_id": cid}
    impl = _VERB_IMPLS[verb]
    return impl(safe_args, channel_id=channel_id, thread_ts=thread_ts, deps=deps)


__all__ = ["ToolError", "ToolDeps", "TOOL_CATALOG", "dispatch"]
