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

# Every result status meaning the verb did NOT do the thing it names. Read by
# `concierge` (may the reply claim success?) and by `connection` (may the
# outcome message say "executed"?).
#
# ONE definition, deliberately. This started as two copies -- one in each of
# those modules -- and when "empty"/"not_running" were added to one, the other
# kept announcing "Autopilot approved & executed start_run" for a no-op that
# started nothing. A comment telling the next person to keep them in step is
# what failed; a single name cannot drift.
NOT_DONE_STATUSES = frozenset({
    "error",           # the verb tried and failed
    "refused",         # a gate declined it before it ran
    "already_running", # a start on a run that was already going -- a no-op
    "not_running",     # a stop with nothing to stop -- a no-op
    "empty",           # nothing to act on (e.g. no runtime configured)
})


TOOL_CATALOG: dict[str, dict[str, Any]] = {
    "list_projects": {
        "trust": "R",
        "summary": "List the coding projects this bridge knows about.",
    },
    "switch_project": {
        "trust": "R",
        "args": (("project_id", True, "the project to switch this channel to"),),
        "summary": "Rebind this channel to a different coding project.",
    },
    "project_status": {
        "trust": "R",
        "summary": "Show the bound project's task log and open blockers.",
    },
    "recent_activity": {
        "trust": "R",
        "args": (("limit", False, "how many entries (default 10)"),),
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
    "set_updates": {
        "trust": "R",
        "args": (("on", False, "true to resume updates, false to mute"),),
        "summary": (
            "Turn this channel's progress updates on or off. Muting never "
            "silences the run stopping, a roadblock, or the run finishing."
        ),
    },
    "list_open_tasks": {
        "trust": "R",
        "summary": (
            "List the project's still-open tasks with their ids — the ids "
            "cancel_task and unblock_task need."
        ),
    },
    "cancel_task": {
        "trust": "C",
        "args": (("task_id", True, "id from list_open_tasks"),),
        "summary": "Cancel (drop) an open task so a blocked run can complete.",
    },
    "unblock_task": {
        "trust": "C",
        "args": (("task_id", True, "id from list_open_tasks"),),
        "summary": "Move a blocked task back to todo so the team can pick it up.",
    },
    "queue_bugs": {
        "trust": "R",
        "args": (("bugs", True, "list of bug descriptions, one string each"),),
        "summary": "File one or more bug reports as new dev tasks.",
    },
    "answer_question": {
        "trust": "R",
        "args": (("question", True, "the question to answer"),),
        "summary": "Answer a question from context already fetched (no side effect).",
    },
    "resolve_decision": {
        "trust": "C",
        "args": (("change_id", True, "the pending change id"),
                 ("decision", True, "\"approved\" or \"declined\"")),
        "summary": "Accept or decline an already-surfaced PM change / gate decision.",
    },
    "spend_cloud": {
        "trust": "C",
        "args": (("amount", True, "spend cap in dollars"),
                 ("reason", True, "why this spend is needed")),
        "summary": "Authorize an action that spends on cloud model calls.",
    },
    "publish_pr": {
        "trust": "C",
        "args": (("title", True, "PR title"), ("body", False, "PR description")),
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
    "reconfigure_team": {
        "trust": "R",
        "args": (("role_routes", True, "map of role -> gateway route id, "
                  "e.g. {\"dev\": \"claude_cli.opus\"}"),),
        "summary": (
            "Change which model a role (pm/dev/reviewer/tester) uses on "
            "this project."
        ),
    },
    "set_next_goal": {
        "trust": "C",
        "args": (("title", True, "the goal, one line"),
                 ("body", False, "any extra scope detail")),
        "summary": (
            "Set the team's next goal — the operative scope they plan "
            "against right now (the North Star stays a reference guardrail)."
        ),
    },
    "set_north_star": {
        "trust": "C",
        "args": (("north_star", True, "the durable charter"),
                 ("definition_of_done", False, "how you will know it is met")),
        "summary": (
            "Rewrite the project's North Star / definition of done (the "
            "durable charter, not the current goal)."
        ),
    },
    "propose_next_goal": {
        "trust": "R",
        "summary": (
            "Read the project's actual repo, docs and recent commits and "
            "propose the team's next goal (proposal only — writes nothing)."
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

    body: dict[str, Any] = {}
    if not (resume or continue_):
        # Fresh start: ``_start_run`` recovers the project's saved team ONLY on
        # resume/continue (``routes/coding.py`` ~:2562), so a fresh start with
        # an empty body fails with "no members" — which is exactly why a Slack
        # "start building" on a brand-new studio project silently never ran.
        # Pass the project's already-confirmed team explicitly, the same way
        # the desktop frontend does on its first Start Run.
        from errorta_council.coding.ledger import LedgerStore

        cfg = LedgerStore(project_id).get_run_config()
        members = [m for m in (cfg.get("members") or []) if isinstance(m, dict)]
        if members:
            body = {"members": members}
    return _start_run(project_id, body, resume=resume, continue_=continue_)


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
    # Injected in tests so `reconfigure_team` never probes the real gateway
    # (which may hit Ollama). `None` (not `[]`) means "not injected" — the
    # verb impl calls `pm_reference.list_available_routes()` lazily on first
    # use, at most once per turn, never at ToolDeps-construction time.
    available_routes: list[dict[str, Any]] | None = None
    # Slice 4 §3.2: the repo-grounded goal proposer. `None` (not the real
    # helper) so `next_goal`'s repo_reader import and its `git log` subprocess
    # stay deferred to first real use, never ToolDeps() construction — the
    # same reason `start_run_fn` defaults to None. Called as
    # `propose_goal_fn(ledger_store, member=..., caller=...)`.
    propose_goal_fn: Callable[..., dict[str, Any]] | None = None
    # The model seam `propose_goal_fn` calls. `concierge.run_turn` sets this to
    # the same PM-member caller it uses for its own turn, so the proposal is
    # routed through the team's configured gateway route. `None` means no model
    # is wired up — `propose_next_goal` refuses cleanly rather than crashing.
    goal_caller: Callable[[dict[str, Any], str], str] | None = None


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


def set_updates(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                 deps: "ToolDeps") -> dict[str, Any]:
    """Mute or unmute this channel's routine progress updates.

    [R] rather than [C]: it spends nothing and is trivially reversible from the
    same chat that set it. The three events the operator requires -- the team
    stopping, a roadblock, the run finishing -- ignore the mute entirely
    (``outbound.poll_once``), so this can never leave someone unaware that a run
    has ended or is blocked waiting on them.

    Accepts the argument as ``on`` (updates on/off). Absent or non-bool means
    "turn them on": the mute is the surprising state, so an ambiguous request
    must not land there.
    """
    raw = args.get("on")
    enabled = raw if isinstance(raw, bool) else True
    deps.store.set_updates(channel_id, enabled=enabled)
    return {"status": "updated", "updates": "on" if enabled else "off"}


# Terminal task states -- a task in one of these is finished work, not open work.
_TERMINAL_TASK_STATES = frozenset({"done", "dropped", "merged"})
_OPEN_TASK_CAP = 20


def list_open_tasks(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                     deps: "ToolDeps") -> dict[str, Any]:
    """The project's non-terminal tasks, WITH their ids.

    Exists because `cancel_task`/`unblock_task` take a `task_id` and nothing the
    PM could see carried one: `project_status`'s "tasks" are team-log entries
    (`at`/`kind`/`member`/`message`/`role`), not task records. Without this the
    other two verbs would be advertised and uncallable -- the same defect as a
    verb whose arguments were never declared.
    """
    project_id = _bound_project_id(deps, channel_id)
    tasks = deps.ledger_factory(project_id).list_tasks()
    open_tasks = [
        t for t in tasks
        if str(getattr(t, "state", "")) not in _TERMINAL_TASK_STATES
    ]
    return {"tasks": [
        {"task_id": str(t.task_id), "title": str(getattr(t, "title", "")),
         "state": str(getattr(t, "state", ""))}
        for t in open_tasks[:_OPEN_TASK_CAP]
    ]}


def cancel_task(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                 deps: "ToolDeps") -> dict[str, Any]:
    """Drop a task so a completion-blocked run can finish.

    [C], not [R]: `queue_bugs` is [R] because it only ADDS work; this SUBTRACTS
    it and so directly steers what the team spends on. `state="dropped"` is the
    runner's own cancel semantic (runner.py:1603), so a cancel from Slack and a
    cancel from the engine leave indistinguishable ledger state.
    """
    from errorta_council.coding.ledger import LedgerError  # noqa: PLC0415

    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        return {"status": "error", "detail": "task_id is required"}
    ledger = deps.ledger_factory(_bound_project_id(deps, channel_id))
    try:
        ledger.update_task(task_id, state="dropped")
    except LedgerError:
        return {"status": "error", "detail": f"unknown task: {task_id}"}
    return {"status": "cancelled", "task_id": task_id}


def unblock_task(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                  deps: "ToolDeps") -> dict[str, Any]:
    """Move a BLOCKED task back to todo -- the "human-required" half of the
    completion gate.

    Refuses any other state on purpose. This is an unblock, not a general state
    setter: allowing it to move a `done` task back into the queue would let a
    misread instruction resurrect finished work.
    """
    from errorta_council.coding.ledger import LedgerError  # noqa: PLC0415

    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        return {"status": "error", "detail": "task_id is required"}
    ledger = deps.ledger_factory(_bound_project_id(deps, channel_id))
    current = next(
        (t for t in ledger.list_tasks() if str(t.task_id) == task_id), None)
    if current is None:
        return {"status": "error", "detail": f"unknown task: {task_id}"}
    state = str(getattr(current, "state", ""))
    if state != "blocked":
        return {"status": "error",
                "detail": f"task {task_id} is {state}, not blocked"}
    try:
        ledger.update_task(task_id, state="todo")
    except LedgerError:
        return {"status": "error", "detail": f"unknown task: {task_id}"}
    return {"status": "unblocked", "task_id": task_id}


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

    A FRESH run with no active Focus and no legacy ``work_request`` is REFUSED
    (``next_goal.start_gate``) rather than started: its PM would plan from the
    North Star alone, which on a project whose charter has gone stale spends
    real budget re-litigating finished work.

    The gate applies to fresh starts ONLY. Its whole rationale is about fresh
    planning; a resume or a continue picks up work the team has already
    planned and partly done. Gating those would strand a project whose only
    Focus was archived on completion and whose run was then interrupted
    mid-task: it could not be resumed from Slack at all until someone set a
    new goal, which changes what the resumed run is building.
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
    if not resume and not continue_:
        # Slice 4 §3.4: refuse to spend on a fresh run with no operative goal.
        # Shared with studio_tools.adopt_project (also a fresh start) via
        # next_goal.start_gate so the two start paths cannot drift.
        from errorta_council.coding import next_goal

        refusal = next_goal.start_gate(ledger_store)
        if refusal:
            return {"status": "refused", "detail": refusal}
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


def _control_action_error_detail(exc: Any) -> str:
    """Turn a ``control_actions.ControlActionError`` into a single detail
    string carrying both the reason and any candidate/available models —
    the grounded-or-refuse info the PM relays to the user (e.g. "I don't
    have a model matching 'gpt5' — available: ..."). ``resolve_route``
    stashes candidates under ``.extra["candidates"]`` (ambiguous match) or
    ``.extra["available"]`` (no match at all); ``no_matching_members``/
    ``no_team`` carry neither, so those stay a clean bare message."""
    detail = str(exc)
    extra = getattr(exc, "extra", None) or {}
    candidates = extra.get("candidates") or extra.get("available")
    if candidates:
        detail = f"{detail} — available: {', '.join(str(c) for c in candidates)}"
    return detail


def reconfigure_team(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                      deps: "ToolDeps") -> dict[str, Any]:
    """R-class — applies immediately (reversible, undoable PmChange); the
    reply announces the change. ``role_routes`` (coding role -> human model
    name, e.g. ``{"reviewer": "opus"}``) comes straight from the concierge's
    parsed args. Grounded-or-refuse against the live/injected model catalog
    via ``control_actions.assign_models_by_role`` — an unresolvable or
    ambiguous model, or a role with no member, becomes a clean error naming
    the reason and any candidates, never a crash or a silent no-op.
    """
    role_routes = args.get("role_routes") or {}
    if not role_routes:
        return {
            "status": "error",
            "detail": "tell me which role → which model, e.g. reviewer → opus",
        }
    from errorta_council.coding import control_actions, pm_reference

    project_id = _bound_project_id(deps, channel_id)
    available = (
        deps.available_routes if deps.available_routes is not None
        else pm_reference.list_available_routes()
    )
    ledger_store = deps.ledger_factory(project_id)
    try:
        control_actions.assign_models_by_role(
            ledger_store, {str(k): str(v) for k, v in role_routes.items()},
            available=available,
        )
    except control_actions.ControlActionError as exc:
        return {"status": "error", "detail": _control_action_error_detail(exc)}
    except Exception as exc:  # noqa: BLE001 - any engine failure -> clean result, never an uncaught raise
        return {"status": "error", "detail": f"couldn't reconfigure ({type(exc).__name__})"}
    return {"status": "reconfigured", "changes": dict(role_routes)}


def set_next_goal(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                   deps: "ToolDeps") -> dict[str, Any]:
    """C-class — writes the team's operative scope, so it directly steers real
    spend; only ever reached once ``dispatch`` saw
    ``confirmed_via="block_actions"``.

    Writes a **Focus** (F137), not the north star: ``runner._pm_prompt`` reads
    ``store.active_focuses()`` and pins "Plan ONLY these, in order" while
    demoting the north star to "REFERENCE ONLY — not a list of things to build
    now" (runner.py:3160-3167). A north-star write would be near-inert here.

    Reversibility is Focus's own lifecycle (``update_focus`` -> ``archived``,
    ledger.py:1763), not a new ``pm_changes`` restore target —
    ``RESTORE_TARGETS`` (pm_changes.py:26) has no focus slot and widening it is
    a larger cross-surface change than this earns.
    """
    from errorta_council.coding.ledger import LedgerError

    title = str(args.get("title") or "").strip()
    body = str(args.get("body") or "")
    project_id = _bound_project_id(deps, channel_id)
    try:
        focus = deps.ledger_factory(project_id).add_focus(
            title=title, body=body, origin="slack_pm")
    except LedgerError as exc:
        # The known, safe-to-surface shape: an empty title. Message carries no
        # secrets (ledger.py:1721).
        return {"status": "error", "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001 - never let an engine failure escape a live turn
        return {"status": "error", "detail": f"couldn't set the goal ({type(exc).__name__})"}
    return {
        "status": "goal_set",
        "focus_id": getattr(focus, "id", ""),
        "title": getattr(focus, "title", title),
    }


def set_north_star(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                    deps: "ToolDeps") -> dict[str, Any]:
    """C-class — rewrites the durable charter; only ever reached once
    ``dispatch`` saw ``confirmed_via="block_actions"``.

    Writes through ``LedgerStore.promote_north_star`` (ledger.py:1878) — the
    ONLY lock-held authoritative writer, which bumps ``revision``.
    Deliberately NOT the ``PUT /north-star`` route (routes/coding.py:4175),
    whose unlocked read-modify-write against the private ``_project_path`` can
    lose-update against a concurrent run write.

    Passes ``already_met=False``. That writer's forward-only
    ``north_star_met_at`` stamp exists for the F141 import flow, where the
    North Star being accepted was *inferred from* an already-built codebase
    and so is already true by construction. A human naming a NEW purpose from
    Slack is the opposite case: on an adopted (``target == "existing"``)
    project the default stamp would record a goal with zero code behind it as
    met and flip the project straight into the ``"steering"`` phase.

    Refuses mid-run, mirroring ``accept_north_star_proposal``'s 409
    (routes/coding.py:4598-4599): rewriting the charter under a live run
    changes what the team is building mid-flight.

    An omitted/empty ``definition_of_done`` PRESERVES the stored one rather
    than blanking it — the in-app modal only ever sends the north star
    (src/features/coding/index.tsx:1177-1183), so a blanking default would
    silently destroy the DoD.
    """
    north_star = str(args.get("north_star") or "").strip()
    if not north_star:
        return {"status": "error", "detail": "north_star is required"}
    project_id = _bound_project_id(deps, channel_id)
    ledger_store = deps.ledger_factory(project_id)
    try:
        if (ledger_store.get_run_state().get("status") or "idle") == "running":
            return {
                "status": "error",
                "detail": "can't rewrite the north star mid-run — stop the run first",
            }
        dod = str(args.get("definition_of_done") or "").strip()
        if not dod:
            dod = str(ledger_store.get_project().definition_of_done or "")
        project = ledger_store.promote_north_star(north_star, dod, already_met=False)
    except Exception as exc:  # noqa: BLE001 - never let an engine failure escape a live turn
        return {
            "status": "error",
            "detail": f"couldn't set the north star ({type(exc).__name__})",
        }
    return {"status": "north_star_set", "revision": getattr(project, "revision", 0)}


def propose_next_goal(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                       deps: "ToolDeps") -> dict[str, Any]:
    """R-class — reads the project's real repo and returns a PROPOSED next
    goal. Writes nothing, which is why it may run straight from chat text.

    The proposal reaches the ledger only through ``set_next_goal``, whose
    confirmation renders the full title and body so a human reads the exact
    text before it becomes the team's scope. That two-step split is what makes
    reading untrusted repo content safe here.

    The model route is resolved from the project's persisted run config, NEVER
    from ``args``. ``args`` is whatever the concierge model emitted, which is
    ultimately derived from chat text including anything a user pasted; since
    this verb is R-class it runs with no confirmation at all, so honouring an
    ``args["member"]`` would let pasted text pick a paid cloud gateway route
    and make a billed call — exactly the decision the C-class ``spend_cloud``
    verb exists to gate. Every other model call on the bridge resolves its
    member from the run config; so does this one.
    """
    project_id = _bound_project_id(deps, channel_id)
    ledger_store = deps.ledger_factory(project_id)
    propose_fn = deps.propose_goal_fn
    if propose_fn is None:
        from errorta_council.coding.next_goal import propose_next_goal as _propose

        propose_fn = _propose
    caller = deps.goal_caller
    if caller is None:
        return {"status": "error", "detail": "no model is wired up for this bridge yet"}
    # Lazy: concierge imports this module at module load, so the reverse edge
    # has to be deferred to call time. One implementation, not a third copy.
    from errorta_slack.concierge import _resolve_pm_member

    pm = _resolve_pm_member(ledger_store)
    if pm is None or not str(pm.get("gateway_route_id") or "").strip():
        return {
            "status": "error",
            "detail": "the team's PM model isn't configured yet — set it and try again",
        }
    member = {**pm, "project_id": project_id}
    try:
        proposal = propose_fn(ledger_store, member=member, caller=caller)
    except Exception as exc:  # noqa: BLE001 - never let an engine failure escape a live turn
        return {
            "status": "error",
            "detail": f"couldn't read the project ({type(exc).__name__})",
        }
    if not str(proposal.get("title") or "").strip():
        return {
            "status": "no_proposal",
            "detail": (
                "I couldn't ground a next goal in what's in the repo — "
                "tell me the goal and I'll set it."
            ),
        }
    return {
        "status": "proposed",
        "title": proposal.get("title", ""),
        "body": proposal.get("body", ""),
        "evidence": list(proposal.get("evidence") or []),
        "stale": bool(proposal.get("stale")),
    }


_VERB_IMPLS: dict[str, Callable[..., dict[str, Any]]] = {
    "list_projects": list_projects,
    "switch_project": switch_project,
    "project_status": project_status,
    "recent_activity": recent_activity,
    "launch_runtime": launch_runtime,
    "stop_runtime": stop_runtime,
    "list_open_tasks": list_open_tasks,
    "cancel_task": cancel_task,
    "unblock_task": unblock_task,
    "set_updates": set_updates,
    "queue_bugs": queue_bugs,
    "answer_question": answer_question,
    "resolve_decision": resolve_decision,
    "spend_cloud": spend_cloud,
    "publish_pr": publish_pr,
    "start_run": start_run,
    "stop_run": stop_run,
    "reconfigure_team": reconfigure_team,
    "set_next_goal": set_next_goal,
    "set_north_star": set_north_star,
    "propose_next_goal": propose_next_goal,
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


__all__ = ["ToolError", "ToolDeps", "TOOL_CATALOG", "NOT_DONE_STATUSES", "dispatch"]
