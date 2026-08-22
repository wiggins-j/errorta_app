"""Boot reconcile: any non-terminal live run is torn down, never resumed (spec §3.6, F-H).

A sidecar restart severs every handle the supervisor held — the daemon thread is
gone, the local children are orphans, the remote brain is still running against a
tunnel that no longer exists. Nothing about that is recoverable into a healthy
run, so recovery does the one honest thing: run the profile's own teardown against
the PERSISTED owned resources, kill whatever is left, report the literals, and
mark the run ``lost_on_restart``.

Failing closed matters more here than anywhere else in the module: a profile that
has since become invalid (edited, moved, deleted) must STILL kill the pgids,
signal the remote pidfiles and close the tunnels the old run wrote down — it just
can't run teardown steps it can no longer read, so ``logoff_verified`` is reported
ABSENT rather than assumed.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from . import profile as _profile
from . import steps as _steps
from .fixloop import ACCEPT_WITHDRAW_DECISION, _default_resolve_confirmation
from .state import RunState, RunStore, now_iso
from .supervisor import Supervisor

_LOG = logging.getLogger("errorta.liverun")

# TERM, wait, then KILL: the same escalation a profile's own teardown uses. The
# remote process is someone else's box — give it a chance to log off cleanly
# before the hammer.
_SIGNAL_GRACE_S = 10.0
_SIGNAL_TIMEOUT_S = 20.0


class _NullLedger:
    """Recovery touches the caps ledger not at all.

    It is not a launch — a restart storm must never silently exhaust the
    profile's budget — so ``check`` always says "no objection" and ``record``
    is a programming error.

    It records no OUTCOME either. A run lost to a sidecar restart is a run we
    know nothing about: it may have been perfectly healthy a millisecond
    earlier. Writing `failed=False` would reset a genuine consecutive-failure
    streak (a restart would launder two bad cycles into a clean slate), and
    writing `failed=True` would blame the profile for our own restart.
    `LaunchLedger.check` skips launches that have no outcome row when it counts
    the streak, so saying nothing is the honest answer — and the only one that
    leaves the streak exactly as it was."""

    def record_outcome(self, run_id: str, *, failed: bool) -> None:
        _LOG.debug("recovery: not recording an outcome for lost run %s", run_id)

    def record(self, *a: Any, **k: Any) -> None:  # pragma: no cover - never called
        raise AssertionError("recovery must not record a launch")

    def check(self, *a: Any, **k: Any) -> None:
        return None

    def record_fix_cycle(self, *a: Any, **k: Any) -> None:  # pragma: no cover
        raise AssertionError("recovery must not record a fix cycle")

    def fix_cycles_today(self, *a: Any, **k: Any) -> int:
        # A recovered run never enters the fix loop (its reason is
        # `sidecar_restart`, which is not fixable), but the supervisor reads
        # this in `snapshot`, so answering is cheaper than an AttributeError.
        return 0


def _signal_remote_pidfiles(sup: Supervisor, state: RunState) -> None:
    """TERM-then-KILL every remote pid this run wrote down. The profile's own
    teardown steps have already had their turn; this is the backstop for a run
    whose profile no longer parses, or whose teardown never reached the signal
    step because the sidecar died mid-launch.

    A ref is dropped from the state ONLY when the signal actually went through.
    An unreachable host is a remote process still running against a tunnel that
    no longer exists — forgetting its pidfile would erase the only record of
    it, so it stays in the (now terminal) state where a human can find it."""
    for ref in list(state.owned_remote_pidfiles):
        host, pidfile = ref.get("host"), ref.get("pidfile")
        if not host or not pidfile:
            sup._event("recovery", {"remote_pidfile": ref, "result": "SKIPPED_MALFORMED"})
            state.owned_remote_pidfiles.remove(ref)
            continue
        action = _profile.Action("remote_signal", {
            "host": host, "pidfile": pidfile, "signal": "TERM",
            "grace_s": _SIGNAL_GRACE_S, "then": "KILL"})
        try:
            res = sup._run_action(action, sup.ctx, timeout_s=_SIGNAL_TIMEOUT_S)
            ok = bool(res.ok)
            detail = res.detail
        except Exception as exc:  # noqa: BLE001 — one unreachable host must not
            # strand the pgids and tunnels of the same run behind it
            _LOG.warning("recovery: remote signal failed for %s", ref, exc_info=True)
            ok, detail = False, type(exc).__name__
        sup._event("recovery", {"remote_pidfile": ref, "ok": ok,
                                "result": "SIGNALLED" if ok else "FAILED", "detail": detail})
        if ok:
            state.owned_remote_pidfiles.remove(ref)


def _withdraw_staged_accept(state: RunState, store: RunStore, resolve_fn) -> None:
    """Take back an acceptance the dead sidecar staged and never answered.

    This is the one thing recovery must do BEFORE anything slow: a pending
    ``accept_live_fix`` is a merge + deliver that Slack's autopilot sweep will
    still fire, on behalf of a run that no longer exists. The store's resolve is
    the atomic claim, so a human who tapped Approve in the meantime wins and is
    reported as the winner."""
    cid = state.fix_confirmation_id
    if not cid:
        return
    detail: dict[str, Any] = {"cid": cid, "decision": ACCEPT_WITHDRAW_DECISION,
                              "where": "boot_recovery"}
    try:
        result = resolve_fn(cid, ACCEPT_WITHDRAW_DECISION)
        detail["claimed"] = bool(result[1]) if isinstance(result, tuple) and len(result) == 2 \
            else False
    except Exception as exc:  # noqa: BLE001 — an unknown/unreadable cid is not fatal
        _LOG.warning("recovery: could not withdraw confirmation %s", cid, exc_info=True)
        detail["claimed"] = False
        detail["error"] = type(exc).__name__
    state.fix_confirmation_id = None
    store.save(state)
    store.append_event(state.run_id, "fix_accept_withdrawn", detail)


def recover_on_boot(*, store: RunStore | None = None, tunnels: Any = None, remote: Any = None,
                    load_profile: Callable[[Path], _profile.Profile] | None = None,
                    run_action=_steps.run_action,
                    run_check=_steps.run_check,
                    resolve_confirmation_fn=None) -> list[str]:
    """Tear down every non-terminal run left by a prior sidecar. Returns the
    run ids marked ``lost_on_restart``."""
    store = store or RunStore()
    load = load_profile or _profile.load_profile
    if tunnels is None:
        from errorta_tunnels import tunnel_manager
        tunnels = tunnel_manager
    if remote is None:
        from errorta_tools.runner.remote import RemoteToolRunner
        remote = RemoteToolRunner()
    resolve_fn = resolve_confirmation_fn or _default_resolve_confirmation
    lost: list[str] = []
    for state in store.list_non_terminal():
        try:
            _recover_one(state, store=store, load=load, tunnels=tunnels, remote=remote,
                         run_action=run_action, run_check=run_check, resolve_fn=resolve_fn)
        except Exception:  # noqa: BLE001 — one poisoned run must not skip the rest
            _LOG.exception("recovery: unrecoverable failure for %s", state.run_id)
        lost.append(state.run_id)
    return lost


def _recover_one(state: RunState, *, store: RunStore, load, tunnels: Any, remote: Any,
                 run_action, run_check, resolve_fn=_default_resolve_confirmation) -> None:
    store.append_event(state.run_id, "phase", {"to": "recovering", "reason": "sidecar_restart"})
    _withdraw_staged_accept(state, store, resolve_fn)
    prof: _profile.Profile | None = None
    try:
        prof = load(_profile.profiles_dir() / f"{state.profile_name}.yaml")
    except Exception as exc:  # noqa: BLE001 — an unreadable profile is expected, not fatal
        store.append_event(state.run_id, "recovery",
                           {"profile": "unavailable", "error": str(exc)[:200]})
    if prof is None:
        # No teardown steps to run, but the owned resources below still die and
        # `logoff_verified` is reported ABSENT — never assumed.
        prof = _profile.Profile(state.profile_name, {}, {}, (), (), (), (),
                                _profile.DEFAULT_CAPS, ())
    sup = Supervisor(prof, store=store, ledger=_NullLedger(), tunnels=tunnels,
                     remote=remote, project_id=state.project_id,
                     run_action=run_action, run_check=run_check)
    # Bind the throwaway supervisor to the PERSISTED state so the owned
    # resources of the LOST run — not the empty ones of this stand-in — are what
    # teardown reaches. `ctx` has to be rebuilt for the same reason: it aliases
    # the state's own lists.
    sup.state = state
    sup.ctx = _steps.Ctx(
        profile=prof, run_id=state.run_id, session_id=state.session_id,
        evidence_dir=Path(state.evidence_dir or store.evidence_dir(state.run_id)),
        tunnels=tunnels, remote=remote, owned_pgids=state.owned_pgids,
        owned_remote_pidfiles=state.owned_remote_pidfiles, owned_tunnels=state.owned_tunnels,
        last_values=state.probe_last_value, launched_monotonic=None)
    sup._stop_reason = "sidecar_restart"
    try:
        sup._do_stopping(final_phase="lost_on_restart")
    except Exception:  # noqa: BLE001 — a teardown that throws must not stop the kills below
        _LOG.exception("recovery: teardown failed for %s", state.run_id)
    # `_do_stopping`'s own `finally` has already killed the pgids and closed the
    # tunnels. The remote pidfiles are the backstop that has to come AFTER the
    # profile's graceful logoff steps had their turn — it goes over its own ssh
    # connection, not through any reverse tunnel, so the closed tunnels above
    # don't matter to it.
    _signal_remote_pidfiles(sup, state)
    if state.phase != "lost_on_restart":
        # `_do_stopping` didn't get to finish the run (it threw), or it paused
        # the PROFILE on a ban signal it found in the evidence. Either way the
        # RUN is lost: the paused marker `_pause` wrote is what keeps the human
        # gate armed, so overwriting the phase here loses nothing.
        state.ended_at = state.ended_at or now_iso()
        state.phase = "lost_on_restart"
        state.reason = "sidecar_restart"
        store.append_event(state.run_id, "phase",
                           {"to": "lost_on_restart", "reason": "sidecar_restart"})
    store.save(state)


__all__ = ["recover_on_boot"]
