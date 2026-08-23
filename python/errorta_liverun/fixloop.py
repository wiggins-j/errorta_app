"""The fix cycle: one live-run failure, one repository, one dev run (spec §3.5).

``FixCycle`` is a poll-driven state machine —

    triage -> task -> run -> watch -> stage -> await -> deploy -> done

— whose ``step()`` the supervisor calls once per tick. It never sleeps and owns
no thread of its own: every wait is a deadline on the injected clock, so the
whole cycle is driven against a fake clock in tests and `Supervisor.stop()`
still interrupts it between ticks.

Every engine touch goes through a ``FixDeps`` seam whose production default is
resolved LAZILY, at call time. Importing this module must not pull in the
council package or the FastAPI route layer (there is a test).

Two invariants this module exists to hold:

* **The merge gate is never bypassed.** Acceptance is *staged* as a C-class
  confirmation and fired by Slack (a human tap, or the autopilot sweep) — this
  module never accepts, never merges, and passes no parameter that could let
  anything else skip the gate. A blocked gate is a paused cycle.
* **Nothing composed by a model reaches an argv.** Triage picks a repo id from
  an enumeration; the brief is template-generated; deploy argvs come byte-for-byte
  from the validated profile through ``steps.run_action``'s identity guard.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from . import brief as _brief
from . import profile as _profile
from . import triage as _triage

_LOG = logging.getLogger("errorta.liverun")

# How often the idle detector re-reads the project's run state + team log. The
# fingerprint is compared on WALL time, so a slow tick cannot fake progress.
POLL_S = 30.0
# After `cancel_requested`, how long we wait for the run to actually go terminal
# before giving up and pausing anyway (spec §3.5 step 6).
CANCEL_WAIT_S = 120.0
# How long the just-started run has to actually show up as running. Until it
# does, a TERMINAL status is the status it already had before we started it (a
# `continue_` start begins from "stopped"), not a run that finished.
RUN_START_GRACE_S = 120.0
TERMINAL_RUN_STATUSES = frozenset({"stopped", "failed"})
# Must outlast a single repository-read dev turn, which may run for
# ERRORTA_REPO_READ_TIMEOUT_S (async_claude_cli.py, default 1500 s).
DEFAULT_IDLE_TIMEOUT_S = 2400.0
# STRICTLY below Slack's own confirmation sweep window
# (`config.DEFAULT_CONFIG["timeout_minutes"] = 30` -> 1800 s, applied by
# `outbound.sweep_timeouts`). The cycle must be the one that withdraws its own
# staged acceptance: if the sweep claims it first the record is `timed_out`
# rather than `declined`, and for the window in between it is still PENDING —
# which is exactly what `sweep_autopilot` fires on. This is also an upper BOUND,
# not just a default: a profile may lower `accept_timeout_s`, never raise it
# past this (see `FixCycle.__init__`).
DEFAULT_ACCEPT_TIMEOUT_S = 1500.0
ACCEPT_VERB = "accept_live_fix"
ACCEPT_WITHDRAW_DECISION = "declined"
MAX_STAGED_PATHS = 50

#: Every code this driver can pause a cycle with. Declared as data so the
#: supervisor, the Slack renderer and the tests all read one list.
PAUSE_CODES = (
    "triage_ambiguous",     # no repo, or two, and no PM turn resolved it
    "repo_not_fixable",     # triage named a repo with no registrable gate
    "fix_no_gate",          # the project cannot produce a gate signal at all
    "fix_project_not_existing",  # the project has no real repo to merge back into
    "fix_project_busy",     # something else owns that project's run
    "fix_run_failed",       # the dev run could not start, or failed empty
    "fix_idle",             # the dev run went quiet and was cancelled
    "fix_no_delivery",      # a clean stop that delivered nothing is not a fix
    "fix_unsafe_paths",     # a delivered path escaped the repository
    "fix_gate_blocked",     # the merge gate said no; nothing was merged
    "fix_accept_unverified",  # approved, but the delivered work is not there
    "fix_declined",         # the confirmation was declined or never answered
    "ban_signal",           # a ban-class string surfaced during deploy
)

#: Repo-relative prefixes whose modification always needs a human tap, autopilot
#: or not (spec §3.7). These are the paths that decide whether the loop can be
#: stopped at all: the brain's safety and kill-switch code, and the supervisor
#: package itself. The profiles directory is matched separately, as an absolute
#: path — a delivered file may not live there at all.
GUARDED_PATH_PREFIXES: tuple[str, ...] = (
    "senditai_ng/safety/",
    "senditai_ng/dispatch/killswitch",
    "errorta_liverun/",
)


# --------------------------------------------------------------------------- #
# Path predicates
# --------------------------------------------------------------------------- #
def _normalize(path: str) -> str | None:
    """A delivered path as a clean repo-relative POSIX path, or ``None`` if it
    is not one (absolute, escaping, or empty). Fail-closed by construction:
    every caller treats ``None`` as the dangerous answer."""
    text = str(path or "").strip()
    if not text:
        return None
    p = PurePosixPath(text)
    if p.is_absolute():
        return None
    parts: list[str] = []
    for part in p.parts:
        if part in ("", "."):
            continue
        if part == "..":
            return None          # never resolve upwards: an escape is an escape
        parts.append(part)
    return "/".join(parts) or None


def escapes_repo(paths: Iterable[str]) -> bool:
    """True if ANY path leaves the repository root. Such a diff is refused
    outright — not merely routed to a human — because a delivered file outside
    the tree is not a repository change at all."""
    return any(_normalize(p) is None for p in paths or ())


def is_human_only_diff(paths: Iterable[str], *, profiles_dir: Path | str) -> bool:
    """True if this diff may only be accepted by a human tap.

    Prefix-matched on the normalized POSIX path, plus the absolute profiles
    directory. Anything that will not normalize (absolute, escaping, empty)
    answers True as well: this predicate is the last thing between an
    autonomous merge and the operator's real files, so an input it cannot
    reason about is never waved through."""
    root = str(profiles_dir or "")
    root = root if root.endswith("/") else root + "/"
    for raw in paths or ():
        text = str(raw or "").strip()
        if root != "/" and text.startswith(root):
            return True
        norm = _normalize(text)
        if norm is None:
            return True
        for prefix in GUARDED_PATH_PREFIXES:
            if norm.startswith(prefix):
                return True
    return False


# --------------------------------------------------------------------------- #
# Seams
# --------------------------------------------------------------------------- #
def _default_ledger_factory(project_id: str):
    from errorta_council.coding.ledger import LedgerStore
    return LedgerStore(project_id)


def _default_workspace_factory(project_id: str):
    # The app's own resolver (routes/coding.py:3883): it sets the project's
    # target and refuses a project with no worktree, which is exactly the
    # precondition the accept path needs.
    from errorta_app.routes.coding import _workspace
    return _workspace(project_id)


def _default_gate_available(store: Any) -> bool:
    from errorta_council.coding import gate_state
    return bool(gate_state.gate_available(store))


def _default_merge_gate_ok(store: Any, workspace: Any) -> bool:
    """Is the evidence gate open right now? Read-only: ``merge_review`` inspects,
    it never merges. This is the SAME question ``accept_live_fix`` asks before it
    merges anything, asked here so the cycle can pause on a blocked gate instead
    of staging a confirmation that could only fail."""
    from errorta_council.coding.evidence import merge_review
    return bool(merge_review(store, workspace)["_gate"].allowed)


def _default_start_run(project_id: str, *, resume: bool, continue_: bool) -> dict[str, Any]:
    """Delegates to Slack's own default rather than copying it: that function
    (``errorta_slack.tools._default_start_run``) already carries the fresh-start
    team recovery a bare ``_start_run(project_id, {})`` is missing, and one
    implementation cannot drift from itself."""
    from errorta_slack.tools import _default_start_run as slack_start
    return slack_start(project_id, resume=resume, continue_=continue_)


def _default_team_log(store: Any) -> list[dict[str, Any]]:
    from errorta_council.coding import team_log
    return team_log.build_team_log(store)


def _default_stage_confirmation(verb: str, args: dict[str, Any], thread_ts: str, *,
                                channel_id: str = "") -> str:
    from errorta_slack import store as slack_store
    return slack_store.stage_confirmation(verb, args, thread_ts, channel_id=channel_id)


def _default_get_confirmation(cid: str) -> dict[str, Any] | None:
    from errorta_slack import store as slack_store
    return slack_store.get_confirmation(cid)


def _default_resolve_confirmation(cid: str, decision: str) -> tuple[dict[str, Any], bool]:
    """The store's atomic claim, used here only to WITHDRAW: a staged acceptance
    the cycle is no longer waiting on must not be left pending, or the autopilot
    sweep will merge and deliver it minutes after the cycle paused."""
    from errorta_slack import store as slack_store
    return slack_store.resolve_confirmation(cid, decision)


def _default_accept_outcome(cid: str) -> dict[str, Any] | None:
    """What the accept effect recorded, read back off the project's own
    decision log — the durable half of the answer.

    Keyed *through* the confirmation record rather than off the cid directly: a
    decision row carries no cid, because `errorta_slack.tools.accept_live_fix`
    never learns the id of the confirmation that authorized it (the id is
    minted by `stage_confirmation` after the args are built). The record does
    know the project and the run, and `(choice, run_id)` identifies the row.

    ``None`` means "nothing recorded" — the caller then falls back to
    re-reading the workspace, never to assuming the merge happened.
    """
    try:
        record = _default_get_confirmation(cid)
        args = dict((record or {}).get("args") or {}) if isinstance(record, dict) else {}
        project_id = str(args.get("project_id") or "")
        run_id = str(args.get("run_id") or "")
        if not project_id or not run_id:
            return None
        store = _default_ledger_factory(project_id)
        # Newest first: a project fixed twice in one day has one row per cycle,
        # and this cycle's is the last one written for this run id.
        for row in reversed(list(store.list_decisions() or ())):
            if not isinstance(row, dict):
                continue
            if str(row.get("choice") or "") != ACCEPT_VERB:
                continue
            if str(row.get("run_id") or "") != run_id:
                continue
            return {"status": str(row.get("status") or ""),
                    "repo_id": str(row.get("repo_id") or ""),
                    "run_id": run_id,
                    "delivered_to": str(row.get("delivered_to") or "")}
    except Exception:  # noqa: BLE001 - an unreadable log is silence, not a verdict
        _LOG.exception("liverun could not read the accept outcome for %s", cid)
    return None


def _default_bound_channel(project_id: str) -> str:
    from errorta_slack import store as slack_store
    return slack_store.channel_for_project(project_id) or ""


def _default_assign_dev_route(project_id: str, route: str) -> list[str]:
    """Seat every `dev` member of `project_id`'s team on `route`.

    Computes "needs a change" the same way `assign_models_by_role` does
    (control_actions.py ~122-125): route mismatch OR `model_mode != "single"`
    -- a multi-mode dev already parked on `route`'s `gateway_route_id` still
    needs reseating to single-mode, and comparing routes alone would miss it.
    Calling the action when nothing would change raises
    `ControlActionError("no_matching_members")`, so this checks BEFORE
    calling it: "nothing to seat" (no team yet, or every dev already
    single-mode on `route`) is a silent no-op here, not a pause.
    """
    from errorta_council.coding import control_actions, pm_reference
    from errorta_council.coding.ledger import LedgerStore
    store = LedgerStore(project_id)
    members = [m for m in (store.get_run_config().get("members") or []) if isinstance(m, dict)]
    devs = [m for m in members if control_actions.coding_role_of(m) == "dev"]
    needs_change = [
        m for m in devs
        if str(m.get("gateway_route_id") or "") != route
        or str(m.get("model_mode") or "single") != "single"
    ]
    if not needs_change:
        return []
    prior = [str(m.get("gateway_route_id") or "") or "unset" for m in needs_change]
    control_actions.assign_models_by_role(
        store, {"dev": route}, available=pm_reference.list_available_routes(),
        # "liverun" is not a member of `pm_changes.SURFACES` (`("pop", "log")`)
        # -- passing it raised `PmChangeError` on every call that actually
        # changed anything, caught nowhere but `_do_triage`'s `except
        # Exception`, so it silently paused `fix_run_failed` instead of ever
        # seating a dev. This is an engine-initiated action during an already
        # -running autonomous cycle, not a user-typed Slack command: "log"
        # (Team-Log only, no Accept/Decline card) is the same surface a PM's
        # own autonomous turn uses, so that's what fits here.
        surface="log")
    return prior


def _default_seed_workspace(project_id: str) -> bool:
    # `adopt_project` never seeds; the first `errorta run` does, via
    # CodingRunner.__init__. A fix cycle that arrives first must not pause on
    # a missing worktree it could have created (live 2026-08-22, README item 10).
    from errorta_council.coding.ledger import LedgerStore
    from errorta_council.coding.workspace import CodingWorkspace
    store = LedgerStore(project_id)
    proj = store.get_project()
    ws = CodingWorkspace(project_id, store)
    ws.set_target(proj.target)
    if ws.exists():
        return False          # never re-stamp seed_head on a worked tree
    ws.setup(target=proj.target, repo_path=proj.repo_path)
    return True


@dataclass
class FixDeps:
    """Every engine seam the cycle reaches through. ``None`` means "resolve the
    production default lazily, at call time" — so this module imports with
    neither the council package nor the route layer present.

    ``triage_fn`` has no production default on purpose: there is no synchronous
    PM-turn seam in the app today, and a cycle that cannot ask stays *ambiguous*
    and pauses for a human. That is the fail-closed direction.
    """
    ledger_factory: Callable[[str], Any] | None = None
    workspace_factory: Callable[[str], Any] | None = None
    gate_available_fn: Callable[[Any], bool] | None = None
    merge_gate_ok_fn: Callable[[Any, Any], bool] | None = None
    start_run_fn: Callable[..., Any] | None = None
    team_log_fn: Callable[[Any], list] | None = None
    stage_confirmation_fn: Callable[..., str] | None = None
    get_confirmation_fn: Callable[[str], Any] | None = None
    resolve_confirmation_fn: Callable[[str, str], Any] | None = None
    #: What the accept effect actually did. `errorta_slack.tools.
    #: accept_live_fix` writes it through `LedgerStore.record_decision`, and
    #: `_default_accept_outcome` reads that row back. This is the only DURABLE
    #: proof the merge landed: `ws.head()` does not move when `deliver` copies
    #: the merged tree out, so the head check downstream is a floor, not a
    #: verdict.
    accept_outcome_fn: Callable[[str], Any] | None = None
    triage_fn: Callable[[str, str, str], str] | None = None
    bound_channel_fn: Callable[[str], str] | None = None
    assign_dev_route_fn: Callable[[str, str], list[str]] | None = None
    seed_workspace_fn: Callable[[str], bool] | None = None

    def ledger(self, project_id: str) -> Any:
        return (self.ledger_factory or _default_ledger_factory)(project_id)

    def workspace(self, project_id: str) -> Any:
        return (self.workspace_factory or _default_workspace_factory)(project_id)

    def gate_available(self, store: Any) -> bool:
        return bool((self.gate_available_fn or _default_gate_available)(store))

    def merge_gate_ok(self, store: Any, workspace: Any) -> bool:
        return bool((self.merge_gate_ok_fn or _default_merge_gate_ok)(store, workspace))

    def start_run(self, project_id: str, *, resume: bool, continue_: bool) -> Any:
        return (self.start_run_fn or _default_start_run)(
            project_id, resume=resume, continue_=continue_)

    def team_log(self, store: Any) -> list:
        return list((self.team_log_fn or _default_team_log)(store) or [])

    def stage_confirmation(self, verb: str, args: dict[str, Any], thread_ts: str, *,
                           channel_id: str) -> str:
        return (self.stage_confirmation_fn or _default_stage_confirmation)(
            verb, args, thread_ts, channel_id=channel_id)

    def get_confirmation(self, cid: str) -> Any:
        return (self.get_confirmation_fn or _default_get_confirmation)(cid)

    def resolve_confirmation(self, cid: str, decision: str) -> Any:
        return (self.resolve_confirmation_fn or _default_resolve_confirmation)(cid, decision)

    def accept_outcome(self, cid: str) -> Any:
        """``None`` when nothing was recorded — the caller then falls back to
        reading the workspace itself, never to assuming it worked."""
        return (self.accept_outcome_fn or _default_accept_outcome)(cid)

    def bound_channel(self, project_id: str) -> str:
        return str((self.bound_channel_fn or _default_bound_channel)(project_id) or "")

    def assign_dev_route(self, project_id: str, route: str) -> list[str]:
        """Seat every `dev` member on `route`. Returns the routes it replaced
        (empty when every dev seat already sat there)."""
        return list((self.assign_dev_route_fn or _default_assign_dev_route)(project_id, route) or [])

    def seed_workspace(self, project_id: str) -> bool:
        """Create the project's worktree if there is none. True when it did."""
        return bool((self.seed_workspace_fn or _default_seed_workspace)(project_id))


