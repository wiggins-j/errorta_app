"""Outbound cursor-poller — proactive push of coding-team state to Slack.

Polls the bound project's coding-team state (task-log activity via
``errorta_council.coding.team_log.build_team_log``, open ``attention``
signals via ``errorta_council.coding.attention.list_open``, and PR-ready
publish events via ``errorta_council.coding.publish_ledger``), diffs against
a durable per-channel cursor (``store.get_cursor``/``store.advance_cursor``),
and posts anything new:

* a **blocking** attention signal (``AttentionSignal.blocking is True``) is
  decision-needed — it's staged via ``store.stage_confirmation`` and posted
  as a buttoned ``render.decision_message``;
* everything else (team-log activity, non-blocking attention alerts, and
  PR-opened publish events) is terminal/FYI — posted as a plain
  ``render.fyi_message``.

``poll_once`` is exactly-once under a mid-loop ``poster`` failure: the
cursor is a JSON-encoded set of already-posted item markers, and it is
advanced ONE ITEM AT A TIME, immediately after that item is successfully
posted — never batched at the end. So if ``poster`` raises after posting
item N, items 1..N are already reflected in the cursor and a re-run only
posts the remainder.

This module also runs the Task 9 timeout auto-decide sweep (spec §5.9):
``store.pop_pending_older_than`` atomically claims every confirmation still
``pending`` after ``timeout_minutes`` (transitioning it to ``timed_out`` and
returning only what THIS call claimed), and ``sweep_timeouts`` applies each
one's decision — the conservative "don't act" choice for the two
irreversible tool classes (``spend_cloud``, ``publish_pr``), or the
confirmation's own declared ``args["on_timeout"]`` tag for everything else —
and posts the outcome back to its thread. Because ``pop_pending_older_than``
is the atomic claim, and ``store.resolve_confirmation`` (the button path,
see ``connection.handle_interaction``) is a SEPARATE atomic claim gated on
the SAME ``state == "pending"`` transition, the two paths can never both
fire a real effect for the same confirmation: whichever claims it first
(pending -> timed_out, or pending -> approved/declined) wins, and the other
sees a non-pending state and no-ops.

For an ``attention_signal``-class confirmation specifically, both paths
write the decision through to the REAL governance signal (not just an
announcement) via the injected ``attention_resolve_fn`` — see
``attention_decision_action`` for the decision -> ``attention.resolve``
action mapping and its documented edge case (no safe auto-action exists for
a "problem"-kind signal).

This module MUST NOT import ``slack_sdk`` at module load — the real Slack
API call lives entirely behind the injected ``poster``, matching every
other module in this optional bridge.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from errorta_council.coding import attention, team_log
from errorta_council.coding.ledger import LedgerStore
from errorta_slack import render
from errorta_slack import store as _slack_store
from errorta_slack import tools

_LOGGER = logging.getLogger(__name__)

# Confirmation "verb" staged by THIS module for a decision-needed attention
# signal — distinct from tools.py's real dispatchable tool verbs (there is
# no matching entry in tools.TOOL_CATALOG, and none is needed: resolving one
# of these routes through the injected `attention_resolve_fn`, in both
# connection.handle_interaction and sweep_timeouts, never through
# tools.dispatch). Public (not module-private) because connection.py
# branches on it by name.
ATTENTION_VERB = "attention_signal"

# The two irreversible tool classes ALWAYS default to the conservative
# ("don't act") choice on timeout, regardless of any declared on_timeout tag
# — spec §5.9's named example of cloud-spend / public-PR.
_CONSERVATIVE_VERBS = frozenset({"spend_cloud", "publish_pr"})

# The ONLY verbs `sweep_autopilot` may fire. `accept_live_fix` is here because
# the live-run fix cycle stages it from the supervisor's thread, where no chat
# turn exists to carry it to `connection._handle_staged_confirmations`. Every
# other C-class verb reaches that path and was already decided there.
AUTOPILOT_SWEEP_VERBS: frozenset[str] = frozenset({"accept_live_fix"})

# Per-class default when a staged confirmation's args carry no explicit
# "on_timeout" tag. Only consulted for verbs NOT in _CONSERVATIVE_VERBS
# (those are always forced to "declined" — see _timeout_decision).
_DEFAULT_ON_TIMEOUT = "declined"


def attention_decision_action(kind: str, decision: str) -> str | None:
    """The ``attention.resolve`` action a Slack decision maps to, for a
    signal of this ``kind`` — the write-through half of an
    ``attention_signal`` confirmation (used by both
    ``connection.handle_interaction`` and ``sweep_timeouts`` so approve,
    decline, and timeout all resolve through the identical mapping).

    ``"approved"`` -> ``"accept"`` (valid for every kind ``attention.py``
    currently defines — both ``"problem"`` and ``"alert"``).

    ``"declined"`` (including the timeout sweep's conservative default) ->
    the safest CLEARING action available for this kind: ``"dismiss"``
    preferred, ``"defer"`` as a fallback (per
    ``attention.VALID_ACTIONS["alert"]``).

    Returns ``None`` if no safe action exists for this kind — notably
    ``"problem"``, whose ``VALID_ACTIONS`` is only ``{"accept", "correct"}``.
    There is no no-op/clear action there, and this function deliberately
    does NOT fall back to ``"accept"`` on a decline/timeout: auto-accepting
    (which can spawn a task) is an active decision, the opposite of the
    conservative "don't act" default a decline is supposed to mean. Callers
    must treat ``None`` as "leave the signal open for a human", not as
    license to invent an action.
    """
    valid = attention.VALID_ACTIONS.get(kind, frozenset())
    if decision == "approved":
        return "accept" if "accept" in valid else None
    if "dismiss" in valid:
        return "dismiss"
    if "defer" in valid:
        return "defer"
    return None


def _default_publish_events_fn(project_id: str) -> list[Any]:
    from errorta_council.coding.publish_ledger import PublishLedger

    return PublishLedger(project_id).list_events()


def _default_liverun_events_fn(project_id: str) -> list[tuple[Any, list[dict[str, Any]]]]:
    """Every live run belonging to ``project_id``, with its full event log.

    Every phase, not just the live ones: a run that has already stopped or
    failed is precisely the run whose ending has not been announced yet, and
    ``RunStore.list_non_terminal`` would filter exactly those out.

    Imported at CALL time so ``errorta_slack.outbound`` -- a module that must
    stay importable with only the Slack bridge installed -- never depends on
    the supervisor package at load.
    """
    from errorta_liverun.state import RunStore

    run_store = RunStore()
    root = run_store._root
    if not root.is_dir():
        return []
    out: list[tuple[Any, list[dict[str, Any]]]] = []
    for run_dir in sorted(root.iterdir()):
        state = run_store.load(run_dir.name)
        if state is not None and state.project_id == project_id:
            out.append((state, run_store.events(state.run_id)))
    return out


@dataclass
class OutboundDeps:
    """Every coding-state seam ``poll_once``/``run_loop``/``sweep_timeouts``
    reach through — all injectable so tests run egress-free with fakes (no
    real engine calls, no network, no real time)."""

    store: Any = _slack_store
    ledger_factory: Callable[[str], Any] = LedgerStore
    team_log_fn: Callable[[Any], list[dict[str, Any]]] = team_log.build_team_log
    attention_list_open: Callable[..., list[Any]] = attention.list_open
    publish_events_fn: Callable[[str], list[Any]] = _default_publish_events_fn
    attention_resolve_fn: Callable[..., Any] = attention.resolve
    liverun_events_fn: Callable[[str], list[tuple[Any, list[dict[str, Any]]]]] = (
        _default_liverun_events_fn)
    # How `sweep_autopilot` runs the effect a claimed confirmation authorizes.
    # Production wires this to `connection.SlackBridge._fire_confirmed_effect`
    # -- the SAME method a button tap goes through -- rather than calling
    # `tools.dispatch(confirmed_via="block_actions")` here, because that marker
    # is set in exactly ONE place in this bridge and a second call site would
    # be a second thing to keep honest. `None` means no verified effect path is
    # wired up (no bridge running); the sweep then claims nothing at all,
    # because a claim with nothing to run it would tell the fix cycle its merge
    # was approved when nothing merged.
    fire_effect_fn: Callable[..., Any] | None = None


def _load_config(config_fn: Callable[[], dict[str, Any]] | None) -> dict[str, Any]:
    """This tick's bridge config. Imported at CALL time only when no explicit
    reader was injected, so a test drives the loop with a plain dict."""
    if config_fn is not None:
        try:
            return dict(config_fn() or {})
        except Exception:  # noqa: BLE001
            _LOGGER.warning("outbound: could not read the bridge config", exc_info=True)
            return {}
    from errorta_slack import config as slack_config

    try:
        return dict(slack_config.load() or {})
    except Exception:  # noqa: BLE001 - an unreadable config means autopilot OFF
        _LOGGER.warning("outbound: could not read the bridge config", exc_info=True)
        return {}


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` off a dict or an attribute-holding object. team_log
    entries are plain dicts; ``AttentionSignal`` and ``PublishEvent`` are
    dataclasses — one accessor so item-building doesn't care which."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


@dataclass
class _Item:
    marker: str
    sort_key: str
    kind: str  # "decision" | "fyi"
    title: str
    detail: str
    signal_id: str | None = None  # only set for kind == "decision"
    signal_kind: str | None = None  # "problem" | "alert" -- only set alongside signal_id
    # Ignores a channel's notification mute. The operator's requirement is that
    # the team stopping, hitting a roadblock, or finishing ALWAYS arrives; a
    # "stop updating me" must quiet routine progress, not hide the run ending.
    # A blocking `kind == "decision"` is mandatory by construction (it carries
    # an Approve/Decline the run is waiting on) and need not set this.
    mandatory: bool = False
    # A local file to attach alongside the item's text (a live-run evidence
    # screenshot); `title` carries the attachment's caption. Optional on BOTH
    # sides: an item may not have one, and a poster may not implement
    # `post_file` -- the text posts either way. See `_post_attachment`.
    file_path: str | None = None
    # A confirmation ALREADY staged by someone else (the live-run fix cycle,
    # from the supervisor's own thread) that this item must render a button
    # for. Set means "do not stage a second one" -- see `poll_once`.
    confirmation_id: str = ""


def _log_items(deps: "OutboundDeps", ledger_store: Any) -> list[_Item]:
    items: list[_Item] = []
    for entry in deps.team_log_fn(ledger_store):
        at = str(entry.get("at", ""))
        kind = str(entry.get("kind", ""))
        message = str(entry.get("message", ""))
        items.append(
            _Item(marker=f"log:{at}:{kind}", sort_key=at, kind="fyi",
                  title="", detail=message)
        )
    return items


def _attention_items(
    deps: "OutboundDeps", project_id: str, ledger_store: Any,
) -> list[_Item]:
    items: list[_Item] = []
    for sig in deps.attention_list_open(project_id, store=ledger_store):
        # ONLY blocking signals are channel messages.
        #
        # A non-blocking signal is a reviewer's note, not a milestone: one live
        # run raised 28 of them against a single spec ("Rounding method not
        # specified", "Custom tip field initial state unspecified", ...), and
        # each would have been its own Slack message in a channel designed to
        # carry 6-12. They are also already summarised -- team_log emits
        # "reviewed an artifact (N finding(s))" for the same review round -- and
        # the detail stays in the app, where it can actually be acted on.
        #
        # A blocking signal is the opposite: the run is halted waiting on the
        # button this item carries, so it posts even through a channel mute.
        if not bool(_get(sig, "blocking", False)):
            continue
        sig_id = str(_get(sig, "id", ""))
        title = str(_get(sig, "title", ""))
        summary = str(_get(sig, "summary", ""))
        items.append(
            _Item(
                marker=f"attn:{sig_id}", sort_key=str(_get(sig, "created_at", "")),
                kind="decision",
                title=title, detail=summary or title,
                signal_id=sig_id, signal_kind=str(_get(sig, "kind", "")),
            )
        )
    return items


def _publish_items(deps: "OutboundDeps", project_id: str) -> list[_Item]:
    items: list[_Item] = []
    for event in deps.publish_events_fn(project_id):
        if _get(event, "state") != "pr_opened":
            continue
        event_id = str(_get(event, "event_id", ""))
        created_at = str(_get(event, "created_at", ""))
        pr_url = _get(event, "pr_url") or ""
        detail = f"PR opened: {pr_url}" if pr_url else "A pull request was opened."
        items.append(
            _Item(marker=f"pub:{event_id}", sort_key=created_at, kind="fyi",
                  title="", detail=detail)
        )
    return items


# Run statuses that are an EVENT worth announcing. "idle"/"running" are
# ongoing conditions, not transitions: emitting them would post an item on
# every tick forever.
_TERMINAL_RUN_STATUSES = ("stopped", "failed")


def _run_state_items(ledger_store: Any) -> list[_Item]:
    """The team stopped / finished — an event no other source carries.

    team_log, attention and the publish ledger all describe work *within* a
    run; a run ENDING writes only run_state (``routes/coding.py`` on a clean
    stop, ``runner.py`` on a failure). Without this source two of the three
    events the operator requires could never fire.

    The marker pairs the status with ``ended_at`` rather than reading a clock,
    so it is stable across polls: re-polling an unchanged run state produces
    the same marker, which the cursor has already seen.
    """
    try:
        state = ledger_store.get_run_state() or {}
    except Exception:  # noqa: BLE001 - an unreadable ledger must not wedge the loop
        _LOGGER.warning("outbound: could not read run state", exc_info=True)
        return []
    status = str(state.get("status") or "")
    if status not in _TERMINAL_RUN_STATUSES:
        return []
    ended_at = str(state.get("ended_at") or "")
    # `last_error` is a raw `str(exc)` (runner.py) and `stop_reason` is
    # engine-authored: neither is operator text, and `render.fyi_message` does
    # NOT escape what it is given. Unescaped, a "<!channel>" inside an
    # exception string pings the entire workspace. Escape, then cap -- escaping
    # EXPANDS, so capping first would bound the wrong string.
    raw_reason = str(state.get("stop_reason") or state.get("last_error") or "").strip()
    reason = render.escape_mrkdwn(raw_reason)[:300] if raw_reason else ""
    if status == "stopped":
        detail = f"The team stopped — {reason}" if reason else "The team stopped."
    else:
        detail = f"The run failed — {reason}" if reason else "The run failed."
    return [
        _Item(marker=f"run:{status}:{ended_at}", sort_key=ended_at, kind="fyi",
              title="", detail=detail, mandatory=True)
    ]


# --------------------------------------------------------------------------
# Live-run supervisor items (spec 2026-08-21 §3.7).
#
# The supervisor writes its whole narrative to `runs/<id>/events.jsonl` and
# nothing else reads it, so without this source a live run launches, stalls,
# gets torn down and pauses awaiting a human entirely off-channel.
# --------------------------------------------------------------------------

# Kinds that post THROUGH a channel mute. The rule is the same one
# `_run_state_items` applies to the coding team: "stop updating me" quiets
# routine progress, it does not hide the run ending, the reason it ended, or a
# hold that is waiting on a person. `phase` is mandatory only for a TERMINAL
# destination -- a `phase -> watching` on every launch is exactly the routine
# noise a mute is for.
_LIVERUN_MANDATORY_KINDS = frozenset({
    "stall", "ban_signal", "caps", "literals",
    # Slice 2 (the fix loop). Each of these is either an autonomous change to
    # the operator's real files, the loop giving up, or the client not coming
    # back -- none of them is the routine progress a mute is for.
    "fix_idle_cancel",      # a dev run was cancelled mid-flight
    "fix_accept_staged",    # a merge is waiting on a decision RIGHT NOW
    "fix_accepted",         # code landed in the operator's files
    "fix_cycle_cap",        # the day's autonomy is spent; a human is needed
    "relaunch_refused",     # the fix shipped but the client did not come back
    "fix_aborted",          # a stop landed mid-cycle: what was left half-done?
    "fix_accept_withdrawn",  # a merge button that was posted has been taken back
})
# Deliberately a LOCAL copy of `errorta_liverun.state.TERMINAL_PHASES`, not an
# import of it: importing that module here would make the Slack bridge depend
# on the supervisor package at load, which `_default_liverun_events_fn` goes
# out of its way to avoid. `test_terminal_phases_do_not_drift` fails CI if the
# two ever disagree, which is the part a comment could not do.
_LIVERUN_TERMINAL_PHASES = frozenset(
    {"stopped", "failed", "paused_awaiting_human", "lost_on_restart"})

# Nothing in an event detail is operator-typed: profile names come off disk,
# stderr tails and matched ban patterns come from the game client and the
# brain. `render.fyi_message` does NOT escape what it is handed, so every
# field below goes through `_esc` -- and is capped AFTER escaping, because
# escaping EXPANDS (one `<` becomes four characters) and capping first would
# bound the wrong string.
_LIVERUN_FIELD_CAP = 300
_LIVERUN_DETAIL_CAP = 700
# A cut can land mid-entity ("&amp;" -> "&am"), which renders as literal junk.
# (connection.SlackBridge._cap_escaped does the same for approval copy; it
# also appends an approval-specific truncation note, which would be wrong in a
# progress line, so the two are deliberately not one function.)
_PARTIAL_ENTITY_RE = re.compile(r"&[A-Za-z]{0,4}$")


def _cap(escaped: str, cap: int) -> str:
    """Cap ALREADY-escaped text, without leaving a half-written entity."""
    if len(escaped) <= cap:
        return escaped
    return _PARTIAL_ENTITY_RE.sub("", escaped[:cap - 1]) + "…"


def _esc(text: Any, cap: int = _LIVERUN_FIELD_CAP) -> str:
    """Escape ``text`` for mrkdwn, then cap the ESCAPED result at ``cap``."""
    return _cap(render.escape_mrkdwn(str(text)), cap)


def _liverun_detail(kind: str, detail: dict[str, Any], profile: str) -> str:
    """One channel-ready line for one supervisor event."""
    if kind == "phase":
        to = _esc(detail.get("to") or "")
        reason = _esc(detail.get("reason") or "")
        line = f"Live run *{_esc(profile)}* → {to}"
        return f"{line} — {reason}" if reason else line
    if kind == "launch_step":
        name = _esc(detail.get("name") or "")
        if detail.get("check") is not None:
            return f"Launch step *{name}*: check {_esc(detail.get('check'))}"
        if detail.get("ok"):
            return f"Launch step *{name}*: ok"
        head = f"Launch step *{name}*: FAILED (rc={_esc(detail.get('exit_code'))})"
        tail = _esc(detail.get("stderr") or detail.get("stdout") or "")
        return f"{head}\n```{tail}```" if tail else head
    if kind == "probe_warn":
        return (f"⚠️ Probe *{_esc(detail.get('id'))}* quiet for "
                f"{_esc(detail.get('stalled_s'))}s — still watching")
    if kind == "stall":
        return (f"🛑 Stall on *{_esc(detail.get('id'))}* after "
                f"{_esc(detail.get('stalled_s'))}s — stopping")
    if kind == "evidence":
        outcome = "ok" if detail.get("ok") else _esc(detail.get("detail") or "failed")
        return f"Evidence *{_esc(detail.get('id'))}*: {outcome}"
    if kind == "teardown_step":
        outcome = "ok" if detail.get("ok") else "FAILED"
        line = f"Teardown *{_esc(detail.get('name'))}*: {outcome}"
        literal = detail.get("literal")
        if literal:
            seen = "PRESENT" if detail.get("ok") else "ABSENT"
            line += f" ({_esc(literal)}={seen})"
        return line
    if kind == "literals":
        parts = ", ".join(f"{_esc(k)}: {_esc(v)}" for k, v in detail.items())
        return f"Teardown literals — {parts}"
    if kind == "ban_signal":
        return (f"🚫 Ban-class signal matched (`{_esc(detail.get('pattern'))}`) — "
                "paused awaiting human. Look at the evidence before you resume.")
    if kind == "caps":
        return f"⛔ Launch cap hit: {_esc(detail.get('code'))} — paused awaiting human"
    if kind == "refused":
        return f"Live run refused: {_esc(detail.get('code'))}"
    # -- the fix loop (spec 2026-08-22 §3.5) ------------------------------- #
    if kind == "fix_skipped":
        return f"No fix cycle: {_esc(detail.get('code'))}"
    if kind == "fix_triage":
        return (f"🔎 Triage: *{_esc(detail.get('repo_id'))}* "
                f"({_esc(detail.get('confidence'))}) — {_esc(detail.get('rationale'))}")
    if kind == "fix_task":
        return (f"📋 Filed a dev task for *{_esc(detail.get('repo_id'))}* "
                f"in project `{_esc(detail.get('project_id'))}` "
                f"(gate: {_esc(detail.get('gate'))})")
    if kind == "fix_team_model":
        frm = ", ".join(_esc(r) for r in (detail.get("from") or []))
        return f"dev seat → {_esc(detail.get('to'))} (was {frm})"
    if kind == "fix_workspace_seeded":
        return f"seeded the project worktree from {_esc(detail.get('repo_path'))}"
    if kind == "fix_run":
        return (f"Fix run {_esc(detail.get('status'))} "
                f"({_esc(detail.get('mode'))}) on `{_esc(detail.get('project_id'))}`")
    if kind == "fix_idle_cancel":
        return (f"⏹️ The fix run went quiet for {_esc(detail.get('idle_s'))}s — "
                "cancelling it. Nothing will be merged.")
    if kind == "fix_accept_staged":
        who = ("A human has to approve this one"
               if detail.get("human_only") else "Waiting for approval")
        return (f"🔀 Fix ready to merge into *{_esc(detail.get('repo_id'))}* "
                f"({_esc(detail.get('n_paths'))} file(s)). {who}.")
    if kind == "fix_accepted":
        return (f"✅ Fix merged into *{_esc(detail.get('repo_id'))}* → "
                f"{_esc(detail.get('delivered_to'))} (head {_esc(detail.get('head'))})")
    if kind == "deploy_step":
        name = _esc(detail.get("name") or "")
        if detail.get("check") is not None:
            return f"Deploy step *{name}*: check {_esc(detail.get('check'))}"
        if detail.get("ok"):
            return f"Deploy step *{name}*: ok"
        head = f"Deploy step *{name}*: FAILED (rc={_esc(detail.get('exit_code'))})"
        tail = _esc(detail.get("tail") or "")
        return f"{head}\n```{tail}```" if tail else head
    if kind == "fix_cycle_cap":
        return (f"⛔ Fix cycles for today: {_esc(detail.get('cycles_today'))}/"
                f"{_esc(detail.get('cap'))} — paused awaiting human")
    if kind == "relaunch_refused":
        return (f"↩️ The fix shipped, but the relaunch was refused: "
                f"{_esc(detail.get('code'))}")
    if kind == "fix_accept_withdrawn":
        # `claimed: False` means somebody else answered the button first, so
        # the merge may well have happened -- saying "withdrawn" flatly there
        # would be the one thing this line must not get wrong.
        cid = _esc(detail.get("cid"), 64)
        if detail.get("claimed"):
            return f"↩️ Took back the pending merge approval (`{cid}`) — nothing merged."
        return (f"↩️ Tried to take back the merge approval (`{cid}`) — it was "
                "already answered; check whether it merged.")
    if kind == "fix_aborted":
        repo = _esc(detail.get("repo_id") or "the repo", 60)
        bits = []
        if detail.get("run_cancelled"):
            bits.append("the dev run was cancelled")
        if detail.get("accept_withdrawn"):
            bits.append("the merge approval was withdrawn")
        tail = f" — {', '.join(bits)}" if bits else " — nothing was in flight"
        return (f"⏹️ Fix cycle for *{repo}* abandoned at "
                f"`{_esc(detail.get('at'), 32)}` ({_esc(detail.get('reason'), 64)})"
                f"{tail}.")
    # An event kind this renderer has not been taught yet still reaches the
    # channel rather than vanishing -- a silent drop is how a supervisor's new
    # failure mode goes unnoticed for a week.
    return f"{_esc(kind)}: {_esc(json.dumps(detail, sort_keys=True))}"


def _accept_title(detail: dict[str, Any]) -> str:
    """The notification line for a staged acceptance. The human-only half is in
    the TITLE, not just the body: on a phone the title is often all that is
    read, and "a human has to do this" is the one thing that must not be the
    part that got collapsed."""
    repo = _esc(detail.get("repo_id") or "the repo", 60)
    suffix = " — human approval required" if detail.get("human_only") else ""
    return f"Merge the live-run fix into {repo}{suffix}"


def _liverun_attachment(kind: str, detail: dict[str, Any]) -> tuple[str, str] | None:
    """The (path, title) of a screenshot to upload alongside this item, if any.

    Only ``evidence`` refs, and only ``.png`` ones: a ref may equally be a log
    excerpt or a journal path, and handing an arbitrary supervisor-named file
    to an upload call is a wider door than this feature needs.
    """
    if kind != "evidence":
        return None
    for ref in detail.get("refs") or []:
        if str(ref).endswith(".png"):
            return str(ref), str(detail.get("id") or "evidence")
    return None


def _liverun_items(deps: "OutboundDeps", project_id: str) -> list[_Item]:
    try:
        runs = deps.liverun_events_fn(project_id)
    except Exception:  # noqa: BLE001
        # The run store is written by ANOTHER process. A half-written, absent
        # or unreadable one must degrade to "no live-run items", not wedge the
        # loop that also carries the coding team's progress.
        _LOGGER.warning("outbound: could not read live-run events", exc_info=True)
        return []
    items: list[_Item] = []
    for state, events in runs:
        profile = str(_get(state, "profile_name", "") or "")
        run_id = str(_get(state, "run_id", "") or "")
        for event in events:
            kind = str(event.get("kind", ""))
            detail = dict(event.get("detail") or {})
            seq = int(event.get("seq", 0))
            attachment = _liverun_attachment(kind, detail)
            # The fix cycle stages its OWN confirmation, from the supervisor's
            # thread, and then polls that id. There is no chat turn to carry
            # it, so `connection._handle_staged_confirmations` never sees it
            # and nothing else would ever post the button. Reuse the id it
            # staged: a second confirmation would be a button whose approval
            # the cycle is not waiting on.
            cid = str(detail.get("cid") or "") if kind == "fix_accept_staged" else ""
            items.append(_Item(
                marker=f"liverun:{run_id}:{seq}",
                # The seq is zero-padded into the SORT key (never the marker,
                # which is the durable cursor entry): same-second events tie on
                # `at`, and a plain-string tie-break would order seq 10 before
                # seq 2.
                sort_key=f"{event.get('at', '')}:{seq:09d}",
                kind="decision" if cid else "fyi",
                confirmation_id=cid,
                title=(_accept_title(detail) if cid
                       else (attachment[1] if attachment else "")),
                detail=_cap(_liverun_detail(kind, detail, profile),
                            _LIVERUN_DETAIL_CAP),
                mandatory=(kind in _LIVERUN_MANDATORY_KINDS
                           or (kind == "phase"
                               and str(detail.get("to")) in _LIVERUN_TERMINAL_PHASES)),
                file_path=attachment[0] if attachment else None,
            ))
    return items


def _current_items(deps: "OutboundDeps", project_id: str) -> list[_Item]:
    ledger_store = deps.ledger_factory(project_id)
    items = (
        _log_items(deps, ledger_store)
        + _attention_items(deps, project_id, ledger_store)
        + _publish_items(deps, project_id)
        + _run_state_items(ledger_store)
        + _liverun_items(deps, project_id)
    )
    # Stable, deterministic order (roughly chronological); the marker
    # breaks ties so equal-timestamp items always sort the same way.
    items.sort(key=lambda it: (it.sort_key, it.marker))
    return items


def current_marker_cursor(project_id: str, *, deps: "OutboundDeps | None" = None) -> str:
    """An encoded cursor covering everything that has already happened.

    Used at adopt time: seeding this makes the first poll a no-op for existing
    history, so an adopted project's channel starts from "what happens next"
    rather than replaying months of team log one message at a time.
    """
    real_deps = deps or OutboundDeps()
    return _encode_posted({it.marker for it in _current_items(real_deps, project_id)})


def _decode_posted(cursor: str | None) -> set[str]:
    if not cursor:
        return set()
    try:
        raw = json.loads(cursor)
    except (TypeError, ValueError):
        return set()
    if not isinstance(raw, list):
        return set()
    return {str(m) for m in raw}


def _encode_posted(posted: set[str]) -> str:
    return json.dumps(sorted(posted))


def _post_attachment(poster: Any, channel_id: str, item: _Item) -> None:
    """Upload ``item``'s file, if it has one and the poster can take one.

    ``post_file`` is an OPTIONAL part of the poster duck-type: the outbound
    tests' poster, and any poster built before evidence existed, has only
    ``post_message``. Both the missing method and a failed upload are
    non-events -- the item's text has already posted, and an attachment must
    never cost the operator the rest of the stream.
    """
    if not item.file_path or not hasattr(poster, "post_file"):
        return
    try:
        poster.post_file(channel_id, "", item.file_path, item.title or "evidence")
    except Exception:  # noqa: BLE001 - see the docstring
        _LOGGER.warning("outbound: evidence upload failed", exc_info=True)


def poll_once(
    channel_id: str, project_id: str, *, deps: "OutboundDeps", poster: Any,
) -> list[str]:
    """Diff the current coding-state fingerprint against the durable cursor
    and post whatever's new. Returns the markers newly posted THIS call.

    The cursor holds a JSON-encoded set of every marker already posted for
    ``channel_id`` (not a single high-water mark) so that an item leaving
    the "open" view later (e.g. an attention signal a human resolves) never
    shifts what counts as "new" for the rest — membership, not position, is
    the dedupe key.
    """
    posted = _decode_posted(deps.store.get_cursor(channel_id))
    items = _current_items(deps, project_id)
    muted = deps.store.updates_muted(channel_id)

    newly_posted: list[str] = []
    for item in items:
        if item.marker in posted:
            continue

        # A muted channel still gets the three the operator requires: the run
        # stopping/failing (item.mandatory) and a roadblock (kind ==
        # "decision", which the run is literally blocked on -- hiding it would
        # deadlock the team behind a button nobody can see).
        #
        # `continue` WITHOUT advancing the cursor: the marker must stay unseen
        # so unmuting delivers the backlog. Marking it posted here would
        # silently discard everything that happened while muted.
        if muted and not (item.mandatory or item.kind == "decision"):
            continue

        if item.kind == "decision":
            # An item may arrive with a confirmation SOMEONE ELSE already
            # staged (the live-run fix cycle, which polls the very id it
            # staged). Staging a second one here would post a button whose
            # approval nothing is waiting on -- and the rollback below would
            # then be resolving a record this module does not own.
            staged_here = not item.confirmation_id
            cid = item.confirmation_id or deps.store.stage_confirmation(
                ATTENTION_VERB,
                {
                    "signal_id": item.signal_id,
                    "project_id": project_id,
                    "signal_kind": item.signal_kind,
                    "class": "attention",
                    "on_timeout": _DEFAULT_ON_TIMEOUT,
                },
                "",
                channel_id=channel_id,
            )
            blocks = render.decision_message(
                item.title or "Decision needed", item.detail, cid,
            )
            try:
                poster.post_message(
                    channel_id, "", item.title or item.detail, blocks=blocks,
                )
            except Exception:
                # The confirmation was staged but never actually shown to a
                # human -- an orphan. Left as "pending", it would later get
                # auto-decided by sweep_timeouts into a confusing phantom
                # "timed out" message for a decision nobody ever saw. Roll it
                # back to a terminal, non-"pending" state the sweep's
                # pop_pending_older_than ignores (this call IS itself the
                # atomic claim -- resolve_confirmation only transitions a
                # still-"pending" record, so it can't clobber a real human
                # decision or a sweep that somehow already claimed it first).
                # Do NOT advance the cursor either -- a re-run must re-stage
                # (a fresh confirmation id) and try posting again from
                # scratch, not skip this item as "already handled".
                #
                # Only for a confirmation THIS call staged. A supervisor-staged
                # one belongs to a fix cycle that is still polling it: killing
                # it here would end that cycle over a transient Slack failure,
                # where leaving it pending lets the next poll (the cursor is
                # not advanced) post the button again.
                try:
                    if staged_here:
                        deps.store.resolve_confirmation(cid, "undelivered")
                except Exception:
                    _LOGGER.exception(
                        "outbound: failed to roll back orphaned confirmation "
                        "%s after a poster failure", cid,
                    )
                raise
        else:
            blocks = render.fyi_message(item.detail)
            poster.post_message(channel_id, "", item.detail, blocks=blocks)
            _post_attachment(poster, channel_id, item)

        # Record the marker as posted BEFORE moving on to the next item —
        # and persist the cursor immediately (not batched) — so a `poster`
        # failure on a LATER item never causes THIS item to be re-posted on
        # retry.
        posted.add(item.marker)
        deps.store.advance_cursor(channel_id, _encode_posted(posted))
        newly_posted.append(item.marker)

    return newly_posted


def _timeout_decision(record: dict[str, Any]) -> tuple[str, str]:
    """The (decision, human-readable reason) for a confirmation that has
    just timed out, per spec §5.9."""
    verb = str(record.get("verb", ""))
    if verb in _CONSERVATIVE_VERBS:
        return (
            "declined",
            f"{verb} is irreversible and nobody decided in time — "
            "defaulting to the conservative choice (don't act)",
        )
    args = record.get("args") or {}
    on_timeout = args.get("on_timeout")
    if on_timeout in ("approved", "declined"):
        return on_timeout, f"applying its declared on_timeout ({on_timeout})"
    return (
        _DEFAULT_ON_TIMEOUT,
        "no on_timeout was declared for this class — defaulting to decline",
    )


def _resolve_attention_on_timeout(
    deps: "OutboundDeps", record: dict[str, Any], decision: str,
) -> str | None:
    """Write the sweep's timeout decision through to the REAL attention
    signal (spec §5.9 rule 3: an auto-decision must be written through the
    coding store, not just announced) — without this, the governance signal
    stays open and the pipeline stays stuck even though Slack says
    "declined". Returns an override note to use in place of the normal
    timeout reason if the write-through couldn't happen (no safe action, or
    the resolve call itself failed), else ``None`` (the normal reason text
    stands unchanged)."""
    args = record.get("args") or {}
    project_id = str(args.get("project_id") or "")
    signal_id = str(args.get("signal_id") or "")
    signal_kind = str(args.get("signal_kind") or "")
    action = attention_decision_action(signal_kind, decision)
    if action is None:
        _LOGGER.warning(
            "outbound: no safe attention.resolve action for signal %s "
            "(kind=%r, decision=%r) -- leaving it open for a human",
            signal_id, signal_kind, decision,
        )
        return "no automatic action was safe for this signal — it's still open for you"
    try:
        deps.attention_resolve_fn(
            project_id, signal_id, action, by="slack-timeout",
            store=deps.ledger_factory(project_id),
        )
    except Exception:
        _LOGGER.exception(
            "outbound: attention_resolve_fn failed for signal %s (action=%r)",
            signal_id, action,
        )
        return "resolving it automatically failed — please check the project"
    return None


def sweep_timeouts(
    *, deps: "OutboundDeps", poster: Any, timeout_minutes: float,
    now: float | None = None,
) -> list[str]:
    """Atomically claim every confirmation pending longer than
    ``timeout_minutes``, write each one's auto-decision through to the real
    store (for ``attention_signal`` confirmations — see
    ``_resolve_attention_on_timeout``), and post the outcome to its thread.
    Returns the claimed confirmation ids.

    ``store.pop_pending_older_than`` IS the atomic claim — it transitions
    matching records from ``pending`` to ``timed_out`` and returns only the
    ones THIS call claimed, so this sweep can never double-fire against a
    human's button click (``connection.handle_interaction``'s own atomic
    claim via ``store.resolve_confirmation`` loses cleanly if the sweep won
    first, and vice versa). This sweep never itself calls ``tools.dispatch``
    — the conservative default for an irreversible tool verb IS "the effect
    never runs"; only a human Approve ever fires one. ``attention_signal``
    confirmations are the one exception: there IS a real write-through for
    them (``attention_resolve_fn``), because the whole point of a Problem/
    Alert signal is that the pipeline stays blocked until it's resolved one
    way or another — announcing a decision without writing it through would
    leave spec §5.9's block in place forever.
    """
    claimed = deps.store.pop_pending_older_than(timeout_minutes * 60, now=now)
    handled: list[str] = []
    for record in claimed:
        decision, reason = _timeout_decision(record)
        verb = str(record.get("verb", ""))
        channel_id = str(record.get("channel_id") or "")
        thread_ts = str(record.get("thread_ts") or "")

        if verb == ATTENTION_VERB:
            override = _resolve_attention_on_timeout(deps, record, decision)
            if override is not None:
                reason = override

        text = f"⏰ *{verb}* timed out — I decided *{decision}* because {reason}."
        try:
            poster.post_message(channel_id, thread_ts, text, blocks=render.fyi_message(text))
        except Exception:
            _LOGGER.exception(
                "outbound: failed to post timeout decision for confirmation %s",
                record.get("id"),
            )
        handled.append(str(record.get("id")))
    return handled


def sweep_autopilot(
    channel_id: str, project_id: str, *, deps: "OutboundDeps", poster: Any,
    config: dict[str, Any] | None = None,
) -> list[str]:
    """Fire the confirmations autopilot would have fired on the inbound path,
    for the ones that never take an inbound path at all. Returns the ids
    claimed by THIS call.

    The live-run fix cycle stages its acceptance from the supervisor's own
    thread. `connection._handle_staged_confirmations` only ever sees
    confirmations that came back inside a *chat turn's* tool results, so with
    autopilot on and no human watching, the one loop autopilot exists for would
    still sit at a button until it timed out.

    Three narrowings, each load-bearing:

    * ``AUTOPILOT_SWEEP_VERBS`` — never a blanket "approve what's pending".
      Every other C-class verb DOES reach the inbound path and was already
      decided there; sweeping them would approve, from a background timer,
      actions a person declined to answer.
    * ``tools.is_human_only`` — the same predicate the inbound path uses, on
      the same staged record, so a guarded diff needs a human tap whether the
      confirmation came from a chat turn or from the supervisor.
    * ``resolve_confirmation`` IS the claim. A human tap or the timeout sweep
      racing us wins cleanly and this call does nothing.

    The effect runs through ``deps.fire_effect_fn`` — production wires that to
    the bridge's ``_fire_confirmed_effect``, the exact method a button tap goes
    through. With no fire path wired up nothing is claimed at all: a claimed
    confirmation says "approved" to the fix cycle, and saying that about a
    merge nothing ran would send it on to deploy an unmerged tree.
    """
    if not bool((config or {}).get("autopilot")):
        return []
    fire = deps.fire_effect_fn
    if fire is None:
        _LOGGER.debug("outbound: autopilot sweep has no verified effect path; skipping")
        return []
    fired: list[str] = []
    try:
        pending = dict(deps.store.list_pending())
    except Exception:  # noqa: BLE001 - an unreadable store must not wedge the tick
        _LOGGER.warning("outbound: could not read pending confirmations", exc_info=True)
        return []
    for cid, record in pending.items():
        verb = str(record.get("verb") or "")
        args = dict(record.get("args") or {})
        if verb not in AUTOPILOT_SWEEP_VERBS or tools.is_human_only(verb, args):
            continue
        # One tick sweeps every bound channel in turn; only this channel's own
        # confirmations are this channel's business.
        if str(record.get("channel_id") or "") != str(channel_id):
            continue
        try:
            claimed_record, claimed = deps.store.resolve_confirmation(cid, "approved")
        except Exception:  # noqa: BLE001
            _LOGGER.exception("outbound: could not claim confirmation %s", cid)
            continue
        if not claimed:            # a human tap or the timeout sweep won the race
            continue
        fired.append(cid)
        thread_ts = str(claimed_record.get("thread_ts") or "")
        try:
            result = fire(claimed_record, channel_id=str(channel_id),
                          thread_ts=thread_ts, verb=verb, decision="approved",
                          approved=True)
            text = _autopilot_outcome_text(verb, result)
        except Exception:  # noqa: BLE001 - the claim is spent; say so and move on
            _LOGGER.exception(
                "outbound: autopilot sweep failed to execute %s (confirmation %s)",
                verb, cid)
            text = (f"⚠️ Autopilot approved *{verb}* and it failed — "
                    "check the project before trusting the run.")
        try:
            poster.post_message(channel_id, thread_ts, text,
                                blocks=render.fyi_message(text))
        except Exception:  # noqa: BLE001
            _LOGGER.exception("outbound: could not post the autopilot outcome for %s", cid)
    return fired


def _autopilot_outcome_text(verb: str, result: Any) -> str:
    """One honest audit line. A verb that "ran" but reported a not-done status
    (a blocked merge gate is the whole point of this one) must never be
    announced as executed — the vocabulary comes from
    ``tools.NOT_DONE_STATUSES``, not from a second list here."""
    status = str((result or {}).get("status") or "") if isinstance(result, dict) else ""
    if status in tools.NOT_DONE_STATUSES:
        return f"🤖 Autopilot approved *{verb}* — it did NOT happen: {status}."
    where = str((result or {}).get("delivered_to") or "") if isinstance(result, dict) else ""
    tail = f" → {render.escape_mrkdwn(where)[:200]}" if where else ""
    return f"🤖 Autopilot approved & executed *{verb}*{tail}."


async def run_loop(
    *,
    bindings_provider: Callable[[], Any],
    deps: "OutboundDeps",
    poster: Any,
    interval_s: float = 15,
    timeout_minutes: float = 30,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now_fn: Callable[[], float] = time.time,
    stop_event: "asyncio.Event | None" = None,
    config_fn: Callable[[], dict[str, Any]] | None = None,
) -> None:
    """A timer loop: each tick, poll every active binding and run the
    timeout sweep, then sleep ``interval_s`` before the next tick.

    Every source of real time / async wait is injectable (``sleep_fn``,
    ``now_fn``) so tests drive it deterministically with fakes — no real
    clock, no real sleeping. ``bindings_provider`` may be sync or async (it
    is awaited if it returns a coroutine) and should return an iterable of
    ``{"channel_id": ..., "project_id": ...}`` (dicts or objects with those
    attributes). Runs until ``stop_event`` is set; with no ``stop_event`` it
    runs forever (real production use — callers doing a bounded test always
    pass one).
    """
    while stop_event is None or not stop_event.is_set():
        try:
            cfg = _load_config(config_fn)
            bindings = bindings_provider()
            if asyncio.iscoroutine(bindings):
                bindings = await bindings
            for binding in bindings or []:
                channel_id = _get(binding, "channel_id")
                project_id = _get(binding, "project_id")
                if not channel_id or not project_id:
                    continue
                poll_once(channel_id, project_id, deps=deps, poster=poster)
                # AFTER the poll, so the button for a just-staged acceptance is
                # in the channel before autopilot answers it -- the audit line
                # then reads as a decision on something visible, not a merge
                # nobody was shown. Read the config every tick (not once at
                # start) so turning autopilot off takes effect on the next
                # tick, like it does on the inbound path.
                sweep_autopilot(channel_id, project_id, deps=deps, poster=poster,
                                config=cfg)

            sweep_timeouts(
                deps=deps, poster=poster, timeout_minutes=timeout_minutes,
                now=now_fn(),
            )
        except Exception:
            # A single bad tick (a transient poster/engine failure) must not
            # kill the whole loop -- log it and keep polling on the next
            # interval.
            _LOGGER.exception("outbound: run_loop tick failed")

        if stop_event is not None and stop_event.is_set():
            return
        await sleep_fn(interval_s)


__all__ = [
    "OutboundDeps",
    "ATTENTION_VERB",
    "AUTOPILOT_SWEEP_VERBS",
    "attention_decision_action",
    "poll_once",
    "current_marker_cursor",
    "sweep_timeouts",
    "sweep_autopilot",
    "run_loop",
]
