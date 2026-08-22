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

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

from errorta_council.coding import attention, pm_changes, team_log
from errorta_council.coding.ledger import LedgerStore
from errorta_slack import store as _slack_store

_LOGGER = logging.getLogger(__name__)


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
    "gate_blocked",    # the evidence gate refused the merge -- NOTHING landed
    "delivery_error",  # the merge landed but delivery threw -- nothing shipped
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
    "list_live_profiles": {
        "trust": "R",
        "summary": "List the operator-authored live-run profiles and whether each validates.",
    },
    "start_live_run": {
        "trust": "C",
        "args": (("profile", True, "the live-run profile name, from list_live_profiles"),),
        "summary": (
            "Launch the bound project's live run from a profile and supervise "
            "it by wall-clock."
        ),
    },
    "stop_live_run": {
        "trust": "R",
        "summary": (
            "Stop the live run now: collect evidence, log off, tear down. "
            "Never waits for approval."
        ),
    },
    "live_status": {
        "trust": "R",
        "summary": (
            "Phase, elapsed time, per-probe health ages, and teardown literals "
            "of the live run."
        ),
    },
    "resume_live_run": {
        "trust": "C",
        "args": (("profile", True, "the paused profile to clear for launching again"),),
        "summary": (
            "Clear a paused-awaiting-human hold on a profile (human approval "
            "only; autopilot never fires this)."
        ),
    },
    "accept_live_fix": {
        "trust": "C",
        # Dispatchable but NOT advertised: the live-run fix cycle stages this
        # verb itself, and it refuses any confirmation a live supervisor is not
        # currently waiting on. Rendering it in the concierge's catalog only
        # ever taught a model to compose a call that must then be refused --
        # see `concierge.build_system_prompt` and the catalog canary.
        "hidden": True,
        "args": (("project_id", True, "the coding project whose fix is being accepted"),
                 ("repo_id", False, "the profile repo the fix belongs to")),
        "summary": (
            "Merge and deliver a live-run fix the supervisor already staged. "
            "The live-run fix cycle stages this itself — do not call it from "
            "chat; it refuses anything it did not stage."
        ),
    },
    "pause_fix_loop": {
        "trust": "R",
        "args": (("profile", True, "the live-run profile to stop fixing"),),
        "summary": (
            "Stop this profile fixing: no new fix cycle starts, and a cycle "
            "already in flight is aborted — its dev run cancelled and its "
            "staged merge withdrawn. Live runs in progress keep running."
        ),
    },
    "resume_fix_loop": {
        "trust": "C",
        "args": (("profile", True, "the profile whose fix loop to re-arm"),),
        "summary": (
            "Re-arm this profile's autonomous fix loop (human approval only; "
            "autopilot never fires this)."
        ),
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


# Verbs autopilot must NEVER auto-approve (spec 2026-08-21 §3.7) -- a human
# taps these. `resume_live_run` clears a hold the supervisor put on a profile
# *because* something ban-class or cap-class happened; the hold's entire value
# is that a person looks before the client launches again. Autopilot clearing
# its own hold is a loop, not a gate. `resume_fix_loop` (spec 2026-08-22 §3.7)
# is the same shape one level in: it re-arms autonomous MERGING after a human
# turned it off. Enforced in `connection._handle_staged_confirmations` and
# `outbound.sweep_autopilot`, not in `dispatch` -- the trust class already
# makes these staged-only; this narrows WHO may confirm.
HUMAN_ONLY_VERBS: frozenset[str] = frozenset({"resume_live_run", "resume_fix_loop"})

# The one verb whose human-only answer depends on its ARGUMENTS rather than
# its name (see `is_human_only`).
ACCEPT_LIVE_FIX = "accept_live_fix"


def is_human_only(verb: str, args: dict[str, Any] | None = None) -> bool:
    """May ONLY a human confirm this staged action?

    Verb-scoped for `HUMAN_ONLY_VERBS`; args-scoped for `accept_live_fix`,
    which is the same verb whether the diff touches one game script or the
    brain's kill switch. Gating on the name alone would either hand autopilot
    the safety code or block every autonomous fix.

    Two independent sources agree or the answer is "human": the `human_only`
    flag the fix cycle computed when it staged the record, AND a re-derivation
    from the record's own `changed_paths` through
    `errorta_liverun.fixloop.is_human_only_diff` -- the same predicate, asked
    again by the process that is about to act. A record whose flag was lost (an
    older staging, a truncated write, a hand-edited confirmations.json) still
    reaches a person, and a diff this process cannot evaluate at all
    (`errorta_liverun` absent, the predicate raising) is treated as guarded.
    Fail-closed in every direction: the cost of a false "human-only" is one
    button tap.
    """
    if verb in HUMAN_ONLY_VERBS:
        return True
    if verb != ACCEPT_LIVE_FIX:
        # `human_only` is not a general-purpose escalation flag some other
        # verb's args can set; it means this one thing on this one verb.
        return False
    args = args or {}
    if bool(args.get("human_only")):
        return True
    paths = [str(p) for p in (args.get("changed_paths") or [])]
    if not paths:
        return False
    try:
        # Lazy, like every other supervisor touch in this module: importing
        # `tools` must never import `errorta_liverun`.
        from errorta_liverun.fixloop import is_human_only_diff
        from errorta_liverun.profile import profiles_dir

        return bool(is_human_only_diff(paths, profiles_dir=profiles_dir()))
    except Exception:  # noqa: BLE001 - an unanswerable diff is a guarded diff
        return True

# A profile name indexes a file on disk (`~/.errorta/liverun/profiles/<name>.
# yaml`), so it is a plain name and nothing else -- no separators, no leading
# dot, no traversal.
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

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
    # Live-run supervisor seams (spec 2026-08-21 §3.7). `None` (not the real
    # callable) for the same reason `start_run_fn` is: the default resolves
    # lazily inside the verb impl, so importing `tools` -- which every bridge
    # start does -- never imports `errorta_liverun` and its launch/subprocess
    # machinery.
    liverun_list_fn: Callable[[], list[dict[str, Any]]] | None = None
    liverun_start_fn: Callable[[str, str | None], dict[str, Any]] | None = None
    liverun_stop_fn: Callable[[str | None], dict[str, Any]] | None = None
    liverun_status_fn: Callable[[str | None], dict[str, Any]] | None = None
    liverun_resume_fn: Callable[[str], dict[str, Any]] | None = None
    liverun_pause_fix_fn: Callable[[str], dict[str, Any]] | None = None
    liverun_resume_fix_fn: Callable[[str], dict[str, Any]] | None = None
    # The three engine seams `accept_live_fix` reaches through. Same `None`
    # rule: the real `_workspace` lives in the FastAPI route layer and
    # `merge_review`/`deliver` pull in the whole evidence + delivery stack, so
    # each resolves lazily inside the verb impl. Split into three seams rather
    # than one "accept it" callable so a test can hold the merge gate open or
    # shut WITHOUT being able to replace the accept itself -- the thing the
    # gate exists to guard.
    workspace_factory: Callable[[str], Any] | None = None
    merge_review_fn: Callable[[Any, Any], dict[str, Any]] | None = None
    deliver_fn: Callable[..., dict[str, Any]] | None = None
    # "Is a LIVE supervisor waiting on exactly this acceptance?" -- called as
    # `fn(run_id, confirmation_id)`. Same `None` rule: the default reaches into
    # `errorta_liverun.supervisor` lazily, at call time. This is the seam that
    # makes `accept_live_fix` an effect bound to supervisor state rather than a
    # verb a chat turn can compose (see the verb's docstring).
    liverun_accept_binding_fn: Callable[[str, str], bool] | None = None


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
    try:
        tasks = deps.ledger_factory(project_id).list_tasks()
    except Exception as exc:  # noqa: BLE001 - never let an engine failure escape a live turn
        return {"status": "error",
                "detail": f"couldn't read the backlog ({type(exc).__name__})"}
    open_tasks = [
        t for t in tasks
        if str(getattr(t, "state", "")) not in _TERMINAL_TASK_STATES
    ]
    shown = open_tasks[:_OPEN_TASK_CAP]
    # Never a silent cap. A PM that reports 20 of 40 as if it were all of them
    # leads the operator to cancel that 20, believe the backlog is clear, and
    # hit the completion gate again on tasks they were never shown.
    return {
        "tasks": [
            {"task_id": str(t.task_id), "title": str(getattr(t, "title", "")),
             "state": str(getattr(t, "state", ""))}
            for t in shown
        ],
        "total_open": len(open_tasks),
        "truncated": len(open_tasks) > len(shown),
    }


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
    except Exception as exc:  # noqa: BLE001 - never let an engine failure escape a live turn
        return {"status": "error",
                "detail": f"couldn't cancel the task ({type(exc).__name__})"}
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
    except Exception as exc:  # noqa: BLE001 - never let an engine failure escape a live turn
        return {"status": "error",
                "detail": f"couldn't unblock the task ({type(exc).__name__})"}
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


# --- Live-run supervisor verbs --------------------------------------------


def _profile_arg(value: Any) -> str:
    name = str(value or "").strip()
    if not _PROFILE_NAME_RE.match(name):
        raise ToolError(
            "bad_profile_name",
            "profile must be a plain name (letters, digits, . _ -) naming an "
            "operator-authored profile — see list_live_profiles",
        )
    return name


def _liverun() -> Any:
    """The supervisor singleton, imported at CALL time. See ToolDeps."""
    from errorta_liverun.supervisor import live_run_manager

    return live_run_manager


def list_live_profiles(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                       deps: "ToolDeps") -> dict[str, Any]:
    if deps.liverun_list_fn is not None:
        rows = deps.liverun_list_fn()
    else:
        from errorta_liverun.profile import list_profiles

        rows = list_profiles()
    return {"status": "ok", "profiles": list(rows)}


def start_live_run(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                   deps: "ToolDeps") -> dict[str, Any]:
    project_id = _bound_project_id(deps, channel_id)
    name = _profile_arg(args.get("profile"))
    fn = deps.liverun_start_fn or (lambda p, pid: _liverun().start(p, project_id=pid))
    return fn(name, project_id)


def stop_live_run(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                  deps: "ToolDeps") -> dict[str, Any]:
    project_id = _bound_project_id(deps, channel_id)
    fn = deps.liverun_stop_fn or (lambda pid: _liverun().stop(project_id=pid))
    return fn(project_id)


def live_status(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                deps: "ToolDeps") -> dict[str, Any]:
    project_id = _bound_project_id(deps, channel_id)
    fn = deps.liverun_status_fn or (lambda pid: _liverun().status(project_id=pid))
    return fn(project_id)


def resume_live_run(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                    deps: "ToolDeps") -> dict[str, Any]:
    # A resume is scoped to a profile, not a project, but it still only
    # answers from a bound channel -- an unbound channel has no business
    # clearing another project's hold.
    _bound_project_id(deps, channel_id)
    name = _profile_arg(args.get("profile"))
    fn = deps.liverun_resume_fn or (lambda p: _liverun().resume(p))
    return fn(name)


def pause_fix_loop(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                   deps: "ToolDeps") -> dict[str, Any]:
    # R-class: turning autonomous merging OFF is subtractive, so it takes
    # effect the moment it is asked for. Live runs are untouched -- this stops
    # the FIX loop, not the supervisor.
    _bound_project_id(deps, channel_id)
    name = _profile_arg(args.get("profile"))
    fn = deps.liverun_pause_fix_fn or (lambda p: _liverun().pause_fix(p))
    return fn(name)


def resume_fix_loop(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                    deps: "ToolDeps") -> dict[str, Any]:
    # C-class AND human-only (see HUMAN_ONLY_VERBS): re-arming autonomous
    # merging is exactly the decision a loop must not make for itself.
    _bound_project_id(deps, channel_id)
    name = _profile_arg(args.get("profile"))
    fn = deps.liverun_resume_fix_fn or (lambda p: _liverun().resume_fix(p))
    return fn(name)


def _default_workspace(project_id: str) -> Any:
    # The app's own resolver: it sets the project's target (which `accept`
    # branches on) and refuses a project with no worktree -- the exact
    # preconditions the accept path needs. Imported at CALL time.
    from errorta_app.routes.coding import _workspace

    return _workspace(project_id)


def _default_merge_review(ledger_store: Any, workspace: Any) -> dict[str, Any]:
    from errorta_council.coding.evidence import merge_review

    return merge_review(ledger_store, workspace)


def _default_deliver(project_id: str, workspace: Any, **kw: Any) -> dict[str, Any]:
    from errorta_council.coding.deliverable import deliver

    return deliver(project_id, workspace, **kw)


#: What the workspace says it is about to write into the operator's tree, or
#: ``None`` when it cannot say. The merge-back preview — the same answer
#: `merge_back` itself computes — never the confirmation record.
def _merge_preview(workspace: Any) -> dict[str, Any] | None:
    try:
        return dict(workspace.preview() or {})
    except Exception:  # noqa: BLE001 - a diff we cannot read is not one we merge
        _LOGGER.exception("slack: could not re-derive the live-run fix diff")
        return None


def _delivered_paths(preview: dict[str, Any]) -> list[str]:
    entries = preview.get("changed_files") or []
    return [str((e.get("path") if isinstance(e, dict) else e) or "") for e in entries]


def _default_accept_binding(run_id: str, confirmation_id: str) -> bool:
    """Whether a non-terminal live run ``run_id`` is waiting on exactly this
    confirmation. Lazy, like every other supervisor touch here, and fail-closed:
    no supervisor package (or a manager that raises) means "not staged"."""
    try:
        return bool(_liverun().accept_is_staged(run_id, confirmation_id))
    except Exception:  # noqa: BLE001
        _LOGGER.exception("slack: could not ask the live-run manager about %s", run_id)
        return False


def _diff_is_guarded(paths: list[str]) -> bool:
    """Fail-closed: no supervisor package, or a predicate that raises, means
    the answer is "a human decides"."""
    try:
        from errorta_liverun.fixloop import is_human_only_diff
        from errorta_liverun.profile import profiles_dir

        return bool(is_human_only_diff(paths, profiles_dir=profiles_dir()))
    except Exception:  # noqa: BLE001
        return True


def accept_live_fix(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                    deps: "ToolDeps") -> dict[str, Any]:
    """C-class. Only ever reached via ``connection._fire_confirmed_effect`` (a
    button tap or the autopilot sweep) from a confirmation the live-run fix
    cycle staged.

    Mirrors ``routes/coding.accept_worktree`` MINUS its ``override``: a blocked
    merge gate returns ``gate_blocked`` and merges NOTHING. There is
    deliberately no parameter that can bypass the gate — that switch exists for
    a human at a desk, and handing it to a loop is the whole thing this slice
    must not do. (There is a grep test.)

    Bound to LIVE supervisor state, not merely to well-formed arguments: a
    non-terminal run must be waiting on THIS confirmation id
    (``deps.liverun_accept_binding_fn``). Without that, a chat turn could emit
    ``accept_live_fix`` with any ``run_id``, and — carrying no ``changed_paths``
    for ``is_human_only`` to judge — autopilot would fire it.

    The outcome is written to the project's decision log under a stable
    ``choice`` so the supervisor has a durable record of what actually
    happened: the confirmation only ever tells it the effect was *claimed*.
    Every way this can fail to land work in the operator's tree — a conflicting
    merge-back, a blocked gate, a delivery that threw — reports a status in
    ``NOT_DONE_STATUSES``. "accepted" is said only about a merge that applied
    AND a delivery that returned.
    """
    project_id = str(args.get("project_id") or "")
    if not project_id:
        raise ToolError("missing_project_id", "accept_live_fix requires args.project_id")
    ledger_store = deps.ledger_factory(project_id)
    run_id = str(args.get("run_id") or "").strip()
    repo_id = str(args.get("repo_id") or "")
    if not run_id:
        # This verb is in the catalog because `dispatch` can only route
        # catalogued verbs -- which also means the concierge can compose a call
        # to it. A merge into the operator's real files is not something a chat
        # turn gets to invent: with no run id there is no fix cycle this accept
        # belongs to, and no supervisor waiting on it.
        return _record_fix_accept(ledger_store, {
            "status": "refused", "reason": "not_supervisor_staged",
            "repo_id": repo_id, "run_id": ""})
    # ...and a run id alone is a string a model can also emit. Bind the effect
    # to LIVE supervisor state: a non-terminal run with this id must be waiting
    # on THIS confirmation. The id is minted by `stage_confirmation` after the
    # cycle built the args and is threaded in here by
    # `connection._fire_confirmed_effect`, so it is the one part of this call a
    # chat turn cannot supply.
    if not (deps.liverun_accept_binding_fn or _default_accept_binding)(
            run_id, str(args.get("_confirmation_id") or "")):
        return _record_fix_accept(ledger_store, {
            "status": "refused", "reason": "not_staged_by_supervisor",
            "repo_id": repo_id, "run_id": run_id})
    # `existing` is the only target with a repository to merge back INTO: a
    # `new`-target project's accept hands back the worktree root and delivery
    # exports a folder, so the operator's tree never changes and the fix cycle
    # would deploy work that never landed. (`fixloop._do_triage` refuses these
    # projects too -- this is the same question asked at the merge itself.)
    proj = ledger_store.get_project()
    if str(getattr(proj, "target", "") or "") != "existing":
        return _record_fix_accept(ledger_store, {
            "status": "refused", "reason": "project_not_existing",
            "repo_id": repo_id, "run_id": run_id})
    workspace = (deps.workspace_factory or _default_workspace)(project_id)
    # Re-derive the diff from the WORKSPACE before anything else touches the
    # operator's files. `is_human_only` answered from the staged record, which
    # is a JSON file written minutes ago: it can be stale, truncated
    # (`fixloop.MAX_STAGED_PATHS`) or hand-edited. This asks the tree itself
    # what the merge is about to deliver, and a guarded answer the record did
    # not declare is a mismatch, not a merge -- the button that was posted said
    # something else. `human_only: True` records ARE merged: that flag means a
    # person was asked, and a person is who got here.
    preview = _merge_preview(workspace)
    if preview is None:
        return _record_fix_accept(ledger_store, {
            "status": "refused", "reason": "diff_unreadable",
            "repo_id": repo_id, "run_id": run_id})
    conflicts = [str(p) for p in (preview.get("conflicts") or [])]
    if conflicts:
        # `merge_back` is fail-closed on conflicts, and it says so by RETURNING
        # `applied: False` -- it does not raise. Asking here as well means the
        # cycle is told "error" before the merge is even attempted, instead of
        # after a no-op the caller could mistake for a merge.
        return _record_fix_accept(ledger_store, {
            "status": "error", "reason": "conflicts", "conflicts": conflicts[:50],
            "repo_id": repo_id, "run_id": run_id})
    delivered = _delivered_paths(preview)
    if _diff_is_guarded(delivered) and not bool(args.get("human_only")):
        return _record_fix_accept(ledger_store, {
            "status": "refused", "reason": "guarded_path_mismatch",
            "repo_id": repo_id, "run_id": run_id})
    review = (deps.merge_review_fn or _default_merge_review)(ledger_store, workspace)
    if not review["_gate"].allowed:
        return _record_fix_accept(ledger_store, {
            "status": "gate_blocked", "gate": review.get("gate"),
            "repo_id": repo_id, "run_id": run_id})
    result = dict(workspace.accept(confirm=True) or {})
    if result.get("applied") is False:
        # THE failure this whole verb exists to report honestly.
        # `CodingWorkspace.accept` -> `merge_back` returns `{"applied": False,
        # "reason": "conflicts" | "unsafe_path"}` on a refusal; it raises
        # nothing. Falling through to `deliver` and recording "accepted" would
        # tell the supervisor a merge landed that never touched a byte -- and it
        # would then deploy and relaunch on a fix that does not exist. `is
        # False`, not falsy: a `new`-target accept returns no `applied` key at
        # all, and that is not a failure.
        return _record_fix_accept(ledger_store, {
            "status": "error", "reason": str(result.get("reason") or "conflicts"),
            "conflicts": [str(p) for p in (result.get("conflicts") or [])][:50],
            "repo_id": repo_id, "run_id": run_id})
    try:
        delivery = dict((deps.deliver_fn or _default_deliver)(
            project_id, workspace,
            target=proj.target, repo_path=proj.repo_path,
            delivery_root=proj.delivery_root if proj.target != "existing" else None) or {})
    except Exception as exc:  # noqa: BLE001 - the merge already happened
        # Same shape as `routes/coding.accept_worktree`: delivery is downstream
        # of a merge that has already landed, so it cannot be retried by
        # raising. It is still NOT a done accept -- `delivery_error` is in
        # `NOT_DONE_STATUSES`, so the cycle pauses and the channel says so
        # rather than announcing a fix that shipped nowhere.
        _LOGGER.exception("slack: live-run fix merged but delivery failed")
        return _record_fix_accept(ledger_store, {
            "status": "delivery_error", "reason": type(exc).__name__,
            "detail": str(exc)[:200], "repo_id": repo_id, "run_id": run_id,
            **result})
    return _record_fix_accept(ledger_store, {
        "status": "accepted", "repo_id": repo_id, "run_id": run_id,
        **result, **delivery})


def _record_fix_accept(ledger_store: Any, outcome: dict[str, Any]) -> dict[str, Any]:
    """Write ``outcome`` to the decision log and return it unchanged.

    Best-effort by design: the merge has already happened by the time this
    runs, so an unwritable ledger must not turn a landed fix into an exception
    the caller reports as a failure.
    """
    try:
        ledger_store.record_decision(
            title=f"live-run fix {outcome['status']}",
            context="live-run fix loop", choice="accept_live_fix",
            rationale=f"repo={outcome.get('repo_id') or '-'} "
                      f"run={outcome.get('run_id') or '-'}",
            related_task_ids=[],
            extra={"status": outcome["status"], "repo_id": outcome.get("repo_id") or "",
                   "run_id": outcome.get("run_id") or "",
                   "delivered_to": outcome.get("delivered_to") or ""})
    except Exception:  # noqa: BLE001 - see the docstring
        _LOGGER.exception("slack: could not record the accept_live_fix outcome")
    return outcome


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
    "list_live_profiles": list_live_profiles,
    "start_live_run": start_live_run,
    "stop_live_run": stop_live_run,
    "live_status": live_status,
    "resume_live_run": resume_live_run,
    "accept_live_fix": accept_live_fix,
    "pause_fix_loop": pause_fix_loop,
    "resume_fix_loop": resume_fix_loop,
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


__all__ = ["ToolError", "ToolDeps", "TOOL_CATALOG", "NOT_DONE_STATUSES",
           "HUMAN_ONLY_VERBS", "ACCEPT_LIVE_FIX", "is_human_only", "dispatch"]
