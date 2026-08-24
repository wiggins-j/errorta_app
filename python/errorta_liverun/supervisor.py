"""The live-run state machine (spec §3.6). One daemon thread per run.

idle -> launching(step i) -> watching -> stopping(reason) -> stopped
                                                          -> paused_awaiting_human
step failed ----------------------------------------------> failed

A stop the fix loop can act on continues instead of ending, ON THE SAME THREAD:

  stopping(reason) -> fixing -> accepting -> deploying -> stopped + relaunch

Every exit path — stall, launch failure, operator stop, supervisor crash —
goes through ``_do_stopping``: evidence first, then teardown, then the
``literals`` verdict. Nothing here retries a launch; a refusal or a failure
ends the cycle and the caps ledger decides whether another one may start.

Time is wall-clock throughout: stalls are ``now - last_ok`` on the injected
clock, never a tick count, so a slow or blocked tick can't fake liveness.

Event kinds appended to the run's ``events.jsonl``: ``phase``, ``launch_step``,
``probe_warn``, ``probe_error``, ``stall``, ``evidence``, ``teardown_step``,
``literals``, ``caps``, ``ban_signal``, ``refused`` — and, for a run that
enters the Slice 2 fix loop, ``fix_skipped``, ``fix_triage``, ``fix_task``,
``fix_run``, ``fix_idle_cancel``, ``fix_accept_staged``, ``fix_accepted``,
``deploy_step``, ``fix_cycle_cap`` and ``relaunch_refused``.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from errorta_app.paths import errorta_home

from . import profile as _profile
from . import steps as _steps
from .brief import EvidenceBundle, EvidenceItem
from .fixloop import FixCycle, FixDeps, FixOutcome
from .state import TERMINAL_PHASES, LaunchLedger, RunState, RunStore, caps_disabled, now_iso

_LOG = logging.getLogger("errorta.liverun")
_CHECK_POLL_S = 2.0
_TICK_S = 1.0
_WATCH_SAVE_S = 30.0
_UNCAPPED = 1 << 30
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_FIX_PHASES = ("fixing", "accepting", "deploying")
# Only a stall or a plain launch-step failure is a bug to fix. A refusal, a ban,
# a cap and an operator stop are all decisions to respect (spec §3.8 rule 7).
_FIXABLE_REASON_RE = re.compile(r"^(stall|launch_step_failed):")
_REFUSAL_TAIL_RE = re.compile(r"^REFUSED:", re.MULTILINE)
# Skip codes that are NOT worth an event: nothing was expected to happen, or the
# more specific event (`fix_cycle_cap`) has already said it.
_SILENT_SKIPS = frozenset({
    "not_configured",   # a Slice 1 profile: it never asked for a fix loop
    "run_lost",         # boot recovery: a run lost to a restart is not a bug to fix
    "fix_in_flight",    # this run already had its cycle
    "fix_cycle_cap",    # the more specific `fix_cycle_cap` event already said it
})


class LiveRunRefused(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def paused_marker(profile_name: str) -> Path:
    """Marker file that refuses every start of ``profile_name`` until an
    operator resumes it. Written on a ban signal or a consecutive-failure cap
    — the two conditions a human, not a retry loop, has to clear."""
    return errorta_home() / "liverun" / "paused" / profile_name


def fix_paused_marker(profile_name: str) -> Path:
    """Marker file that keeps ``profile_name`` running but refuses to enter the
    FIX loop. Separate from ``paused_marker`` on purpose: pausing autonomous
    merging is subtractive and R-class (`pause_fix_loop`), while re-arming it is
    human-only — and neither should silently stop or start live runs."""
    return errorta_home() / "liverun" / "fix-paused" / profile_name


class Supervisor:
    """Drives one live run. `_tick` is the whole machine: `run_once_blocking`
    just calls it until the phase is terminal, and tests call it directly so
    the machine can be driven against a fake clock without real sleeps."""

    def __init__(self, profile: _profile.Profile, *, store: RunStore, ledger: LaunchLedger,
                 tunnels: Any, remote: Any, project_id: str | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Any] | None = None,
                 teardown_sleep: Callable[[float], Any] | None = None,
                 wall: Callable[[], float] = time.time,
                 run_action=_steps.run_action, run_check=_steps.run_check,
                 run_probe=_steps.run_probe, fix_deps: FixDeps | None = None,
                 relaunch_fn: Callable[..., dict] | None = None,
                 fix_of: str | None = None, fix_cycle: int = 0) -> None:
        self.profile = profile
        self.store = store
        self.ledger = ledger
        self._clock = clock          # monotonic: timeouts, stalls, elapsed
        self._wall = wall            # epoch: the ledger, and check `step_start`
        self._stop = threading.Event()
        # Same shape as `_stop`, one level in: `pause_fix` is a REQUEST, and the
        # supervisor's own thread is what honours it. See `pause_fix`.
        self._pause_fix = threading.Event()
        self._pause_fix_reason: str | None = None
        self._sleep = sleep or (lambda s: self._stop.wait(s))
        # Teardown polls must NOT use `_sleep`: its default is `self._stop.wait`,
        # which returns instantly once `stop()` has been called — and teardown
        # only ever runs after that. Polling a logoff check on a real interval
        # is the whole point; a spin loop would hammer ssh/HTTP for the length
        # of the step timeout and starve the very thing it is waiting for.
        self._teardown_sleep = teardown_sleep or time.sleep
        self._run_action, self._run_check, self._run_probe = run_action, run_check, run_probe
        self._thread: threading.Thread | None = None
        self._stop_reason: str | None = None
        self._operator_stop_reason: str | None = None   # what `stop()` was told
        self._stopping = False
        self._closed = False
        self._closed_once = False
        self._banned = False
        self._refused = False        # a launch step DECLINED; not a bug to fix
        self._fix: FixCycle | None = None
        self._fix_aborted = False
        self._fix_deps = fix_deps or FixDeps()
        self._relaunch_fn = relaunch_fn
        self._relaunched = False
        self._evidence_items: list[EvidenceItem] = []
        self._stalled_s: float | None = None
        self._warned: set[str] = set()
        # Which watch ids have seen a DEFINITE False (not just a transport
        # `None`) since their `_probe_last_ok` was last refreshed -- this is
        # what tells a stall apart from a stall that never actually saw
        # evidence of failure, only an inability to look (spec: tri-state
        # remote probes, live 2026-08-23 run aaadac).
        self._probe_saw_false: set[str] = set()
        self._probe_next: dict[str, float] = {}
        self._probe_last_ok: dict[str, float] = {}
        self._last_watch_save: float | None = None
        self._step_started: float | None = None       # monotonic
        self._step_started_wall: float = 0.0          # epoch
        rid = store.new_run_id()
        self.state = RunState(
            run_id=rid, profile_name=profile.name, project_id=project_id, phase="idle",
            reason=None, session_id=f"lr-{rid}", step_index=0, started_at=now_iso(),
            launched_at=None, ended_at=None, evidence_dir=str(store.evidence_dir(rid)),
            fix_of=fix_of, fix_cycle=int(fix_cycle))
        self.ctx = _steps.Ctx(
            profile=profile, run_id=rid, session_id=self.state.session_id,
            evidence_dir=Path(self.state.evidence_dir), tunnels=tunnels, remote=remote,
            owned_pgids=self.state.owned_pgids, owned_remote_pidfiles=self.state.owned_remote_pidfiles,
            owned_tunnels=self.state.owned_tunnels, last_values=self.state.probe_last_value,
            launched_monotonic=None, clock=clock)

    # -- lifecycle --------------------------------------------------------- #
    def start(self, *, blocking: bool = False) -> RunState:
        """Arm the run: refuse if paused or capped, record the launch, and move
        to ``launching``. ``blocking=True`` then drives the loop on this thread;
        otherwise the caller drives it — ``start_background`` in production,
        ``_tick`` in tests. Nothing is spawned behind the caller's back."""
        if self.state.phase != "idle":
            # One Supervisor drives one run; a second start would spend another
            # launch from the caps ledger and reset a machine already in flight.
            self._event("refused", {"code": "already_started", "phase": self.state.phase})
            raise LiveRunRefused("already_started", self.state.phase)
        if paused_marker(self.profile.name).exists():
            self._event("refused", {"code": "paused_awaiting_human"})
            raise LiveRunRefused("paused_awaiting_human", "resume_live_run required")
        code = self.ledger.check(self.profile.name, self.profile.caps, self._wall())
        if caps_disabled():
            # Operator debug switch: the ledger already verdicted None above
            # (`LaunchLedger.check` itself short-circuits) -- this is only the
            # loud announcement that a check was skipped, once per start.
            self._event("caps", {"code": "caps_disabled_by_operator"})
        elif code:
            self._event("refused", {"code": code})
            raise LiveRunRefused(code)
        self.ledger.record(self.profile.name, self.state.run_id, self._wall())
        self._set_phase("launching")
        if blocking:
            self.run_once_blocking()
        return self.state

    def start_background(self) -> RunState:
        """`start` plus the daemon thread that drives the run to a terminal
        phase. Joinable via `join` — `LiveRunManager.teardown_all` does."""
        state = self.start()
        self._thread = threading.Thread(target=self.run_once_blocking, daemon=True,
                                        name=f"liverun-{self.state.run_id}")
        self._thread.start()
        return state

    def run_once_blocking(self) -> None:
        try:
            while self.state.phase not in TERMINAL_PHASES:
                self._tick()
                if self.state.phase not in TERMINAL_PHASES:
                    self._sleep(_TICK_S)
        except BaseException as exc:  # noqa: BLE001 — dying silently leaves the world running
            _LOG.exception("liverun %s crashed", self.state.run_id)
            self._stop_reason = f"supervisor_error:{type(exc).__name__}"
            try:
                self._do_stopping(final_phase="failed")
            except Exception:  # noqa: BLE001
                _LOG.exception("liverun %s teardown after crash failed", self.state.run_id)
            if self.state.phase not in TERMINAL_PHASES:
                self._finish("failed", self._stop_reason)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise

    def stop(self, reason: str = "operator_stop") -> None:
        """Never gated: the next tick from any non-terminal phase tears down.

        The reason is recorded TWICE on purpose. `_stop_reason` keeps Slice 1's
        first-reason-wins semantics (a stall that is already tearing down is
        what ended the run). `_operator_stop_reason` is what THIS call asked
        for, which is the only honest answer for a run that had already stopped
        and was in a fix cycle when the request arrived."""
        self._operator_stop_reason = self._operator_stop_reason or reason
        self._stop_reason = self._stop_reason or reason
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # -- one tick of the machine (public for tests) ------------------------ #
    def _tick(self) -> None:
        ph = self.state.phase
        if ph in TERMINAL_PHASES:
            return
        if self._pause_fix.is_set() and self._fix is not None and ph in _FIX_PHASES:
            # BEFORE the phase dispatch, so the cycle does not take one more
            # step (which could be the step that approves the merge) after the
            # operator asked for it to stop.
            self._do_pause_fix()
            return
        if self._stop.is_set() or ph == "stopping":
            self._do_stopping()
        elif ph == "launching":
            self._tick_launch()
        elif ph == "watching":
            self._tick_watch()
        elif ph in _FIX_PHASES:
            self._tick_fix()

    def _tick_launch(self) -> None:
        steps = self.profile.launch
        i = self.state.step_index
        if i >= len(steps):
            self.state.launched_at = now_iso()
            self.ctx.launched_monotonic = self._clock()
            now = self._clock()
            for w in self.profile.watch:
                self._probe_last_ok[w.id] = now   # the stall window opens now
                self._probe_next[w.id] = now
                self._probe_saw_false.discard(w.id)
            self._set_phase("watching")
            return
        step = steps[i]
        if self._step_started is None:
            self._step_started = self._clock()
            self._step_started_wall = self._wall()
            if step.action is not None:
                res = self._run_action(step.action, self.ctx, timeout_s=step.timeout_s)
                self._event("launch_step", {"name": step.name, "ok": res.ok, "exit_code": res.exit_code,
                                            "stdout": res.stdout_tail[-400:],
                                            "stderr": res.stderr_tail[-400:]})
                if self._scan_ban(f"{res.stdout_tail}\n{res.stderr_tail}", where=f"launch:{step.name}"):
                    self._stop_reason = self._stop_reason or "ban_signal"
                    self._do_stopping()
                    return
                if not res.ok:
                    reason = f"launch_step_failed:{step.name}"
                    tails = f"{res.stdout_tail}\n{res.stderr_tail}"
                    if res.exit_code == 3 or _REFUSAL_TAIL_RE.search(tails):
                        # The brain DECLINED the launch (risk budget). Not an
                        # error to retry, and not a bug to file a task about —
                        # a decision to respect (Slice 1 F-A, spec §3.5).
                        self._refused = True
                    if res.exit_code == 3:
                        reason += ":refused"
                    self._evidence_items.append(EvidenceItem(
                        id=f"launch:{step.name}", ok=False, detail=res.detail,
                        stdout_tail=res.stdout_tail, stderr_tail=res.stderr_tail))
                    self._stop_reason = reason
                    self._do_stopping(final_phase="failed")
                    return
        if step.check is None or self._run_check(step.check, self.ctx, step_start=self._step_started_wall):
            if step.check is not None:
                self._event("launch_step", {"name": step.name, "check": "passed"})
            self.state.step_index += 1
            self._step_started = None
            self._save()
            return
        if self._clock() - self._step_started > step.timeout_s:
            self._event("launch_step", {"name": step.name, "check": "timeout"})
            self._stop_reason = f"launch_step_failed:{step.name}:check_timeout"
            self._do_stopping(final_phase="failed")
            return
        self._sleep(_CHECK_POLL_S)

    def _tick_watch(self) -> None:
        now = self._clock()
        dirty = False
        for w in self.profile.watch:
            if now < self._probe_next.get(w.id, 0.0):
                continue
            self._probe_next[w.id] = now + w.every_s
            try:
                result = self._run_probe(w.probe, self.ctx)
            except Exception as exc:  # noqa: BLE001 — a probe that throws is a probe that isn't ok
                result = False
                self._event("probe_error", {"id": w.id, "error": type(exc).__name__,
                                            "detail": str(exc)[:200]})
            if result is True:
                self._probe_last_ok[w.id] = now
                self._probe_saw_false.discard(w.id)   # a fresh window starts here
                stamp = now_iso()
                dirty = dirty or self.state.probe_last_ok.get(w.id) != stamp
                self.state.probe_last_ok[w.id] = stamp
                self._warned.discard(w.id)
                continue
            # `result` is False (a definite failure) or None (a transport
            # failure: we could not observe the box at all this tick). Either
            # way `last_ok` does NOT move -- but only a definite False is
            # remembered as evidence toward the eventual stall reason.
            if result is False:
                self._probe_saw_false.add(w.id)
            stalled_for = now - self._probe_last_ok.get(w.id, now)
            if stalled_for < w.stall_after_s:
                continue
            if w.on_stall == "warn":
                # An unknown result is not something to warn about -- there is
                # nothing to tell the operator except "we could not check",
                # which is not actionable and not what `probe_warn` means.
                if result is False and w.id not in self._warned:
                    self._warned.add(w.id)      # one warning per stall episode
                    self._event("probe_warn", {"id": w.id, "stalled_s": stalled_for})
                continue
            unverifiable = w.id not in self._probe_saw_false
            detail = {"id": w.id, "stalled_s": stalled_for}
            if unverifiable:
                detail["unverifiable"] = True
            self._event("stall", detail)
            self._stalled_s = stalled_for
            self._stop_reason = f"stall:{w.id}:unverifiable" if unverifiable else f"stall:{w.id}"
            self._do_stopping()
            return
        # A watching tick is the hot path — a run can sit here for hours. Only
        # write `state.json` when the persisted picture actually moved, or once
        # every `_WATCH_SAVE_S` so a crash can never leave the file stale by
        # more than that.
        stale = self._last_watch_save is None or (now - self._last_watch_save) >= _WATCH_SAVE_S
        if dirty or stale:
            self._last_watch_save = now
            self._save()

    # -- stopping: evidence, teardown, literals ---------------------------- #
    def _do_stopping(self, *, final_phase: str = "stopped") -> None:
        """Evidence, then teardown, then the closing sequence — for EVERY exit
        path. The closing sequence lives in a ``finally`` because it is the part
        that must not be optional: a step that throws halfway through teardown
        would otherwise skip the kills, the literals verdict, the ledger outcome
        and the terminal phase, leaving a run wedged at ``stopping`` that the
        manager reports as live forever."""
        if self.state.phase in TERMINAL_PHASES:
            return                              # already closed out; a no-op
        reason = self._stop_reason or "unknown"
        in_fix = self._fix is not None and self.state.phase in _FIX_PHASES
        if in_fix:
            # A fix cycle owns a coding run and a staged acceptance. Both have
            # to be told, BEFORE anything here lands the run terminal — a
            # supervisor that just exits leaves a dev run burning tokens and a
            # button the autopilot sweep will happily press.
            self._abort_fix(self._operator_stop_reason or reason)
        if self._stopping:
            # Re-entered while a first pass was unwinding — the crash handler in
            # `run_once_blocking` comes back through here, and so does a stop
            # that arrived during a fix cycle. Never replay evidence or teardown,
            # but do finish the closing sequence the first pass dropped
            # (`_close_out` is itself idempotent).
            if in_fix and self._operator_stop_reason:
                # The RUN already stopped (that stall is why there was a cycle
                # at all). What ended THIS is the operator, and it is not a
                # failure — reporting `failed: stall:brain-log` would blame the
                # profile for a human pressing stop.
                self._close_out(final_phase="stopped", reason=self._operator_stop_reason)
            else:
                self._close_out(final_phase="failed", reason=reason)
            return
        self._stopping = True
        try:
            # Inside the try: even the `stopping` state write can fail (a full
            # or read-only disk), and that must not cost the close-out below.
            self._set_phase("stopping", reason)
            self._run_evidence()
            self._run_teardown()
        finally:
            self._close_out(final_phase=final_phase, reason=reason)

    def _run_evidence(self) -> None:
        for step in self.profile.evidence:
            if step.action is None:
                continue
            try:
                res = self._run_action(step.action, self.ctx, timeout_s=step.timeout_s)
            except Exception as exc:  # noqa: BLE001 — evidence is best-effort; teardown must still run
                _LOG.exception("liverun %s evidence step %s failed", self.state.run_id, step.name)
                res = _steps.StepResult(False, now_iso(), now_iso(), detail=type(exc).__name__)
            self._scan_ban(f"{res.stdout_tail}\n{res.stderr_tail}", where=f"evidence:{step.name}")
            # Keep the full (already redacted, already bounded) capture in
            # memory: the event log records only 'what happened', while the fix
            # brief and triage need the text itself, and re-reading it off disk
            # would re-import untrusted bytes through a second path.
            self._evidence_items.append(EvidenceItem(
                id=step.name, ok=bool(res.ok), detail=res.detail,
                stdout_tail=res.stdout_tail, stderr_tail=res.stderr_tail,
                refs=tuple(res.evidence_refs or ())))
            self._event("evidence", {"id": step.name, "ok": res.ok, "refs": res.evidence_refs,
                                     "detail": res.detail})

    def _run_teardown(self) -> None:
        for step in self.profile.teardown:
            ok, literal_ok, text = self._teardown_step(step)
            self._scan_ban(text, where=f"teardown:{step.name}")
            if step.evidence_literal:
                self.state.literals[step.evidence_literal] = literal_ok
            self._event("teardown_step", {"name": step.name, "ok": ok,
                                          "literal": step.evidence_literal,
                                          "literal_ok": literal_ok if step.evidence_literal else None})

    def _close_out(self, *, final_phase: str, reason: str) -> None:
        """Everything that MUST happen once a run is stopping, whatever went
        wrong while it was stopping. Each stage is independently guarded: one
        failure must not cost the others, and the run must land terminal even if
        every one of them fails."""
        if self._closed:
            return
        self._closed = True
        # A fix cycle re-opens the close-out (`_enter_fix_loop` clears `_closed`)
        # so the run can land terminal after it. The kills and the state write
        # must run again — a deploy step can spawn a local process — but the
        # ledger outcome and the literals verdict are statements about the LIVE
        # session and are made exactly once.
        stages: tuple[tuple[str, Callable[[], Any]], ...] = (
            ("kill_owned", self._kill_owned), ("save", self._save))
        if not self._closed_once:
            stages += (("outcome", lambda: self._record_outcome(final_phase, reason)),
                       ("literals", self._emit_literals))
        for stage, fn in stages:
            try:
                fn()
            except Exception:  # noqa: BLE001
                _LOG.exception("liverun %s close-out stage %s failed", self.state.run_id, stage)
        self._closed_once = True
        try:
            if self._banned:
                self._pause("ban_signal")
            elif (not caps_disabled()) and self._consecutive_failures_hit():
                self._event("caps", {"code": "cap_consecutive_failures"})
                self._pause("cap_consecutive_failures")
            else:
                # Deliberate, not a side effect of `LaunchLedger.check` going
                # quiet: the operator switch covers this pause too. It is the
                # ONE pause an operator debugging a live loop re-resumes over
                # and over, so leaving it live under CAPS_OFF would defeat the
                # switch's own purpose. Ban-signal pauses are untouched --
                # `self._banned` is checked first and does not consult caps at
                # all.
                # Slice 2: the ONE behavioural change to the closing sequence.
                # Teardown has already completed here — the fix cycle never runs
                # against a live game session.
                skip = ("run_lost" if final_phase == "lost_on_restart"
                        else self._enter_fix_loop(reason))
                if skip is None:
                    return                  # phase is now `fixing`; the tick loop drives on
                if skip == "fix_cycle_cap":
                    self._pause("fix_cycle_cap")
                    return
                if skip not in _SILENT_SKIPS:
                    self._event("fix_skipped", {"code": skip})
                self._finish(final_phase, reason)
        except Exception:  # noqa: BLE001
            _LOG.exception("liverun %s could not finish", self.state.run_id)
        if self.state.phase not in TERMINAL_PHASES:
            # Even the state write failed. Force the IN-MEMORY phase terminal
            # anyway: `LiveRunManager._active` reads this object, and a run stuck
            # at `stopping` would make the manager refuse every future start of
            # the profile with `already_running`, forever.
            self.state.phase = "failed"
            self.state.reason = self.state.reason or reason
            self.state.ended_at = self.state.ended_at or now_iso()

    # -- the fix loop (spec §3.5) ------------------------------------------ #
    def _enter_fix_loop(self, reason: str) -> str | None:
        """``None`` means the run just entered ``fixing``; any string is the code
        that kept it out. Evaluated in the order the spec states the conditions,
        so the FIRST true reason is the one reported."""
        if not self.profile.repos and self.profile.fix_loop is None:
            return "not_configured"            # a Slice 1 profile: say nothing
        if self._fix is not None:
            return "fix_in_flight"             # this run already had its cycle
        if self._refused:
            return "brain_refused"
        if (reason or "").endswith(":unverifiable"):
            # The supervisor could not actually SEE the box when this stall
            # window expired (a transport failure the whole way through, spec:
            # tri-state remote probes). No repo owns "we could not look" --
            # filing a task here would send a dev chasing a bug that was never
            # observed to exist.
            return "stall_unverifiable"
        if not _FIXABLE_REASON_RE.match(reason or "") or (reason or "").endswith(":refused"):
            return "reason_not_fixable"
        if self._stop.is_set():
            # Someone asked for this run to END (operator stop, sidecar
            # shutdown). Starting a fix cycle now would ignore that request and
            # keep the thread alive doing new work.
            return "stop_requested"
        fix_loop = self.profile.fix_loop
        if fix_loop is None or not fix_loop.enabled:
            return "fix_loop_disabled"
        if not self.profile.repos:
            return "no_repos"
        if paused_marker(self.profile.name).exists():
            return "profile_paused"
        if fix_paused_marker(self.profile.name).exists():
            return "fix_loop_paused"
        if caps_disabled():
            # Operator debug switch: the fix-cycle-per-day arithmetic is
            # skipped entirely, loudly, once per entry into the fix loop.
            self._event("caps", {"code": "caps_disabled_by_operator"})
        else:
            cap = int(fix_loop.max_fix_cycles_per_day)
            try:
                today = int(self.ledger.fix_cycles_today(self.profile.name, self._wall()))
            except Exception:  # noqa: BLE001 — an unreadable ledger is a full ledger
                _LOG.exception("liverun %s could not read the fix-cycle ledger", self.state.run_id)
                today = cap
            if today >= cap:
                self._event("fix_cycle_cap", {"cycles_today": today, "cap": cap})
                return "fix_cycle_cap"
        self._closed = False                   # this run is not over after all
        self.state.fix_repo_id = None
        self._set_phase("fixing", reason)
        self._fix = FixCycle(
            self._fix_bundle(reason), self.profile, None, self._fix_deps,
            run_id=self.state.run_id, project_id=self.state.project_id or "",
            clock=self._clock, wall=self._wall,
            idle_timeout_s=fix_loop.idle_timeout_s,
            accept_timeout_s=fix_loop.accept_timeout_s,
            cycle=self.state.fix_cycle + 1, ctx=self.ctx,
            run_action=self._run_action, run_check=self._run_check,
            ban_scan=self._scan_ban)
        return None

    def _fix_bundle(self, reason: str) -> EvidenceBundle:
        """What the dev team is told, built from what this run already holds. The
        probe / step id comes off the reason string the supervisor itself wrote,
        never off captured text."""
        probe_id = reason.split(":", 1)[1] if reason.startswith("stall:") else None
        probe_kind = next((w.probe.kind for w in self.profile.watch if w.id == probe_id), None) \
            if probe_id else None
        step_name = None
        if reason.startswith("launch_step_failed:"):
            step_name = reason.split(":")[1] or None
        return EvidenceBundle(
            run_id=self.state.run_id, profile_name=self.profile.name, stop_reason=reason,
            stalled_probe_id=probe_id, stalled_probe_kind=probe_kind, stalled_s=self._stalled_s,
            launch_step_name=step_name,
            literals=dict(self.state.literals), evidence=tuple(self._evidence_items),
            evidence_dir=self.state.evidence_dir)

    def _tick_fix(self) -> None:
        """One step of the fix cycle, then translate its outcome. The cycle owns
        no thread and never sleeps: it runs here, on the supervisor's own daemon
        thread, so `stop()` still interrupts between ticks and `teardown_all`
        still joins it."""
        cycle = self._fix
        if cycle is None:                       # can only happen after a restart
            self._finish("failed", self._stop_reason or "fix_cycle_lost")
            return
        try:
            out = cycle.step()
        except Exception as exc:  # noqa: BLE001 — a driver bug pauses; it never wedges the run
            _LOG.exception("liverun %s fix cycle raised", self.state.run_id)
            out = FixOutcome("paused", f"fix_error:{type(exc).__name__}", [], failed=True)
        for kind, detail in out.events:
            self._event(kind, detail)
        dirty = False
        if cycle.repo_id and self.state.fix_repo_id != cycle.repo_id:
            self.state.fix_repo_id, dirty = cycle.repo_id, True
        if cycle.task_id and self.state.fix_task_id != cycle.task_id:
            self.state.fix_task_id, dirty = cycle.task_id, True
        # The PENDING cid only: once it resolves either way the cycle clears it,
        # and boot recovery must not try to withdraw a confirmation somebody
        # already answered.
        if (cycle.cid or None) != self.state.fix_confirmation_id:
            self.state.fix_confirmation_id, dirty = cycle.cid or None, True
        if dirty:
            self._save()
        if out.kind == "aborted":
            return                              # `_do_stopping` owns what happens next
        if out.kind == "paused":
            if out.failed:
                self._record_fix_cycle(failed=True)
            self._pause(out.code)
            return
        if out.kind == "deployed":
            self._record_fix_cycle(failed=False)
            # The OLD run goes terminal first: until it does, the manager still
            # holds this profile and would refuse the relaunch as already_running.
            self._close_out(final_phase="stopped", reason=out.code)
            self._relaunch()
            return
        if cycle.phase != self.state.phase:
            self._set_phase(cycle.phase)

    def _abort_fix(self, reason: str) -> None:
        """Stop everything the cycle started, once."""
        cycle = self._fix
        if cycle is None or self._fix_aborted:
            return
        self._fix_aborted = True
        try:
            out = cycle.abort(reason)
        except Exception:  # noqa: BLE001 — the run must still land terminal
            _LOG.exception("liverun %s fix abort failed", self.state.run_id)
            return
        for kind, detail in out.events:
            self._event(kind, detail)
        self.state.fix_confirmation_id = None
        # Counted, but NOT as a failure: an operator stop (or a sidecar
        # shutdown) is not the repository's fault, and a cycle that spent a dev
        # run should still count against the day cap.
        self._record_fix_cycle(failed=False)

    def pause_fix(self, reason: str = "fix_loop_paused") -> None:
        """Ask this run to abort its fix cycle. Never gated, never blocking.

        Writing the fix-pause marker only stops the NEXT cycle; one already
        awaiting acceptance owns a dev run and a staged confirmation that the
        autopilot sweep will happily press minutes later, so "the fix loop is
        off" would be a lie until that cycle is told.

        A REQUEST, exactly like `stop()`, and for the same reason: this is
        called from whichever thread asked (Slack's, the sidecar's), while the
        daemon thread may be inside `_tick_fix`. `_abort_fix` and `_close_out`
        are both check-then-set on plain attributes (`_fix_aborted`, `_closed`),
        so driving them from two threads can duplicate an event and a fix-cycle
        ledger row. Setting an Event costs nothing and the next tick — on the
        one thread that owns the machine — does the work.
        """
        self._pause_fix_reason = self._pause_fix_reason or reason
        self._pause_fix.set()

    def _do_pause_fix(self) -> None:
        """The request, honoured on the supervisor's own thread. Only ever
        reached from `_tick` with a live cycle in a fix phase — where teardown
        has already completed, so nothing is landed terminal out from under a
        live game session."""
        reason = self._pause_fix_reason or "fix_loop_paused"
        self._abort_fix(reason)
        self._close_out(final_phase="stopped", reason=reason)

    def _record_fix_cycle(self, *, failed: bool) -> None:
        try:
            self.ledger.record_fix_cycle(self.profile.name, self.state.run_id,
                                         self.state.fix_repo_id or "", failed=failed,
                                         at=self._wall())
        except Exception:  # noqa: BLE001 — an unrecorded cycle must not wedge the run
            _LOG.exception("liverun %s could not record the fix cycle", self.state.run_id)

    def _relaunch(self) -> None:
        """A NEW run id, linked by ``fix_of``, entering ``launching`` from the
        top — so every Slice 1 cap is evaluated by the untouched `start`. A
        refusal is an event, never a retry."""
        if self._relaunched:
            return
        self._relaunched = True
        if self._stop.is_set():
            # The deploy finished into a shutdown / operator stop. Starting a
            # new live run now would launch a client nobody asked for.
            self._event("relaunch_refused", {"code": "stop_requested"})
            return
        fn = self._relaunch_fn or live_run_manager.start
        try:
            result = fn(profile_name=self.profile.name, project_id=self.state.project_id,
                        fix_of=self.state.run_id, fix_cycle=self.state.fix_cycle + 1)
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("liverun %s relaunch failed", self.state.run_id)
            self._event("relaunch_refused", {"code": f"error:{type(exc).__name__}"})
            return
        result = result if isinstance(result, dict) else {}
        if str(result.get("status") or "") != "started":
            self._event("relaunch_refused",
                        {"code": str(result.get("reason") or result.get("status") or "unknown")})

    def _record_outcome(self, final_phase: str, reason: str) -> None:
        failed = (final_phase == "failed" or reason.startswith("stall")
                  or reason.startswith("supervisor_error") or reason == "ban_signal")
        self.ledger.record_outcome(self.state.run_id, failed=failed)

    def _emit_literals(self) -> None:
        """The literals verdict is the last thing said about the run, so it
        lands before whichever phase event closes it out."""
        declared = {s.evidence_literal for s in self.profile.teardown if s.evidence_literal}
        declared.add("logoff_verified")   # spec-mandatory: always reported
        self._event("literals", {k: "PRESENT" if self.state.literals.get(k) else "ABSENT"
                                 for k in sorted(declared)})

    def _teardown_step(self, step: _profile.Step) -> tuple[bool, bool, str]:
        """Run one teardown sub-step. Returns ``(step_ok, literal_ok, text)``.

        ``literal_ok`` is the ONLY thing an ``evidence_literal`` may rest on,
        and it is False unless a declared CHECK passed. An action's own exit
        status is not evidence about the world: `_run_remote_signal` exits 0
        when there was no pidfile to signal at all, so a check-less step
        claiming `logoff_verified` would be a forged receipt. `profile.py`
        rejects such a step at load time; this is the second lock, for a
        `Profile` built in code (tests, recovery's synthesized stand-in)."""
        ok = True
        text = ""
        started, started_wall = self._clock(), self._wall()
        if step.action is not None:
            try:
                res = self._run_action(step.action, self.ctx, timeout_s=step.timeout_s)
                ok = res.ok
                text = f"{res.stdout_tail}\n{res.stderr_tail}"
            except Exception as exc:  # noqa: BLE001 — one broken teardown step must not skip the rest
                _LOG.exception("liverun %s teardown step %s failed", self.state.run_id, step.name)
                ok = False
                self._event("teardown_step", {"name": step.name, "error": type(exc).__name__})
        if step.check is None:
            return ok, False, text
        ok = False
        while True:
            try:
                if self._run_check(step.check, self.ctx, step_start=started_wall):
                    ok = True
                    break
            except Exception:  # noqa: BLE001 — a check that throws is a check that hasn't passed
                _LOG.exception("liverun %s teardown check %s failed", self.state.run_id, step.name)
            if self._clock() - started > step.timeout_s:
                break
            self._teardown_sleep(_CHECK_POLL_S)
        return ok, ok, text

    def _kill_owned(self) -> None:
        """Reconcile what this run wrote down as OWNED against reality: kill
        surviving process groups, close tunnels we opened. Every release is
        reported (`recovery` events) and a resource is dropped from the state
        ONLY when it was actually released — an id silently forgotten is a leak
        nobody can find afterwards.

        In a live run `owned_pgids` is live-only (steps prunes a reaped child),
        so this is normally a no-op; on the boot-recovery path it is the whole
        point. Remote processes are the profile's own teardown steps to signal;
        we hold no handle on them beyond their pidfiles."""
        started_after = _steps._iso_to_epoch(self.state.started_at)
        for pgid in list(self.state.owned_pgids):
            if not _steps._pgid_is_ours(pgid, started_after=started_after):
                # The pid may have been recycled while the sidecar was down.
                # Leaving an orphan is recoverable; SIGKILLing a stranger is not.
                self._event("recovery", {"pgid": pgid, "result": "SKIPPED_NOT_OURS"})
                self.state.owned_pgids.remove(pgid)
                continue
            try:
                _steps._killpg(pgid)
                self._event("recovery", {"pgid": pgid, "result": "KILLED"})
                self.state.owned_pgids.remove(pgid)
            except Exception:  # noqa: BLE001 — keep the id so the leak stays visible
                _LOG.exception("liverun %s killpg %s failed", self.state.run_id, pgid)
                self._event("recovery", {"pgid": pgid, "result": "FAILED"})
        for tid in list(self.state.owned_tunnels):
            tdef = self.profile.tunnels.get(tid)
            if self.ctx.tunnels is None or tdef is None:
                # No manager, or a profile that no longer declares this tunnel —
                # the spec can't even be rebuilt, so nothing was closed. Keep the
                # id: the ssh child (if any) is still out there.
                self._event("recovery", {"tunnel": tid, "tunnel_close": "ABSENT",
                                         "reason": "spec_unavailable"})
                continue
            try:
                closed = self.ctx.tunnels.close(_steps.tunnel_spec_for(tdef, self.ctx))
            except Exception:  # noqa: BLE001
                _LOG.exception("liverun %s tunnel close %s failed", self.state.run_id, tid)
                closed = False
            if closed:
                self._event("recovery", {"tunnel": tid, "tunnel_close": "PRESENT"})
                self.state.owned_tunnels.remove(tid)
            else:
                # `TunnelManager.close` returns False when its in-memory registry
                # has no such tunnel — exactly the case after a sidecar restart.
                # Nothing was closed, so nothing may be forgotten.
                self._event("recovery", {"tunnel": tid, "tunnel_close": "ABSENT",
                                         "reason": "not_registered"})

    def _consecutive_failures_hit(self) -> bool:
        """Ask the ledger ONLY the consecutive-failure question. The rate caps
        (gap/hourly/daily) all describe whether a run may *start*, and the run
        that just finished trips every one of them — leaving them in would mask
        the streak behind a `cap_gap` that is not about failures at all."""
        caps = replace(self.profile.caps, min_launch_gap_s=0,
                       max_launches_per_hour=_UNCAPPED, max_launches_per_day=_UNCAPPED)
        return self.ledger.check(self.profile.name, caps, self._wall()) == "cap_consecutive_failures"

    def _scan_ban(self, text: str, *, where: str) -> bool:
        for pat in self.profile.ban_signals:
            if re.search(pat, text or ""):
                if not self._banned:
                    self._banned = True
                    self._event("ban_signal", {"pattern": pat, "where": where})
                return True
        return False

    def _pause(self, why: str) -> None:
        marker = paused_marker(self.profile.name)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{why} {now_iso()} {self.state.run_id}\n")
        self._finish("paused_awaiting_human", why)

    # -- reporting --------------------------------------------------------- #
    def snapshot(self) -> dict[str, Any]:
        st = self.state
        now = self._clock()
        last_ok = self._probe_last_ok
        probes = {w.id: {"last_ok_age_s": (now - last_ok[w.id]) if w.id in last_ok else None,
                         "on_stall": w.on_stall, "stall_after_s": w.stall_after_s}
                  for w in self.profile.watch}
        launched = self.ctx.launched_monotonic
        return {"status": "live", "run_id": st.run_id, "profile": st.profile_name,
                "project_id": st.project_id, "phase": st.phase, "reason": st.reason,
                "step_index": st.step_index,
                "elapsed_s": (now - launched) if launched is not None else None,
                "probes": probes,
                # Headroom, stated as the refusal a relaunch would get right now.
                # `caps_disabled` names the switch explicitly -- `would_refuse:
                # None` alone is ambiguous between "the switch is on" and "no
                # cap would be hit anyway".
                "caps": {"would_refuse": self.ledger.check(self.profile.name, self.profile.caps,
                                                           self._wall()),
                        "caps_disabled": caps_disabled()},
                "literals": dict(st.literals),
                # The fix loop, stated the same way: where this run sits in a fix
                # chain, and how much autonomy is left before a human is needed.
                "fix_cycle": st.fix_cycle, "fix_of": st.fix_of,
                "fix_repo_id": st.fix_repo_id,
                "fix_cycles_today": self._fix_cycles_today(),
                "fix_cap": int(self.profile.fix_loop.max_fix_cycles_per_day)
                if self.profile.fix_loop is not None
                else int(_profile.FIX_CAP_DEFAULTS["max_fix_cycles_per_day"]),
                "fix_paused": fix_paused_marker(self.profile.name).exists()}

    def _fix_cycles_today(self) -> int:
        """Reporting only — a ledger this cannot read must not break `status`."""
        try:
            return int(self.ledger.fix_cycles_today(self.profile.name, self._wall()))
        except Exception:  # noqa: BLE001
            _LOG.exception("liverun %s could not count fix cycles", self.state.run_id)
            return -1

    # -- bookkeeping ------------------------------------------------------- #
    def _set_phase(self, phase: str, reason: str | None = None) -> None:
        self.state.phase = phase
        if reason is not None:
            self.state.reason = reason
        self._save()
        self._event("phase", {"to": phase, "reason": reason})

    def _finish(self, phase: str, reason: str | None) -> None:
        self.state.ended_at = now_iso()
        self._set_phase(phase, reason)

    def _save(self) -> None:
        self.store.save(self.state)

    def _event(self, kind: str, detail: dict[str, Any]) -> int:
        return self.store.append_event(self.state.run_id, kind, detail)


