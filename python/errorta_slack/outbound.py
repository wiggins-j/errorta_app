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
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from errorta_council.coding import attention, team_log
from errorta_council.coding.ledger import LedgerStore
from errorta_slack import render
from errorta_slack import store as _slack_store

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
        sig_id = str(_get(sig, "id", ""))
        sig_kind = str(_get(sig, "kind", ""))
        created_at = str(_get(sig, "created_at", ""))
        title = str(_get(sig, "title", ""))
        summary = str(_get(sig, "summary", ""))
        blocking = bool(_get(sig, "blocking", False))
        items.append(
            _Item(
                marker=f"attn:{sig_id}", sort_key=created_at,
                kind="decision" if blocking else "fyi",
                title=title, detail=summary or title,
                signal_id=sig_id, signal_kind=sig_kind,
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


def _current_items(deps: "OutboundDeps", project_id: str) -> list[_Item]:
    ledger_store = deps.ledger_factory(project_id)
    items = (
        _log_items(deps, ledger_store)
        + _attention_items(deps, project_id, ledger_store)
        + _publish_items(deps, project_id)
        + _run_state_items(ledger_store)
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
            cid = deps.store.stage_confirmation(
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
                try:
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
            bindings = bindings_provider()
            if asyncio.iscoroutine(bindings):
                bindings = await bindings
            for binding in bindings or []:
                channel_id = _get(binding, "channel_id")
                project_id = _get(binding, "project_id")
                if not channel_id or not project_id:
                    continue
                poll_once(channel_id, project_id, deps=deps, poster=poster)

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
    "attention_decision_action",
    "poll_once",
    "current_marker_cursor",
    "sweep_timeouts",
    "run_loop",
]
