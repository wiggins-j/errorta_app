"""The live-run state machine (spec §3.6). One daemon thread per run.

idle -> launching(step i) -> watching -> stopping(reason) -> stopped
                                                          -> paused_awaiting_human
step failed ----------------------------------------------> failed

Every exit path — stall, launch failure, operator stop, supervisor crash —
goes through ``_do_stopping``: evidence first, then teardown, then the
``literals`` verdict. Nothing here retries a launch; a refusal or a failure
ends the cycle and the caps ledger decides whether another one may start.

Time is wall-clock throughout: stalls are ``now - last_ok`` on the injected
clock, never a tick count, so a slow or blocked tick can't fake liveness.

Event kinds appended to the run's ``events.jsonl``: ``phase``, ``launch_step``,
``probe_warn``, ``probe_error``, ``stall``, ``evidence``, ``teardown_step``,
``literals``, ``caps``, ``ban_signal``, ``refused``.
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
from .state import TERMINAL_PHASES, LaunchLedger, RunState, RunStore, now_iso

_LOG = logging.getLogger("errorta.liverun")
_CHECK_POLL_S = 2.0
_TICK_S = 1.0
_WATCH_SAVE_S = 30.0
_UNCAPPED = 1 << 30
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class LiveRunRefused(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def paused_marker(profile_name: str) -> Path:
    """Marker file that refuses every start of ``profile_name`` until an
    operator resumes it. Written on a ban signal or a consecutive-failure cap
    — the two conditions a human, not a retry loop, has to clear."""
    return errorta_home() / "liverun" / "paused" / profile_name


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
                 run_probe=_steps.run_probe) -> None:
        self.profile = profile
        self.store = store
        self.ledger = ledger
        self._clock = clock          # monotonic: timeouts, stalls, elapsed
        self._wall = wall            # epoch: the ledger, and check `step_start`
        self._stop = threading.Event()
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
        self._stopping = False
        self._closed = False
        self._banned = False
        self._warned: set[str] = set()
        self._probe_next: dict[str, float] = {}
        self._probe_last_ok: dict[str, float] = {}
        self._last_watch_save: float | None = None
        self._step_started: float | None = None       # monotonic
        self._step_started_wall: float = 0.0          # epoch
        rid = store.new_run_id()
        self.state = RunState(
            run_id=rid, profile_name=profile.name, project_id=project_id, phase="idle",
            reason=None, session_id=f"lr-{rid}", step_index=0, started_at=now_iso(),
            launched_at=None, ended_at=None, evidence_dir=str(store.evidence_dir(rid)))
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
        if code:
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
        """Never gated: the next tick from any non-terminal phase tears down."""
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
        if self._stop.is_set() or ph == "stopping":
            self._do_stopping()
        elif ph == "launching":
            self._tick_launch()
        elif ph == "watching":
            self._tick_watch()

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
                    if res.exit_code == 3:
                        # The brain refused the launch (risk budget). Not an
                        # error to retry — a decision to respect.
                        reason += ":refused"
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
                ok = bool(self._run_probe(w.probe, self.ctx))
            except Exception as exc:  # noqa: BLE001 — a probe that throws is a probe that isn't ok
                ok = False
                self._event("probe_error", {"id": w.id, "error": type(exc).__name__,
                                            "detail": str(exc)[:200]})
            if ok:
                self._probe_last_ok[w.id] = now
                stamp = now_iso()
                dirty = dirty or self.state.probe_last_ok.get(w.id) != stamp
                self.state.probe_last_ok[w.id] = stamp
                self._warned.discard(w.id)
                continue
            stalled_for = now - self._probe_last_ok.get(w.id, now)
            if stalled_for < w.stall_after_s:
                continue
            if w.on_stall == "warn":
                if w.id not in self._warned:      # one warning per stall episode
                    self._warned.add(w.id)
                    self._event("probe_warn", {"id": w.id, "stalled_s": stalled_for})
                continue
            self._event("stall", {"id": w.id, "stalled_s": stalled_for})
            self._stop_reason = f"stall:{w.id}"
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
        if self._stopping:
            # Re-entered while a first pass was unwinding — the crash handler in
            # `run_once_blocking` comes back through here. Never replay evidence
            # or teardown, but do finish the closing sequence the first pass
            # dropped (`_close_out` is itself idempotent).
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
        for stage, fn in (("kill_owned", self._kill_owned),
                          ("save", self._save),
                          ("outcome", lambda: self._record_outcome(final_phase, reason)),
                          ("literals", self._emit_literals)):
            try:
                fn()
            except Exception:  # noqa: BLE001
                _LOG.exception("liverun %s close-out stage %s failed", self.state.run_id, stage)
        try:
            if self._banned:
                self._pause("ban_signal")
            elif self._consecutive_failures_hit():
                self._event("caps", {"code": "cap_consecutive_failures"})
                self._pause("cap_consecutive_failures")
            else:
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
                "caps": {"would_refuse": self.ledger.check(self.profile.name, self.profile.caps,
                                                           self._wall())},
                "literals": dict(st.literals)}

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
                 load_profile=None) -> None:
        self._store, self._ledger = store, ledger
        self._tunnels, self._remote = tunnels, remote
        self._load_profile = load_profile or _profile.load_profile
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

    def start(self, profile_name: str, *, project_id: str | None = None) -> dict[str, Any]:
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
                             project_id=project_id)
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
        return {"status": "resumed"}

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

__all__ = ["Supervisor", "LiveRunManager", "LiveRunRefused", "live_run_manager", "paused_marker"]