@dataclass
class FixOutcome:
    """What one ``step()`` did.

    ``kind``: ``pending`` (still working), ``accepted`` (the delivered work
    landed; the cycle is now deploying), ``paused`` (the supervisor must
    ``_pause(code)``), ``deployed`` (the cycle is complete and the relaunch may
    happen), ``aborted`` (a stop arrived mid-cycle; everything the cycle started
    has been stopped and everything it staged has been withdrawn). ``failed`` says whether the launch ledger should count a FAILED fix
    cycle — the driver decides that, because only it knows whether a pause
    consumed a cycle or merely declined to start one."""
    kind: str
    code: str = ""
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    failed: bool = False
    detail: str = ""


class FixCycle:
    """One fix cycle. Poll-driven: no sleeps, no threads, no I/O in ``__init__``."""

    def __init__(self, bundle: _brief.EvidenceBundle, profile: _profile.Profile,
                 repo: Any, deps: FixDeps, *, run_id: str, project_id: str | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time,
                 idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
                 accept_timeout_s: float = DEFAULT_ACCEPT_TIMEOUT_S,
                 cycle: int = 0, ctx: Any = None,
                 run_action: Callable[..., Any] | None = None,
                 run_check: Callable[..., bool] | None = None,
                 ban_scan: Callable[..., bool] | None = None,
                 profiles_dir: Path | None = None) -> None:
        self.bundle = bundle
        self.profile = profile
        self.deps = deps
        self.run_id = run_id
        self.project_id = project_id or ""
        self.cycle = int(cycle)
        self._clock, self._wall = clock, wall
        self._idle_timeout_s = float(idle_timeout_s)
        # Never above the module bound: the cycle has to withdraw its own staged
        # acceptance before Slack's timeout sweep can claim it.
        self._accept_timeout_s = min(float(accept_timeout_s), DEFAULT_ACCEPT_TIMEOUT_S)
        self._ctx = ctx
        self._run_action = run_action
        self._run_check = run_check
        self._ban_scan = ban_scan
        self._profiles_dir = profiles_dir
        self._repo = repo
        self._state = "triage"
        self._events: list[tuple[str, dict[str, Any]]] = []
        self._store: Any = None
        self._ws: Any = None
        self._head_before = ""
        self.task_id: str | None = None
        self.focus_id: str = ""
        self.cid: str = ""          # cleared the moment it is resolved either way
        self.accept_cid: str = ""   # what was staged, for the record
        self._run_started = False
        self._run_status = ""
        self._run_started_at = 0.0
        self._saw_running = False
        self._fingerprint: tuple | None = None
        self._last_progress = 0.0
        self._next_poll = 0.0
        self._cancelled_at: float | None = None
        self._accept_deadline = 0.0
        self._deploy_i = 0
        self._deploy_started: float | None = None
        self._deploy_started_wall = 0.0
        self._deploy_action_done = False

    # -- what the supervisor reads ---------------------------------------- #
    @property
    def repo_id(self) -> str | None:
        return getattr(self._repo, "id", None)

    @property
    def phase(self) -> str:
        return {"stage": "accepting", "await": "accepting",
                "deploy": "deploying", "done": "deploying"}.get(self._state, "fixing")

    @property
    def run_started(self) -> bool:
        """Whether this cycle started a coding run that may still be going."""
        return self._run_started

    # -- the tick ---------------------------------------------------------- #
    def step(self) -> FixOutcome:
        self._events = []
        return getattr(self, f"_do_{self._state}")()

    # -- outcome helpers --------------------------------------------------- #
    def _event(self, kind: str, detail: dict[str, Any]) -> None:
        self._events.append((kind, detail))

    def _pending(self, code: str = "") -> FixOutcome:
        return FixOutcome("pending", code, self._events)

    def _pause(self, code: str, *, failed: bool, detail: str = "") -> FixOutcome:
        # EVERY exit that is not an approval takes the staged acceptance with it.
        self._withdraw_accept()
        self._state = "paused"
        return FixOutcome("paused", code, self._events, failed=failed, detail=detail)

    def _do_paused(self) -> FixOutcome:
        # Terminal for this object: the supervisor has already paused the run.
        return FixOutcome("paused", "", [], failed=False)

    def _do_aborted(self) -> FixOutcome:
        return FixOutcome("aborted", "fix_aborted", [], failed=False)

    def _withdraw_accept(self) -> bool:
        """Claim this cycle's own pending confirmation as ``declined``.

        A staged acceptance nobody is waiting on is not inert: the autopilot
        sweep fires on PENDING records, so leaving one behind turns a paused or
        stopped cycle into an autonomous merge + deliver minutes later. The
        store's resolve IS the atomic claim, so losing the race to a human tap
        (or to the timeout sweep) is reported, never fought."""
        cid, self.cid = self.cid, ""
        if not cid:
            return False
        result = self._safe(self.deps.resolve_confirmation, cid, ACCEPT_WITHDRAW_DECISION,
                            default=None)
        claimed = bool(result[1]) if isinstance(result, tuple) and len(result) == 2 else False
        self._event("fix_accept_withdrawn", {"cid": cid, "claimed": claimed,
                                             "decision": ACCEPT_WITHDRAW_DECISION})
        return claimed

    def abort(self, reason: str) -> FixOutcome:
        """A stop arrived mid-cycle. Leave nothing running and nothing pending.

        Called by the supervisor from its stopping path, on the same thread, so
        it can rely on `step()` not being in flight. Idempotent."""
        self._events = []
        if self._state == "aborted":
            return FixOutcome("aborted", "fix_aborted", [], failed=False, detail=reason)
        cancelled = False
        if self._run_started and self._status() not in TERMINAL_RUN_STATUSES:
            # The dev run outlives this supervisor unless it is told to stop —
            # through the one cancel signal Slack's `stop_run` also uses.
            cancelled = self._safe(
                lambda: self._store.set_run_state(cancel_requested=True), default=None) is not None
        withdrawn = self._withdraw_accept()
        at_state, self._state = self._state, "aborted"
        self._event("fix_aborted", {"reason": reason, "repo_id": self.repo_id,
                                    "at": at_state, "run_cancelled": cancelled,
                                    "accept_withdrawn": withdrawn,
                                    "task_id": self.task_id or ""})
        return FixOutcome("aborted", "fix_aborted", self._events, failed=False, detail=reason)

    # -- 1. triage --------------------------------------------------------- #
    def _do_triage(self) -> FixOutcome:
        if self._repo is None:
            res = _triage.classify(self.bundle, self.profile)
            repo_id, rationale, confidence = res.repo_id, res.rationale, res.confidence
            if repo_id is None:
                repo_id, rationale = self._ask_pm(rationale)
            self._event("fix_triage", {"classes": list(res.classes), "repo_id": repo_id,
                                       "confidence": confidence, "rationale": rationale})
            if repo_id is None:
                return self._pause("triage_ambiguous", failed=False, detail=rationale)
            self._repo = self.profile.repo_by_id(repo_id)
        if self._repo is None:
            return self._pause("triage_ambiguous", failed=False,
                               detail="triage named a repo this profile does not declare")
        if not getattr(self._repo, "fixable", False):
            return self._pause("repo_not_fixable", failed=False,
                               detail=f"repo `{self.repo_id}` has no registrable gate")
        project_id = str(self._repo.errorta_project)
        try:
            self._store = self.deps.ledger(project_id)
        except Exception as exc:  # noqa: BLE001 - an unreachable ledger is a pause, not a crash
            _LOG.exception("liverun %s could not open ledger for %s", self.run_id, project_id)
            return self._pause("fix_no_gate", failed=False, detail=type(exc).__name__)
        # A `new`-target project has no source tree to merge back into: its
        # `accept` returns the worktree root as a deliverable and `deliver`
        # exports a folder. Nothing lands in `repo.path`, so the deploy steps
        # would rsync a tree the fix never touched -- and the whole cycle would
        # report a fix that does not exist. Fail-closed: a target this cannot
        # read at all is not one it merges into either.
        target = self._safe(
            lambda: str(getattr(self._store.get_project(), "target", "") or ""), default="")
        if target != "existing":
            return self._pause(
                "fix_project_not_existing", failed=False,
                detail=f"project `{project_id}` target is `{target or 'unreadable'}`, "
                       "not `existing` — there is no repository to merge into")
        if not self._safe(self.deps.gate_available, self._store, default=False):
            # Before ANY task is filed: work no gate can accept is work that
            # cannot be merged, and filing it would spend a dev run to prove it.
            return self._pause("fix_no_gate", failed=False,
                               detail=f"project `{project_id}` has no registrable gate")
        status = self._status()
        if status == "running":
            return self._pause("fix_project_busy", failed=False, detail=status)
        # Seating the dev is a DURABLE team mutation (`set_run_config`), so it
        # must not happen ahead of a check that can still bounce the cycle
        # with nothing to restore -- `fix_project_busy` above included. Past
        # this point the cycle is committed to running (or pausing for a
        # reason that has nothing to undo).
        route = str(getattr(getattr(self.profile, "fix_loop", None), "dev_route", "") or "")
        if route:
            try:
                prior = self.deps.assign_dev_route(project_id, route)
            except Exception as exc:  # noqa: BLE001 - a seat that cannot be filled is a pause
                _LOG.exception("liverun %s could not seat the dev on %s", self.run_id, route)
                return self._pause("fix_run_failed", failed=False,
                                   detail=f"dev_route_unavailable:{route}:{type(exc).__name__}")
            if prior:
                self._event("fix_team_model", {"project_id": project_id, "role": "dev",
                                               "from": prior, "to": route})
        try:
            if self.deps.seed_workspace(project_id):
                repo_path = self._safe(
                    lambda: str(getattr(self._store.get_project(), "repo_path", "") or ""),
                    default="")
                self._event("fix_workspace_seeded", {"project_id": project_id,
                                                     "repo_path": repo_path})
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("liverun %s could not seed workspace for %s", self.run_id, project_id)
            return self._pause("fix_run_failed", failed=False, detail=f"seed:{type(exc).__name__}")
        try:
            self._ws = self.deps.workspace(project_id)
            self._head_before = str(self._ws.head() or "")
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("liverun %s could not open workspace for %s", self.run_id, project_id)
            return self._pause("fix_run_failed", failed=False,
                               detail=f"{type(exc).__name__}:{str(exc)[:80]}")
        self._state = "task"
        return self._pending("fix_triage")

    def _ask_pm(self, why: str) -> tuple[str | None, str]:
        """The one PM turn spec §3.4 step 4 allows. The model chooses from an
        enumeration and composes nothing: an unparseable reply, an unknown id or
        any extra key leaves the cycle ambiguous."""
        if self.deps.triage_fn is None:
            return None, f"{why}; no PM turn seam is configured"
        route = getattr(self.profile.fix_loop, "triage_route", "pm")
        prompt = _triage.build_triage_prompt(self.bundle, self.profile)
        try:
            reply = self.deps.triage_fn(prompt, self.project_id, route)
        except Exception as exc:  # noqa: BLE001 - a model that errors is a model that abstained
            _LOG.exception("liverun %s triage turn failed", self.run_id)
            return None, f"{why}; the PM turn failed ({type(exc).__name__})"
        legal = tuple(r.id for r in self.profile.repos)
        repo_id, detail = _triage.parse_triage_reply(str(reply or ""), legal)
        if repo_id is None:
            return None, f"{why}; the PM reply was rejected ({detail})"
        return repo_id, detail

    # -- 2. file the task -------------------------------------------------- #
    def _do_task(self) -> FixOutcome:
        gate = _gate_label(self._store)
        title, detail = _brief.build_fix_brief(self.bundle, self._repo, gate_label=gate)
        project_id = str(self._repo.errorta_project)
        # The fix is the team's OPERATIVE GOAL, not just one task in the backlog:
        # without an active Focus the PM plans against the North Star and, on an
        # existing repo, scaffolds it from scratch (live 2026-08-22, run b8370d).
        # Same seam Slack's `set_next_goal` uses; the body points at the task.
        try:
            focus = self._store.add_focus(
                title=title, origin="liverun",
                body=("Live-run fix cycle: the ONLY goal is this one fix. The repository "
                      "already exists and works; do not scaffold, re-architect, or plan "
                      "beyond the filed task. The task carries the evidence and the gate."))
            self.focus_id = str(getattr(focus, "id", "") or "")
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("liverun %s could not set the fix focus", self.run_id)
            return self._pause("fix_run_failed", failed=False, detail=type(exc).__name__)
        try:
            task = self._store.add_task(title=title, role="dev", detail=detail,
                                        task_type="implementation")
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("liverun %s could not file the fix task", self.run_id)
            return self._pause("fix_run_failed", failed=False, detail=type(exc).__name__)
        self.task_id = str(getattr(task, "task_id", "") or "")
        self._event("fix_task", {"task_id": self.task_id, "repo_id": self.repo_id,
                                 "project_id": project_id, "gate": gate,
                                 "focus_id": self.focus_id})
        self._state = "run"
        return self._pending("fix_task")

    # -- 3. start the dev run ---------------------------------------------- #
    def _do_run(self) -> FixOutcome:
        project_id = str(self._repo.errorta_project)
        status = self._status()
        if status == "running":
            return self._pause("fix_project_busy", failed=False, detail=status)
        resume = status == "interrupted"
        continue_ = status == "stopped"
        mode = "resume" if resume else ("continue" if continue_ else "fresh")
        try:
            result = self.deps.start_run(project_id, resume=resume, continue_=continue_)
        except Exception as exc:  # noqa: BLE001 - the engine's failure is this cycle's failure
            _LOG.exception("liverun %s could not start the fix run", self.run_id)
            return self._pause("fix_run_failed", failed=True, detail=type(exc).__name__)
        # The app's own `_start_run` answers `{"started": True, ...}` with no
        # status key; anything that DOES carry one and does not say "started" is
        # a refusal (a start gate, a health preflight) and ends the cycle here
        # rather than leaving it watching a run that was never started.
        out_status = str((result or {}).get("status") or "started") if isinstance(result, dict) \
            else "started"
        self._event("fix_run", {"project_id": project_id, "mode": mode, "status": out_status})
        if out_status == "already_running":
            return self._pause("fix_project_busy", failed=False, detail=out_status)
        if out_status != "started":
            return self._pause("fix_run_failed", failed=True, detail=out_status)
        now = self._clock()
        self._run_started = True
        self._run_started_at = now
        self._last_progress = now
        self._next_poll = now + POLL_S
        self._fingerprint = self._sample()
        # A start that flipped the run state to `running` before returning has
        # already answered the "did it actually start?" question.
        self._saw_running = self._fingerprint[0] == "running"
        self._state = "watch"
        return self._pending("fix_run")

    # -- 4. watch it ------------------------------------------------------- #
    def _do_watch(self) -> FixOutcome:
        now = self._clock()
        if now < self._next_poll:
            return self._pending("fix_watch")
        self._next_poll = now + POLL_S
        fingerprint = self._sample()
        if fingerprint != self._fingerprint:
            self._fingerprint = fingerprint
            self._last_progress = now
        status = str(fingerprint[0] or "")
        if status == "running":
            self._saw_running = True
        started_long_ago = now - self._run_started_at > RUN_START_GRACE_S
        if status in TERMINAL_RUN_STATUSES and (self._saw_running or started_long_ago):
            self._run_status = status
            self._state = "stage"
            return self._pending("fix_run_terminal")
        if not self._saw_running and started_long_ago and status not in TERMINAL_RUN_STATUSES:
            # Two minutes in and the project's run state has never once said
            # `running`: nothing picked the task up.
            return self._pause("fix_run_failed", failed=True, detail=f"never_started:{status}")
        idle = now - self._last_progress
        if idle > self._idle_timeout_s:
            self._event("fix_idle_cancel", {"idle_s": int(idle),
                                            "project_id": str(self._repo.errorta_project)})
            # The ONLY cancel signal this loop may use is the one Slack's
            # `stop_run` sets — the run loop's own cooperative stop.
            self._safe(lambda: self._store.set_run_state(cancel_requested=True), default=None)
            self._cancelled_at = now
            self._state = "cancelled"
            return self._pending("fix_idle_cancel")
        return self._pending("fix_watch")

    def _do_cancelled(self) -> FixOutcome:
        """A cancelled run is a failed cycle whatever it does next; we wait only
        so the pause is honest about whether the run actually stopped."""
        now = self._clock()
        waited = now - (self._cancelled_at or now)
        if now < self._next_poll and waited <= CANCEL_WAIT_S:
            return self._pending("fix_idle_wait")
        self._next_poll = now + POLL_S
        status = self._status()
        if status in TERMINAL_RUN_STATUSES or waited > CANCEL_WAIT_S:
            return self._pause("fix_idle", failed=True,
                               detail=status if status in TERMINAL_RUN_STATUSES
                               else "cancel_not_acknowledged")
        return self._pending("fix_idle_wait")

    def _sample(self) -> tuple:
        """`(status, cancel_requested, len(team_log), last_entry_at)` — the
        liveness fingerprint (G-6, G-8). Any change is progress."""
        state = self._safe(self._store.get_run_state, default={}) or {}
        log = self._safe(self.deps.team_log, self._store, default=[]) or []
        last_at = ""
        if log:
            last = log[-1]
            last_at = str(last.get("at") or "") if isinstance(last, dict) else str(last)
        return (str(state.get("status") or "idle"), bool(state.get("cancel_requested")),
                len(log), last_at)

    def _status(self) -> str:
        state = self._safe(self._store.get_run_state, default={}) or {}
        return str(state.get("status") or "idle")

    # -- 5. stage the acceptance ------------------------------------------- #
    def _do_stage(self) -> FixOutcome:
        paths = self._safe(self._ws.changed_paths, "master", base=self._head_before,
                           default=None)
        if paths is None:
            return self._pause("fix_no_delivery", failed=True, detail="diff unreadable")
        paths = [str(p) for p in paths]
        if not paths:
            code = "fix_run_failed" if self._run_status == "failed" else "fix_no_delivery"
            return self._pause(code, failed=True, detail=self._run_status or "stopped")
        if escapes_repo(paths):
            return self._pause("fix_unsafe_paths", failed=True,
                               detail="a delivered path leaves the repository")
        if not self._safe(self.deps.merge_gate_ok, self._store, self._ws, default=False):
            # Nothing is staged: a confirmation for a merge the gate will refuse
            # is a button that can only lie about what tapping it does.
            return self._pause("fix_gate_blocked", failed=True, detail="gate closed")
        human_only = is_human_only_diff(paths, profiles_dir=self._profiles_dir
                                        or _profile.profiles_dir())
        project_id = str(self._repo.errorta_project)
        args = {"project_id": project_id, "repo_id": self.repo_id, "run_id": self.run_id,
                "task_id": self.task_id or "", "cycle": self.cycle,
                "human_only": human_only, "changed_paths": paths[:MAX_STAGED_PATHS]}
        try:
            self.cid = self.deps.stage_confirmation(
                ACCEPT_VERB, args, "", channel_id=self.deps.bound_channel(project_id))
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("liverun %s could not stage the acceptance", self.run_id)
            return self._pause("fix_declined", failed=True, detail=type(exc).__name__)
        self.accept_cid = self.cid
        self._next_poll = self._clock() + POLL_S
        self._event("fix_accept_staged", {"cid": self.cid, "repo_id": self.repo_id,
                                          "human_only": human_only, "n_paths": len(paths)})
        self._accept_deadline = self._clock() + self._accept_timeout_s
        self._state = "await"
        return self._pending("fix_accept_staged")

    # -- 6. wait for the decision ------------------------------------------ #
    def _do_await(self) -> FixOutcome:
        now = self._clock()
        timed_out = now > self._accept_deadline
        if now < self._next_poll and not timed_out:
            return self._pending("fix_awaiting_accept")
        self._next_poll = now + POLL_S
        record = self._safe(self.deps.get_confirmation, self.cid, default=None)
        state = str((record or {}).get("state") or "") if isinstance(record, dict) else ""
        if state == "pending":
            if timed_out:
                # `_pause` withdraws it: an unanswered acceptance must not be
                # left pending for the autopilot sweep to find later.
                return self._pause("fix_declined", failed=True, detail="timeout")
            return self._pending("fix_awaiting_accept")
        if state != "approved":
            return self._pause("fix_declined", failed=True, detail=state or "missing")
        # Approved means the confirmation was CLAIMED and its effect fired.
        # Nothing to withdraw any more.
        self.cid = ""
        return self._verify_accepted()

    def _verify_accepted(self) -> FixOutcome:
        """Approved is what the DECISION was; this asks what actually happened.

        Three questions, cheapest last-resort first: what the effect recorded
        (once anything records it — `accept_outcome_fn`), whether the merge gate
        is still open (`accept_live_fix` asks the identical question, so a closed
        gate now means it merged nothing), and whether the delivered work is
        still on the branch at all. The third is a floor, not a proof of merge:
        it catches a workspace that was reset or re-seeded between staging and
        approval, which would make deploying meaningless.
        """
        outcome = self._safe(self.deps.accept_outcome, self.accept_cid, default=None)
        status = str((outcome or {}).get("status") or "") if isinstance(outcome, dict) else ""
        if status == "gate_blocked":
            return self._pause("fix_gate_blocked", failed=True, detail=status)
        if status and status != "accepted":
            return self._pause("fix_accept_unverified", failed=True, detail=status)
        if not self._safe(self.deps.merge_gate_ok, self._store, self._ws, default=False):
            return self._pause("fix_gate_blocked", failed=True, detail="gate closed at accept")
        head = str(self._safe(self._ws.head, default="") or "")
        if not head or head == self._head_before:
            return self._pause("fix_accept_unverified", failed=True,
                               detail=f"head unchanged at {head or 'unknown'}")
        self._event("fix_accepted", {"repo_id": self.repo_id,
                                     "delivered_to": str(getattr(self._repo, "path", "")),
                                     "head": head, "verified_by": status or "workspace"})
        self._state = "deploy"
        return FixOutcome("accepted", "fix_accepted", self._events)

    # -- 7. deploy --------------------------------------------------------- #
    def _do_deploy(self) -> FixOutcome:
        steps = tuple(getattr(self._repo, "deploy", ()) or ())
        if self._deploy_i >= len(steps):
            self._state = "done"
            return FixOutcome("deployed", f"fix_cycle_complete:{self.repo_id}", self._events)
        step = steps[self._deploy_i]
        if not self._deploy_action_done:
            # Recorded ONCE per step, not per tick: `file_mtime_newer` compares
            # a check's `step_start` against an mtime, so a start stamp that
            # moved with every poll could never be older than the file it is
            # waiting for.
            self._deploy_started = self._clock()
            self._deploy_started_wall = self._wall()
            if step.action is not None:
                try:
                    res = self._run_action(step.action, self._ctx, timeout_s=step.timeout_s)
                except Exception as exc:  # noqa: BLE001
                    _LOG.exception("liverun %s deploy step %s raised", self.run_id, step.name)
                    return self._deploy_failed(step.name, detail=type(exc).__name__)
                tail = f"{res.stdout_tail}\n{res.stderr_tail}"
                self._event("deploy_step", {"name": step.name, "ok": bool(res.ok),
                                            "exit_code": res.exit_code,
                                            "tail": tail[-400:]})
                if self._ban_scan is not None and self._ban_scan(tail, where=f"deploy:{step.name}"):
                    return self._pause("ban_signal", failed=True, detail=step.name)
                if not res.ok:
                    return self._pause(f"deploy_failed:{step.name}", failed=True,
                                       detail=str(res.exit_code))
            self._deploy_action_done = True
            # Deploy-step evidence literals are deliberately NOT merged into the
            # run's literals: deploy runs after teardown, so a literal written
            # here would be a claim about a session that is already down.
        if step.check is None:
            return self._next_deploy_step()
        ok = False
        try:
            ok = bool(self._run_check(step.check, self._ctx,
                                      step_start=self._deploy_started_wall))
        except Exception:  # noqa: BLE001 - a check that throws is a check that hasn't passed
            _LOG.exception("liverun %s deploy check %s raised", self.run_id, step.name)
        if ok:
            self._event("deploy_step", {"name": step.name, "check": "passed"})
            return self._next_deploy_step()
        if self._clock() - (self._deploy_started or self._clock()) > step.timeout_s:
            self._event("deploy_step", {"name": step.name, "check": "timeout"})
            return self._deploy_failed(step.name, suffix=":check_timeout", detail="check_timeout")
        return self._pending("fix_deploying")

    def _next_deploy_step(self) -> FixOutcome:
        self._deploy_i += 1
        self._deploy_action_done = False
        self._deploy_started = None
        self._deploy_started_wall = 0.0
        if self._deploy_i >= len(tuple(getattr(self._repo, "deploy", ()) or ())):
            self._state = "done"
            return FixOutcome("deployed", f"fix_cycle_complete:{self.repo_id}", self._events)
        return self._pending("fix_deploying")

    def _deploy_failed(self, name: str, *, suffix: str = "", detail: str = "") -> FixOutcome:
        return self._pause(f"deploy_failed:{name}{suffix}", failed=True, detail=detail)

    def _do_done(self) -> FixOutcome:
        return FixOutcome("deployed", f"fix_cycle_complete:{self.repo_id}", [])

    # -- shared ------------------------------------------------------------ #
    def _safe(self, fn: Callable[..., Any], *args: Any, default: Any = None, **kw: Any) -> Any:
        """Call an engine seam; a failure is the ``default``, never a raise. The
        cycle turns every engine problem into a pause, because a supervisor that
        dies here leaves a run wedged in a non-terminal phase."""
        try:
            return fn(*args, **kw)
        except Exception:  # noqa: BLE001
            _LOG.exception("liverun %s fix seam %s failed", self.run_id,
                           getattr(fn, "__name__", fn))
            return default


def _gate_label(store: Any) -> str:
    """One human-readable line naming the gate the dev team must pass. Prose for
    the brief, never a decision — so any failure degrades to a generic name."""
    try:
        commands = store.get_test_commands() or {}
        cmd_id = sorted(commands)[0]
        argv = commands[cmd_id].get("argv") or []
        return f"{cmd_id} — {' '.join(str(a) for a in argv)}".strip(" —")[:200]
    except Exception:  # noqa: BLE001
        return "the project's registered acceptance gate"


__all__ = ["FixCycle", "FixDeps", "FixOutcome", "GUARDED_PATH_PREFIXES", "PAUSE_CODES",
           "ACCEPT_VERB", "escapes_repo", "is_human_only_diff"]