class LiveRunManager:
    """Registry of supervisors; the seam Slack and the server reach through.

    With no selector, `stop`/`status` address the most recently started live
    run — the single-live-run case the sidecar is actually operated in.
    """

    def __init__(self, *, store: RunStore | None = None, ledger: LaunchLedger | None = None,
                 tunnels: Any = None, remote: Any = None,
                 load_profile=None, fix_deps: FixDeps | None = None) -> None:
        self._store, self._ledger = store, ledger
        self._tunnels, self._remote = tunnels, remote
        self._load_profile = load_profile or _profile.load_profile
        # Handed to every supervisor this manager starts, so a fix cycle can be
        # driven against injected engine seams without reaching past the
        # manager to construct a Supervisor by hand. `None` keeps every seam on
        # its lazily-resolved production default.
        self._fix_deps = fix_deps
        # Re-entrant: `start` holds the lock across its own `_active()` call.
        self._lock = threading.RLock()
        self._runs: dict[str, Supervisor] = {}  # profile name -> latest supervisor

    def _deps(self) -> tuple[RunStore, LaunchLedger, Any, Any]:
        if self._store is None:
            self._store = RunStore()
        if self._ledger is None:
            self._ledger = LaunchLedger()
        if self._tunnels is None:
            from errorta_tunnels import tunnel_manager
            self._tunnels = tunnel_manager
        if self._remote is None:
            from errorta_tools.runner.remote import RemoteToolRunner
            self._remote = RemoteToolRunner()
        return self._store, self._ledger, self._tunnels, self._remote

    def _active(self) -> dict[str, Supervisor]:
        # Under the lock: `_runs` is mutated by `start` from whichever thread
        # asked (Slack, HTTP, the lifespan), and iterating a dict that another
        # thread is inserting into raises RuntimeError.
        with self._lock:
            return {k: s for k, s in self._runs.items() if s.state.phase not in TERMINAL_PHASES}

    def _find(self, profile_name: str | None, project_id: str | None) -> Supervisor | None:
        active = self._active()
        if profile_name:
            return active.get(profile_name)
        if project_id:
            return next((s for s in active.values() if s.state.project_id == project_id), None)
        if not active:
            return None
        return max(active.values(), key=lambda s: s.state.started_at)

    def start(self, profile_name: str, *, project_id: str | None = None,
              fix_of: str | None = None, fix_cycle: int = 0) -> dict[str, Any]:
        """``fix_of``/``fix_cycle`` only LABEL the new run; nothing else about
        this method changes, so a relaunch after a fix is refused by exactly the
        same caps as any other start."""
        if not _PROFILE_NAME_RE.match(profile_name or ""):
            return {"status": "refused", "reason": "bad_profile_name"}
        store, ledger, tunnels, remote = self._deps()
        try:
            prof = self._load_profile(_profile.profiles_dir() / f"{profile_name}.yaml")
        except _profile.ProfileError as exc:
            return {"status": "refused", "reason": f"profile_invalid:{exc.code}"}
        except OSError:
            return {"status": "refused", "reason": "profile_invalid:unreadable"}
        with self._lock:
            active = self._active()
            if profile_name in active:
                return {"status": "refused", "reason": "already_running",
                        "run_id": active[profile_name].state.run_id}
            if project_id and any(s.state.project_id == project_id for s in active.values()):
                return {"status": "refused", "reason": "project_has_live_run"}
            sup = Supervisor(prof, store=store, ledger=ledger, tunnels=tunnels, remote=remote,
                             project_id=project_id, fix_of=fix_of, fix_cycle=fix_cycle,
                             fix_deps=self._fix_deps,
                             # THIS manager, not the module singleton: a
                             # supervisor must relaunch into the registry that
                             # holds it, or the relaunch lands in a manager that
                             # will never stop or join it.
                             relaunch_fn=self.start)
            try:
                sup.start_background()
            except LiveRunRefused as exc:
                return {"status": "refused", "reason": exc.code}
            self._runs[profile_name] = sup
        return {"status": "started", "run_id": sup.state.run_id}

    def stop(self, *, profile_name: str | None = None, project_id: str | None = None,
             reason: str = "operator_stop") -> dict[str, Any]:
        sup = self._find(profile_name, project_id)
        if sup is None:
            return {"status": "empty"}
        sup.stop(reason)
        return {"status": "stopping", "run_id": sup.state.run_id}

    def status(self, *, profile_name: str | None = None, project_id: str | None = None) -> dict[str, Any]:
        sup = self._find(profile_name, project_id)
        if sup is None:
            with self._lock:
                runs = list(self._runs.values())
            last = max(runs, key=lambda s: s.state.started_at) if runs else None
            return {"status": "empty", "last": last.state.to_dict() if last else None}
        return sup.snapshot()

    def resume(self, profile_name: str) -> dict[str, Any]:
        if not _PROFILE_NAME_RE.match(profile_name or ""):
            return {"status": "refused", "reason": "bad_profile_name"}
        marker = paused_marker(profile_name)
        if not marker.exists():
            return {"status": "empty"}
        marker.unlink()
        # A human resumed: forgive the consecutive-failure streak, or the next
        # start is refused `cap_consecutive_failures` forever (live 2026-08-22).
        try:
            self._deps()[1].record_reset(profile_name, time.time())
        except Exception:  # noqa: BLE001
            _LOG.warning("resume: could not record the streak reset", exc_info=True)
        return {"status": "resumed"}

    def pause_fix(self, profile_name: str) -> dict[str, Any]:
        """Stop this profile fixing: the marker keeps future cycles out, and a
        live run is ASKED to abort a cycle already in flight (cancelling its dev
        run, withdrawing its staged acceptance).

        The marker alone was not enough. It is read by `_enter_fix_loop`, which
        a cycle already past has already passed — so a profile "paused" while
        one was awaiting acceptance would still have merged and deployed it,
        because the autopilot sweep fires on the PENDING confirmation the cycle
        left behind.

        ``"pausing"`` when a live run was asked, ``"paused"`` when there was no
        run to ask. The distinction is honest rather than cosmetic: the hold is
        in place either way, but the abort is done by the supervisor's own
        thread on its next tick (see `Supervisor.pause_fix`), so this call
        cannot truthfully say it has already happened. Nothing is check-then-act
        here for the same reason — whether a cycle is in flight is the
        supervisor thread's to decide, and a request it does not need is a
        no-op.

        Live runs in `launching`/`watching` keep running: this is a hold on
        autonomous MERGING, not on the supervisor. Idempotent — pausing an
        already-paused profile is still paused, because the operator's question
        was "is it off?", not "did I change something?"."""
        if not _PROFILE_NAME_RE.match(profile_name or ""):
            return {"status": "refused", "reason": "bad_profile_name"}
        marker = fix_paused_marker(profile_name)
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(now_iso(), encoding="utf-8")
        except OSError as exc:
            _LOG.exception("liverun could not write the fix-pause marker")
            return {"status": "error", "detail": type(exc).__name__}
        # Marker FIRST: if the abort lands the run terminal and something
        # relaunches, the relaunch must already see the profile paused.
        sup = self._active().get(profile_name)
        if sup is None:
            return {"status": "paused", "profile": profile_name}
        sup.pause_fix("fix_loop_paused")
        return {"status": "pausing", "profile": profile_name,
                "run_id": sup.state.run_id}

    def accept_is_staged(self, run_id: str, confirmation_id: str) -> bool:
        """Did a LIVE supervisor stage exactly this acceptance?

        The binding `errorta_slack.tools.accept_live_fix` refuses without. That
        verb is in the concierge's dispatch table, so a model can compose a call
        to it with any `run_id` it likes; `is_human_only` sees no `changed_paths`
        on such a call, answers False, and autopilot fires it. Requiring the
        confirmation id to be the one a non-terminal run is *currently waiting
        on* is what a chat turn cannot forge: the id is minted by
        `stage_confirmation` after the cycle built the args, and this manager is
        the only place that remembers it."""
        run_id, confirmation_id = str(run_id or ""), str(confirmation_id or "")
        if not run_id or not confirmation_id:
            return False
        for sup in self._active().values():
            if str(sup.state.run_id) != run_id:
                continue
            return str(sup.state.fix_confirmation_id or "") == confirmation_id
        return False

    def resume_fix(self, profile_name: str) -> dict[str, Any]:
        """Re-arm the fix loop. Human-only at the Slack layer
        (`tools.HUMAN_ONLY_VERBS`): re-arming autonomous merging is exactly the
        decision the loop must not make for itself."""
        if not _PROFILE_NAME_RE.match(profile_name or ""):
            return {"status": "refused", "reason": "bad_profile_name"}
        marker = fix_paused_marker(profile_name)
        if not marker.exists():
            return {"status": "empty"}
        try:
            marker.unlink()
        except OSError as exc:
            _LOG.exception("liverun could not clear the fix-pause marker")
            return {"status": "error", "detail": type(exc).__name__}
        return {"status": "resumed", "profile": profile_name}

    def teardown_all(self) -> None:
        for sup in list(self._active().values()):
            sup.stop("sidecar_shutdown")
        # Join every supervisor we ever started, not just the ones still
        # active — one that finished between the two loops still owns a thread.
        with self._lock:
            runs = list(self._runs.values())
        for sup in runs:
            sup.join(timeout=60)


live_run_manager = LiveRunManager()

__all__ = ["Supervisor", "LiveRunManager", "LiveRunRefused", "live_run_manager",
           "paused_marker", "fix_paused_marker"]
