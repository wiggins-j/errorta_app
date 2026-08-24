from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from errorta_slack import connection, outbound, render, store, tools

# Not a module-level `pytestmark = pytest.mark.asyncio` (unlike test_connection.py)
# because this file also has one deliberately-sync test
# (test_outbound_module_does_not_import_slack_sdk) -- a module-wide mark would
# make pytest-asyncio warn about it. Each async test is marked individually
# instead.


@pytest.fixture(autouse=True)
def _isolated_errorta_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


# --- Fakes -------------------------------------------------------------
#
# outbound.poll_once / sweep_timeouts / run_loop are all deliberately
# testable with a SYNC poster (unlike connection.SlackBridge's poster,
# which is async) -- see outbound.py's module docstring. Keep the two
# distinct so a test never accidentally mixes them up.


class SyncFakePoster:
    """A sync duck-typed poster stand-in for outbound.py -- records every
    post_message call, and can be told to raise on a specific 1-indexed
    call to simulate a mid-poll failure."""

    def __init__(self, *, fail_on_call: int | None = None) -> None:
        self.messages: list[dict[str, Any]] = []
        self.call_count = 0
        self._fail_on_call = fail_on_call

    def post_message(self, channel_id, thread_ts, text, blocks=None) -> dict[str, Any]:
        self.call_count += 1
        if self._fail_on_call is not None and self.call_count == self._fail_on_call:
            raise RuntimeError("simulated poster failure")
        self.messages.append(
            {"channel_id": channel_id, "thread_ts": thread_ts, "text": text, "blocks": blocks}
        )
        return {"ts": f"ts-{self.call_count}"}


class AsyncFakePoster:
    """Async duck-typed poster stand-in for connection.SlackBridge (mirrors
    test_connection.py's FakePoster) -- used only by the cross-module claim
    race test below."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def post_message(self, channel_id, thread_ts, text, blocks=None) -> dict[str, Any]:
        self.messages.append(
            {"channel_id": channel_id, "thread_ts": thread_ts, "text": text, "blocks": blocks}
        )
        return {"ts": "ts-1"}

    async def add_reaction(self, channel_id, ts, name) -> None:
        pass


def _log_entry(at: str, kind: str, message: str) -> dict[str, Any]:
    return {"at": at, "role": "pm", "member": "", "kind": kind, "message": message}


def _signal(
    sig_id: str, *, created_at: str, title: str, summary: str = "", blocking: bool = True,
    kind: str = "alert",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=sig_id, created_at=created_at, title=title, summary=summary,
        blocking=blocking, kind=kind,
    )


class AttentionResolveSpy:
    """A fake standing in for `attention.resolve` -- records every call
    (Critical 1/2 regression coverage: outbound.py and connection.py must
    write the Slack decision through to the REAL attention signal via this
    seam, not just announce it)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, project_id: str, signal_id: str, action: str, *,
        by: str = "user", store: Any = None, **kwargs: Any,
    ) -> Any:
        self.calls.append(
            {"project_id": project_id, "signal_id": signal_id, "action": action, "by": by}
        )
        return SimpleNamespace(state="dismissed"), None


def _publish_event(
    event_id: str, *, created_at: str, state: str = "pr_opened", pr_url: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(event_id=event_id, created_at=created_at, state=state, pr_url=pr_url)


def _deps(
    *,
    log_entries: list[dict[str, Any]] | None = None,
    signals: list[Any] | None = None,
    publish_events: list[Any] | None = None,
    attention_resolve_fn: Any = None,
) -> outbound.OutboundDeps:
    return outbound.OutboundDeps(
        store=store,
        ledger_factory=lambda project_id: object(),  # opaque -- the fakes below ignore it
        team_log_fn=lambda ledger_store: list(log_entries or []),
        attention_list_open=lambda project_id, store=None: list(signals or []),
        publish_events_fn=lambda project_id: list(publish_events or []),
        attention_resolve_fn=attention_resolve_fn or (lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("attention_resolve_fn must not be called by this test")
        )),
    )


# --- poll_once: first pass posts + advances cursor --------------------------


@pytest.mark.asyncio
async def test_poll_once_posts_new_items_and_advances_cursor() -> None:
    entries = [
        _log_entry("2026-01-01T00:00:00", "pr_merged", "merged the login fix"),
        _log_entry("2026-01-01T00:01:00", "tested_pass", "ran the tests: passed"),
    ]
    deps = _deps(log_entries=entries)
    poster = SyncFakePoster()

    markers = outbound.poll_once("C1", "proj-a", deps=deps, poster=poster)

    assert len(markers) == 2
    assert len(poster.messages) == 2
    assert [m["text"] for m in poster.messages] == [
        "merged the login fix", "ran the tests: passed",
    ]
    assert store.get_cursor("C1") is not None


# --- poll_once: idempotent on a second call ----------------------------


@pytest.mark.asyncio
async def test_poll_once_second_call_posts_nothing() -> None:
    entries = [_log_entry("2026-01-01T00:00:00", "pr_merged", "merged the login fix")]
    deps = _deps(log_entries=entries)
    poster = SyncFakePoster()

    first = outbound.poll_once("C1", "proj-a", deps=deps, poster=poster)
    second = outbound.poll_once("C1", "proj-a", deps=deps, poster=poster)

    assert len(first) == 1
    assert second == []
    assert len(poster.messages) == 1


# --- poll_once: exactly-once under a mid-poll poster failure ------------


@pytest.mark.asyncio
async def test_poll_once_poster_failure_leaves_remainder_for_rerun() -> None:
    entries = [
        _log_entry("2026-01-01T00:00:00", "pr_merged", "item-1"),
        _log_entry("2026-01-01T00:01:00", "tested_pass", "item-2"),
        _log_entry("2026-01-01T00:02:00", "pr_opened", "item-3"),
    ]
    deps = _deps(log_entries=entries)
    failing_poster = SyncFakePoster(fail_on_call=2)

    with pytest.raises(RuntimeError):
        outbound.poll_once("C1", "proj-a", deps=deps, poster=failing_poster)

    # item-1 got through before the raise on item-2; the cursor reflects it.
    assert [m["text"] for m in failing_poster.messages] == ["item-1"]

    # A re-run (with a healthy poster) posts only the un-posted remainder --
    # item-1 is never re-posted (no dupes).
    healthy_poster = SyncFakePoster()
    markers = outbound.poll_once("C1", "proj-a", deps=deps, poster=healthy_poster)

    assert [m["text"] for m in healthy_poster.messages] == ["item-2", "item-3"]
    assert len(markers) == 2


# --- poll_once: blocking attention signal -> decision message ----------


@pytest.mark.asyncio
async def test_poll_once_blocking_attention_signal_becomes_decision_message() -> None:
    signals = [
        _signal("sig-1", created_at="2026-01-01T00:00:00", title="Tests are failing",
                summary="the CI suite has been red for 3 runs", blocking=True),
    ]
    deps = _deps(signals=signals)
    poster = SyncFakePoster()

    markers = outbound.poll_once("C1", "proj-a", deps=deps, poster=poster)

    assert markers == ["attn:sig-1"]
    assert len(poster.messages) == 1
    blocks = poster.messages[0]["blocks"]
    assert blocks == render.decision_message(
        "Tests are failing", "the CI suite has been red for 3 runs",
        _confirmation_id_from_blocks(blocks),
    )

    cid = _confirmation_id_from_blocks(blocks)
    record = store.get_confirmation(cid)
    assert record is not None
    assert record["state"] == "pending"
    assert record["verb"] == "attention_signal"
    assert record["args"]["signal_id"] == "sig-1"
    assert record["channel_id"] == "C1"


def _confirmation_id_from_blocks(blocks: list[dict[str, Any]]) -> str:
    for block in blocks:
        if block.get("type") != "actions":
            continue
        for element in block.get("elements", []):
            if element.get("action_id") == "slack_approve":
                return str(element["value"])
    raise AssertionError("no Approve button found in blocks")


# --- poll_once: a decision message that fails to post leaves no orphan -----


@pytest.mark.asyncio
async def test_poll_once_decision_post_failure_rolls_back_the_staged_confirmation() -> None:
    """Task 9 review Important finding: staging happens before posting. If
    the post then fails, a naive implementation leaves the confirmation
    "pending" forever with no human ever having seen it -- and the timeout
    sweep later "decides" it into a confusing phantom message. The staged
    confirmation must be rolled back to a terminal, non-"pending" state (so
    the sweep ignores it), and the cursor must NOT advance (so a re-run
    re-stages and re-tries from scratch, with a fresh confirmation id)."""
    signals = [
        _signal("sig-orphan", created_at="2026-01-01T00:00:00", title="Blocked",
                summary="needs a decision", blocking=True, kind="alert"),
    ]
    deps = _deps(signals=signals)
    failing_poster = SyncFakePoster(fail_on_call=1)

    with pytest.raises(RuntimeError):
        outbound.poll_once("C1", "proj-a", deps=deps, poster=failing_poster)

    # Exactly one confirmation was staged (for sig-orphan); it must now be in
    # a terminal, non-"pending" state -- never left dangling as "pending".
    confirmations = store._load_confirmations()  # type: ignore[attr-defined]
    assert len(confirmations) == 1
    (orphan,) = confirmations.values()
    assert orphan["args"]["signal_id"] == "sig-orphan"
    assert orphan["state"] != "pending"

    # The sweep must never later "decide" it -- pop_pending_older_than only
    # claims "pending" records.
    sweep_poster = SyncFakePoster()
    handled = outbound.sweep_timeouts(
        deps=outbound.OutboundDeps(store=store), poster=sweep_poster,
        timeout_minutes=0, now=orphan["created_at"] + 1,
    )
    assert handled == []
    assert sweep_poster.messages == []

    # The cursor did not advance -- a re-run re-stages a FRESH confirmation
    # and tries posting again (rather than treating sig-orphan as "already
    # handled" and silently dropping it forever).
    assert store.get_cursor("C1") is None
    healthy_poster = SyncFakePoster()
    markers = outbound.poll_once("C1", "proj-a", deps=deps, poster=healthy_poster)
    assert markers == ["attn:sig-orphan"]
    assert len(healthy_poster.messages) == 1

    confirmations_after = store._load_confirmations()  # type: ignore[attr-defined]
    pending = [r for r in confirmations_after.values() if r["state"] == "pending"]
    assert len(pending) == 1
    assert pending[0]["args"]["signal_id"] == "sig-orphan"
    assert pending[0]["id"] != orphan["id"]  # a fresh confirmation, not the orphan reused


# --- poll_once: non-blocking attention signal -> plain FYI, no staging -----


@pytest.mark.asyncio
async def test_poll_once_non_blocking_attention_signal_is_not_posted() -> None:
    """Slice 6 changed this contract deliberately.

    A non-blocking signal used to post as a buttonless FYI. One live run raised
    28 of them against a single spec, in a channel designed to carry 6-12
    milestone messages, and team_log already reports "reviewed an artifact
    (N finding(s))" for the same review round.

    The original assertion -- that such a signal never grows a button -- still
    holds, and now holds more strongly: it produces no message at all.
    """
    signals = [
        _signal("sig-2", created_at="2026-01-01T00:00:00", title="Reviewer note",
                summary="a save button has no autosave guidance", blocking=False),
    ]
    deps = _deps(signals=signals)
    poster = SyncFakePoster()

    markers = outbound.poll_once("C1", "proj-a", deps=deps, poster=poster)

    assert markers == []
    assert poster.messages == []


# --- poll_once: PR-ready publish event -> FYI ---------------------------


@pytest.mark.asyncio
async def test_poll_once_pr_opened_publish_event_is_fyi() -> None:
    events = [
        _publish_event("ev-1", created_at="2026-01-01T00:00:00",
                        state="pr_opened", pr_url="https://example.invalid/pr/1"),
    ]
    deps = _deps(publish_events=events)
    poster = SyncFakePoster()

    markers = outbound.poll_once("C1", "proj-a", deps=deps, poster=poster)

    assert markers == ["pub:ev-1"]
    assert "https://example.invalid/pr/1" in poster.messages[0]["text"]


@pytest.mark.asyncio
async def test_poll_once_ignores_publish_events_not_pr_opened() -> None:
    events = [_publish_event("ev-2", created_at="2026-01-01T00:00:00", state="pushed")]
    deps = _deps(publish_events=events)
    poster = SyncFakePoster()

    markers = outbound.poll_once("C1", "proj-a", deps=deps, poster=poster)

    assert markers == []
    assert poster.messages == []


# --- poll_once: cursor tracks membership, not position ------------------


@pytest.mark.asyncio
async def test_poll_once_new_items_from_multiple_sources_all_post_once() -> None:
    entries = [_log_entry("2026-01-01T00:00:00", "pr_merged", "log item")]
    signals = [_signal("sig-3", created_at="2026-01-01T00:01:00", title="Blocker",
                        summary="something's stuck", blocking=True)]
    events = [_publish_event("ev-3", created_at="2026-01-01T00:02:00",
                              state="pr_opened", pr_url="https://example.invalid/pr/3")]
    deps = _deps(log_entries=entries, signals=signals, publish_events=events)
    poster = SyncFakePoster()

    markers = outbound.poll_once("C1", "proj-a", deps=deps, poster=poster)

    assert set(markers) == {"log:2026-01-01T00:00:00:pr_merged", "attn:sig-3", "pub:ev-3"}
    assert len(poster.messages) == 3

    # Re-run: nothing new.
    assert outbound.poll_once("C1", "proj-a", deps=deps, poster=poster) == []


# --- sweep_timeouts: conservative default for irreversible verbs -----------


@pytest.mark.asyncio
async def test_sweep_timeouts_declines_spend_cloud_by_conservative_default() -> None:
    cid = store.stage_confirmation(
        "spend_cloud", {"amount": 5, "reason": "extra pass"}, "thread-1",
        channel_id="C1", now=1000.0,
    )
    deps = outbound.OutboundDeps(store=store)
    poster = SyncFakePoster()

    handled = outbound.sweep_timeouts(
        deps=deps, poster=poster, timeout_minutes=30, now=1000.0 + 31 * 60,
    )

    assert handled == [cid]
    assert store.get_confirmation(cid)["state"] == "timed_out"
    assert len(poster.messages) == 1
    assert poster.messages[0]["channel_id"] == "C1"
    assert poster.messages[0]["thread_ts"] == "thread-1"
    text = poster.messages[0]["text"].lower()
    assert "declined" in text
    assert "spend_cloud" in text


@pytest.mark.asyncio
async def test_sweep_timeouts_declines_publish_pr_by_conservative_default() -> None:
    cid = store.stage_confirmation(
        "publish_pr", {"title": "x"}, "thread-2", channel_id="C1", now=1000.0,
    )
    deps = outbound.OutboundDeps(store=store)
    poster = SyncFakePoster()

    outbound.sweep_timeouts(deps=deps, poster=poster, timeout_minutes=30, now=1000.0 + 31 * 60)

    assert store.get_confirmation(cid)["state"] == "timed_out"
    assert "declined" in poster.messages[0]["text"].lower()


# --- sweep_timeouts: other classes use their declared on_timeout -----------


@pytest.mark.asyncio
async def test_sweep_timeouts_applies_declared_on_timeout_approved() -> None:
    store.stage_confirmation(
        "attention_signal",
        {"signal_id": "sig-1", "project_id": "proj-a", "signal_kind": "alert",
         "on_timeout": "approved"},
        "thread-3", channel_id="C1", now=1000.0,
    )
    spy = AttentionResolveSpy()
    deps = outbound.OutboundDeps(store=store, attention_resolve_fn=spy)
    poster = SyncFakePoster()

    outbound.sweep_timeouts(deps=deps, poster=poster, timeout_minutes=30, now=1000.0 + 31 * 60)

    assert "approved" in poster.messages[0]["text"].lower()
    assert spy.calls == [
        {"project_id": "proj-a", "signal_id": "sig-1", "action": "accept", "by": "slack-timeout"}
    ]


@pytest.mark.asyncio
async def test_sweep_timeouts_defaults_to_decline_with_no_declared_on_timeout() -> None:
    store.stage_confirmation(
        "attention_signal",
        {"signal_id": "sig-1", "project_id": "proj-a", "signal_kind": "alert"},
        "thread-4", channel_id="C1", now=1000.0,
    )
    spy = AttentionResolveSpy()
    deps = outbound.OutboundDeps(store=store, attention_resolve_fn=spy)
    poster = SyncFakePoster()

    outbound.sweep_timeouts(deps=deps, poster=poster, timeout_minutes=30, now=1000.0 + 31 * 60)

    assert "declined" in poster.messages[0]["text"].lower()
    assert spy.calls == [
        {"project_id": "proj-a", "signal_id": "sig-1", "action": "dismiss", "by": "slack-timeout"}
    ]


# --- Critical 1 (Task 9 review): the timeout sweep must WRITE THROUGH the ---
# --- decision to the real attention signal, not just announce it -----------


@pytest.mark.asyncio
async def test_sweep_timeouts_resolves_attention_signal_with_right_ids_and_action() -> None:
    """The core Critical 1 regression: sweep_timeouts must call the injected
    attention_resolve_fn with the STAGED signal_id/project_id and the
    correctly-mapped action -- not merely post a decision message that
    leaves the real governance signal open (which would defeat spec §5.9's
    whole purpose: the pipeline stays stuck)."""
    cid = store.stage_confirmation(
        outbound.ATTENTION_VERB,
        {"signal_id": "sig-critical-1", "project_id": "proj-z", "signal_kind": "alert",
         "on_timeout": "declined"},
        "thread-crit1", channel_id="C1", now=1000.0,
    )
    spy = AttentionResolveSpy()
    deps = outbound.OutboundDeps(store=store, attention_resolve_fn=spy)
    poster = SyncFakePoster()

    handled = outbound.sweep_timeouts(
        deps=deps, poster=poster, timeout_minutes=30, now=1000.0 + 31 * 60,
    )

    assert handled == [cid]
    assert len(spy.calls) == 1
    assert spy.calls[0]["project_id"] == "proj-z"
    assert spy.calls[0]["signal_id"] == "sig-critical-1"
    assert spy.calls[0]["action"] == "dismiss"
    assert spy.calls[0]["by"] == "slack-timeout"


@pytest.mark.asyncio
async def test_sweep_timeouts_problem_kind_has_no_safe_action_and_skips_resolve() -> None:
    """A "problem"-kind signal's only VALID_ACTIONS are accept/correct -- no
    safe "clear it" action exists. The sweep must NOT invent one (esp. must
    not auto-accept, which can spawn a task -- the opposite of the
    conservative default a timeout is supposed to mean): it skips calling
    attention_resolve_fn and says so in the posted message, leaving the
    signal open for a human."""
    store.stage_confirmation(
        outbound.ATTENTION_VERB,
        {"signal_id": "sig-problem-1", "project_id": "proj-a", "signal_kind": "problem"},
        "thread-crit1b", channel_id="C1", now=1000.0,
    )
    spy = AttentionResolveSpy()
    deps = outbound.OutboundDeps(store=store, attention_resolve_fn=spy)
    poster = SyncFakePoster()

    handled = outbound.sweep_timeouts(
        deps=deps, poster=poster, timeout_minutes=30, now=1000.0 + 31 * 60,
    )

    assert handled  # still claimed (state advances to timed_out) even though unresolved
    assert spy.calls == []  # never called with an invented, unsafe action
    assert "no automatic action was safe" in poster.messages[0]["text"].lower()


@pytest.mark.asyncio
async def test_sweep_timeouts_ignores_confirmations_still_within_window() -> None:
    store.stage_confirmation(
        "spend_cloud", {"amount": 5}, "thread-5", channel_id="C1", now=1000.0,
    )
    deps = outbound.OutboundDeps(store=store)
    poster = SyncFakePoster()

    handled = outbound.sweep_timeouts(
        deps=deps, poster=poster, timeout_minutes=30, now=1000.0 + 5 * 60,
    )

    assert handled == []
    assert poster.messages == []


# --- Carried requirement: claim semantics close the sweep/button race ------


@pytest.mark.asyncio
async def test_timeout_sweep_wins_race_then_button_click_is_a_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the background sweep claims an expired confirmation first, a
    button click that arrives afterward for the SAME confirmation must not
    dispatch the effect a second time."""
    cid = store.stage_confirmation(
        "spend_cloud", {"amount": 5}, "click-thread-1", channel_id="C1", now=1000.0,
    )

    dispatch_calls: list[str] = []
    monkeypatch.setattr(
        tools, "dispatch",
        lambda verb, args, **k: dispatch_calls.append(verb),
    )

    out_deps = outbound.OutboundDeps(store=store)
    sweep_poster = SyncFakePoster()
    handled = outbound.sweep_timeouts(
        deps=out_deps, poster=sweep_poster, timeout_minutes=30, now=1000.0 + 31 * 60,
    )
    assert handled == [cid]
    assert dispatch_calls == []  # the sweep itself never dispatches

    tool_deps = tools.ToolDeps(store=store)
    async_poster = AsyncFakePoster()
    bridge = connection.SlackBridge(
        None, async_poster, tool_deps, lambda member, prompt: "{}",
        config={"allowed_team_ids": ["T1"], "allowed_user_ids": ["U1"]},
    )
    payload = {
        "type": "block_actions", "team": {"id": "T1"}, "user": {"id": "U1"},
        "channel": {"id": "C1"}, "message": {"ts": "click-thread-1"},
        "actions": [{"action_id": "slack_approve", "value": cid}],
    }
    await bridge.handle_interaction(payload)

    # The sweep already claimed it -- the late click must lose the race and
    # fire the effect zero additional times.
    assert dispatch_calls == []
    assert store.get_confirmation(cid)["state"] == "timed_out"
    assert async_poster.messages == []  # no outcome posted for a losing claim


@pytest.mark.asyncio
async def test_button_click_wins_race_then_timeout_sweep_is_a_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a human's Approve click resolves an confirmation first, the
    background timeout sweep must not also claim (and re-decide) it, even
    if it later runs past that confirmation's timeout window."""
    cid = store.stage_confirmation(
        "spend_cloud", {"amount": 5}, "click-thread-2", channel_id="C1", now=1000.0,
    )

    dispatch_calls: list[str] = []
    monkeypatch.setattr(
        tools, "dispatch",
        lambda verb, args, **k: dispatch_calls.append(verb),
    )

    tool_deps = tools.ToolDeps(store=store)
    async_poster = AsyncFakePoster()
    bridge = connection.SlackBridge(
        None, async_poster, tool_deps, lambda member, prompt: "{}",
        config={"allowed_team_ids": ["T1"], "allowed_user_ids": ["U1"]},
    )
    payload = {
        "type": "block_actions", "team": {"id": "T1"}, "user": {"id": "U1"},
        "channel": {"id": "C1"}, "message": {"ts": "click-thread-2"},
        "actions": [{"action_id": "slack_approve", "value": cid}],
    }
    await bridge.handle_interaction(payload)

    assert dispatch_calls == ["spend_cloud"]
    assert store.get_confirmation(cid)["state"] == "approved"

    out_deps = outbound.OutboundDeps(store=store)
    sweep_poster = SyncFakePoster()
    handled = outbound.sweep_timeouts(
        deps=out_deps, poster=sweep_poster, timeout_minutes=30, now=1000.0 + 31 * 60,
    )

    # pop_pending_older_than only claims records still "pending" -- this one
    # is "approved", so the sweep must not touch it (and must not post a
    # second, contradictory "outcome" for it).
    assert handled == []
    assert sweep_poster.messages == []
    assert store.get_confirmation(cid)["state"] == "approved"
    assert dispatch_calls == ["spend_cloud"]  # still exactly one real dispatch


# --- Critical 2 (Task 9 review): approving/declining an attention_signal ---
# --- decision must resolve the real signal, not crash on tools.dispatch ----


@pytest.mark.asyncio
async def test_handle_interaction_approve_attention_signal_resolves_via_injected_fn() -> None:
    """attention_signal is not a tools.TOOL_CATALOG verb -- Approving one
    must route through attention_resolve_fn (not tools.dispatch, and never
    with confirmed_via="block_actions"), and must not raise."""
    cid = store.stage_confirmation(
        outbound.ATTENTION_VERB,
        {"signal_id": "sig-crit2-a", "project_id": "proj-a", "signal_kind": "alert"},
        "thread-att-1", channel_id="C1",
    )
    spy = AttentionResolveSpy()
    tool_deps = tools.ToolDeps(store=store, attention_resolve_fn=spy)
    async_poster = AsyncFakePoster()
    bridge = connection.SlackBridge(
        None, async_poster, tool_deps, lambda member, prompt: "{}",
        config={"allowed_team_ids": ["T1"], "allowed_user_ids": ["U1"]},
    )
    payload = {
        "type": "block_actions", "team": {"id": "T1"}, "user": {"id": "U1"},
        "channel": {"id": "C1"}, "message": {"ts": "thread-att-1"},
        "actions": [{"action_id": "slack_approve", "value": cid}],
    }

    await bridge.handle_interaction(payload)  # must not raise

    assert spy.calls == [
        {"project_id": "proj-a", "signal_id": "sig-crit2-a", "action": "accept", "by": "slack"}
    ]
    assert store.get_confirmation(cid)["state"] == "approved"
    assert len(async_poster.messages) == 1
    assert "executed" in async_poster.messages[0]["text"].lower()


@pytest.mark.asyncio
async def test_handle_interaction_decline_attention_signal_resolves_with_dismiss() -> None:
    """Unlike a real tool verb (where Decline is a pure no-op), declining an
    attention_signal decision must still write a real resolution through --
    a "no" answer needs to clear the block too, just with the safe/clearing
    action instead of "accept"."""
    cid = store.stage_confirmation(
        outbound.ATTENTION_VERB,
        {"signal_id": "sig-crit2-b", "project_id": "proj-a", "signal_kind": "alert"},
        "thread-att-2", channel_id="C1",
    )
    spy = AttentionResolveSpy()
    tool_deps = tools.ToolDeps(store=store, attention_resolve_fn=spy)
    async_poster = AsyncFakePoster()
    bridge = connection.SlackBridge(
        None, async_poster, tool_deps, lambda member, prompt: "{}",
        config={"allowed_team_ids": ["T1"], "allowed_user_ids": ["U1"]},
    )
    payload = {
        "type": "block_actions", "team": {"id": "T1"}, "user": {"id": "U1"},
        "channel": {"id": "C1"}, "message": {"ts": "thread-att-2"},
        "actions": [{"action_id": "slack_decline", "value": cid}],
    }

    await bridge.handle_interaction(payload)

    assert spy.calls == [
        {"project_id": "proj-a", "signal_id": "sig-crit2-b", "action": "dismiss", "by": "slack"}
    ]
    assert store.get_confirmation(cid)["state"] == "declined"


@pytest.mark.asyncio
async def test_handle_interaction_attention_signal_never_uses_block_actions_dispatch() -> None:
    """The `confirmed_via="block_actions"` marker is reserved for real tool
    verbs (tools.dispatch's injection guard) -- an attention_signal
    resolution must never pass through tools.dispatch at all, so that
    marker can never appear on it."""
    cid = store.stage_confirmation(
        outbound.ATTENTION_VERB,
        {"signal_id": "sig-crit2-c", "project_id": "proj-a", "signal_kind": "alert"},
        "thread-att-3", channel_id="C1",
    )
    dispatch_calls: list[Any] = []

    def _dispatch_spy(*args: Any, **kwargs: Any) -> Any:
        dispatch_calls.append((args, kwargs))
        raise AssertionError("tools.dispatch must never be called for attention_signal")

    tool_deps = tools.ToolDeps(store=store, attention_resolve_fn=AttentionResolveSpy())
    async_poster = AsyncFakePoster()
    bridge = connection.SlackBridge(
        None, async_poster, tool_deps, lambda member, prompt: "{}",
        config={"allowed_team_ids": ["T1"], "allowed_user_ids": ["U1"]},
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tools, "dispatch", _dispatch_spy)
        payload = {
            "type": "block_actions", "team": {"id": "T1"}, "user": {"id": "U1"},
            "channel": {"id": "C1"}, "message": {"ts": "thread-att-3"},
            "actions": [{"action_id": "slack_approve", "value": cid}],
        }
        await bridge.handle_interaction(payload)

    assert dispatch_calls == []


@pytest.mark.asyncio
async def test_handle_interaction_unknown_verb_posts_clean_error_not_crash() -> None:
    """Critical 2 regression: a confirmation staged with a verb that is
    neither outbound.ATTENTION_VERB nor a real tools.TOOL_CATALOG entry (a
    corrupted/forged record, or a future bug) must never crash the
    callback -- tools.dispatch's ToolError is caught, logged, and a clean
    error is posted instead."""
    cid = store.stage_confirmation("not_a_real_verb", {}, "thread-bad-1", channel_id="C1")
    tool_deps = tools.ToolDeps(store=store)
    async_poster = AsyncFakePoster()
    bridge = connection.SlackBridge(
        None, async_poster, tool_deps, lambda member, prompt: "{}",
        config={"allowed_team_ids": ["T1"], "allowed_user_ids": ["U1"]},
    )
    payload = {
        "type": "block_actions", "team": {"id": "T1"}, "user": {"id": "U1"},
        "channel": {"id": "C1"}, "message": {"ts": "thread-bad-1"},
        "actions": [{"action_id": "slack_approve", "value": cid}],
    }

    await bridge.handle_interaction(payload)  # must not raise

    # Claimed (state changed) even though the effect failed -- it cannot be
    # retried, so the clean error is the only signal the human gets.
    assert store.get_confirmation(cid)["state"] == "approved"
    assert len(async_poster.messages) == 1
    assert async_poster.messages[0]["text"] == connection._EFFECT_ERROR_TEXT


@pytest.mark.asyncio
async def test_handle_interaction_problem_kind_attention_signal_posts_clean_error() -> None:
    """Approving a "problem"-kind attention_signal (no safe action exists --
    see outbound.attention_decision_action) must not crash either."""
    cid = store.stage_confirmation(
        outbound.ATTENTION_VERB,
        {"signal_id": "sig-crit2-d", "project_id": "proj-a", "signal_kind": "problem"},
        "thread-att-4", channel_id="C1",
    )
    spy = AttentionResolveSpy()
    tool_deps = tools.ToolDeps(store=store, attention_resolve_fn=spy)
    async_poster = AsyncFakePoster()
    bridge = connection.SlackBridge(
        None, async_poster, tool_deps, lambda member, prompt: "{}",
        config={"allowed_team_ids": ["T1"], "allowed_user_ids": ["U1"]},
    )
    payload = {
        "type": "block_actions", "team": {"id": "T1"}, "user": {"id": "U1"},
        "channel": {"id": "C1"}, "message": {"ts": "thread-att-4"},
        "actions": [{"action_id": "slack_decline", "value": cid}],
    }

    await bridge.handle_interaction(payload)  # must not raise

    assert spy.calls == []
    assert store.get_confirmation(cid)["state"] == "declined"
    assert async_poster.messages[0]["text"] == connection._EFFECT_ERROR_TEXT


# --- run_loop: ticks on interval, drains bindings + runs the sweep ---------


@pytest.mark.asyncio
async def test_run_loop_polls_each_binding_and_stops_on_stop_event() -> None:
    entries = [_log_entry("2026-01-01T00:00:00", "pr_merged", "hello")]
    deps = _deps(log_entries=entries)
    poster = SyncFakePoster()
    stop_event = asyncio.Event()

    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        stop_event.set()  # stop after exactly one tick

    def bindings_provider() -> list[dict[str, Any]]:
        return [{"channel_id": "C1", "project_id": "proj-a"}]

    await outbound.run_loop(
        bindings_provider=bindings_provider, deps=deps, poster=poster,
        interval_s=15, sleep_fn=fake_sleep, now_fn=lambda: 1000.0,
        stop_event=stop_event,
    )

    assert sleeps == [15]
    assert len(poster.messages) == 1
    assert poster.messages[0]["text"] == "hello"


@pytest.mark.asyncio
async def test_run_loop_runs_timeout_sweep_each_tick() -> None:
    cid = store.stage_confirmation(
        "spend_cloud", {"amount": 5}, "loop-thread-1", channel_id="C1", now=1000.0,
    )
    deps = outbound.OutboundDeps(store=store)
    poster = SyncFakePoster()
    stop_event = asyncio.Event()

    async def fake_sleep(delay: float) -> None:
        stop_event.set()

    await outbound.run_loop(
        bindings_provider=lambda: [],
        deps=deps, poster=poster, interval_s=1, timeout_minutes=30,
        sleep_fn=fake_sleep, now_fn=lambda: 1000.0 + 31 * 60,
        stop_event=stop_event,
    )

    assert store.get_confirmation(cid)["state"] == "timed_out"
    assert len(poster.messages) == 1


@pytest.mark.asyncio
async def test_run_loop_survives_a_bad_tick_and_keeps_going() -> None:
    """A single failing tick (e.g. a transient poster error) must not kill
    the whole loop -- the next tick should still run."""
    call_count = {"n": 0}

    def flaky_bindings_provider() -> list[dict[str, Any]]:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("transient failure")
        return []

    deps = outbound.OutboundDeps(store=store)
    poster = SyncFakePoster()
    stop_event = asyncio.Event()
    tick_count = {"n": 0}

    async def fake_sleep(delay: float) -> None:
        tick_count["n"] += 1
        if tick_count["n"] >= 2:
            stop_event.set()

    await outbound.run_loop(
        bindings_provider=flaky_bindings_provider, deps=deps, poster=poster,
        interval_s=1, sleep_fn=fake_sleep, now_fn=lambda: 1000.0, stop_event=stop_event,
    )

    assert call_count["n"] == 2  # the loop kept going after the first tick's failure


# --- Lazy slack_sdk import guard --------------------------------------------


def test_outbound_module_does_not_import_slack_sdk() -> None:
    """outbound.py must not pull in slack_sdk at module load.

    Order-independent by construction (matches store.py's hardened test):
    checks outbound.py's OWN bound names and source text, not process-global
    ``sys.modules`` -- a sibling test module importing slack_sdk earlier in
    the same pytest session must not make this assertion false-fail.

    """
    import inspect

    assert "slack_sdk" not in vars(outbound)
    source = inspect.getsource(outbound)
    assert "import slack_sdk" not in source


def test_outbound_module_does_not_import_the_live_run_supervisor() -> None:
    """Same rule, second package: `errorta_liverun` owns launch/subprocess
    machinery, and `outbound` must stay importable with only the bridge
    installed. Order-independent -- reads this module's own bound names and
    source, never process-global ``sys.modules``."""
    import inspect

    assert "errorta_liverun" not in vars(outbound)
    for line in inspect.getsource(outbound).splitlines():
        if "errorta_liverun" in line and "import " in line:
            assert line.startswith(" "), f"module-level supervisor import: {line!r}"


# --------------------------------------------------------------------------
# Slice 5b Task 4 — run state as a fourth content source.
#
# The operator requires three events to ALWAYS arrive: the team stops, hits a
# roadblock, or finishes. Roadblock was already covered (a blocking attention
# signal becomes a buttoned decision), but run termination reached no source at
# all: _current_items read the team log, attention and the publish ledger, and
# none of them carries run state. "The team finished" could not fire.
# --------------------------------------------------------------------------


def _run_state_deps(run_state: dict[str, Any], **kw: Any) -> outbound.OutboundDeps:
    deps = _deps(**kw)
    deps.ledger_factory = lambda project_id: SimpleNamespace(
        get_run_state=lambda: dict(run_state))
    return deps


@pytest.mark.asyncio
async def test_run_state_item_emitted_on_stop() -> None:
    store.advance_cursor("C-run", "")
    deps = _run_state_deps(
        {"status": "stopped", "ended_at": "2026-01-02T00:00:00",
         "stop_reason": "north star met"})
    poster = SyncFakePoster()

    posted = outbound.poll_once("C-run", "p1", deps=deps, poster=poster)

    assert posted == ["run:stopped:2026-01-02T00:00:00"]
    assert "stopped" in poster.messages[0]["text"].lower()


@pytest.mark.asyncio
async def test_run_state_item_not_repeated_on_a_second_poll() -> None:
    store.advance_cursor("C-run2", "")
    deps = _run_state_deps(
        {"status": "stopped", "ended_at": "2026-01-02T00:00:00"})
    poster = SyncFakePoster()

    outbound.poll_once("C-run2", "p1", deps=deps, poster=poster)
    again = outbound.poll_once("C-run2", "p1", deps=deps, poster=poster)

    assert again == []
    assert len(poster.messages) == 1


@pytest.mark.asyncio
async def test_run_failed_emits_an_item() -> None:
    store.advance_cursor("C-run3", "")
    deps = _run_state_deps(
        {"status": "failed", "ended_at": "2026-01-03T00:00:00",
         "last_error": "provider timeout"})
    poster = SyncFakePoster()

    posted = outbound.poll_once("C-run3", "p1", deps=deps, poster=poster)

    assert posted == ["run:failed:2026-01-03T00:00:00"]


@pytest.mark.asyncio
async def test_idle_and_running_states_emit_nothing() -> None:
    """Only terminal transitions are events. A run that is merely in progress
    would otherwise post an item on every single 15s tick."""
    for status in ("idle", "running"):
        channel = f"C-run-{status}"
        store.advance_cursor(channel, "")
        deps = _run_state_deps({"status": status, "ended_at": None})
        poster = SyncFakePoster()

        assert outbound.poll_once(channel, "p1", deps=deps, poster=poster) == []


# --------------------------------------------------------------------------
# Slice 5b Task 5 — mute quiets routine progress, never the mandatory three.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mute_suppresses_ordinary_milestones() -> None:
    store.advance_cursor("C-mute", "")
    store.set_updates("C-mute", enabled=False)
    deps = _deps(log_entries=[
        _log_entry("2026-01-01T00:00:00", "pr_merged", "merged the login fix")])
    poster = SyncFakePoster()

    assert outbound.poll_once("C-mute", "p1", deps=deps, poster=poster) == []
    assert poster.messages == []


@pytest.mark.asyncio
async def test_mute_does_not_suppress_run_termination() -> None:
    store.advance_cursor("C-mute2", "")
    store.set_updates("C-mute2", enabled=False)
    deps = _run_state_deps({"status": "stopped", "ended_at": "2026-01-02T00:00:00"})
    poster = SyncFakePoster()

    posted = outbound.poll_once("C-mute2", "p1", deps=deps, poster=poster)

    assert posted == ["run:stopped:2026-01-02T00:00:00"]
    assert len(poster.messages) == 1


@pytest.mark.asyncio
async def test_mute_does_not_suppress_a_blocking_decision() -> None:
    """A roadblock is one of the three that always arrive -- and the run is
    literally waiting on the button, so hiding it would deadlock the team."""
    store.advance_cursor("C-mute3", "")
    store.set_updates("C-mute3", enabled=False)
    deps = _deps(signals=[
        _signal("s1", created_at="2026-01-01T00:00:00", title="Need a decision",
                blocking=True)])
    poster = SyncFakePoster()

    posted = outbound.poll_once("C-mute3", "p1", deps=deps, poster=poster)

    assert posted == ["attn:s1"]
    assert len(poster.messages) == 1


@pytest.mark.asyncio
async def test_muted_ordinary_item_is_not_marked_posted() -> None:
    """Suppression must not burn the marker: unmuting has to deliver the
    backlog, not skip it forever."""
    store.advance_cursor("C-mute4", "")
    store.set_updates("C-mute4", enabled=False)
    deps = _deps(log_entries=[
        _log_entry("2026-01-01T00:00:00", "pr_merged", "merged the login fix")])
    poster = SyncFakePoster()

    outbound.poll_once("C-mute4", "p1", deps=deps, poster=poster)
    store.set_updates("C-mute4", enabled=True)
    posted = outbound.poll_once("C-mute4", "p1", deps=deps, poster=poster)

    assert posted == ["log:2026-01-01T00:00:00:pr_merged"]


@pytest.mark.asyncio
async def test_run_failure_reason_is_escaped() -> None:
    """`last_error` is a raw str(exc) and fyi_message does not escape. An
    exception carrying "<!channel>" must not ping the whole workspace."""
    store.advance_cursor("C-esc", "")
    deps = _run_state_deps({
        "status": "failed", "ended_at": "2026-01-04T00:00:00",
        "last_error": "boom <!channel> & <https://evil|click>",
    })
    poster = SyncFakePoster()

    outbound.poll_once("C-esc", "p1", deps=deps, poster=poster)

    text = poster.messages[0]["text"]
    assert "<!channel>" not in text
    assert "&lt;!channel&gt;" in text


# --------------------------------------------------------------------------
# Slice 6 F2 — reviewer nitpicks are not milestones.
#
# The live run produced 28 non-blocking `alert` signals on one spec, each
# queued as its own Slack message, against an approved design of 6-12 milestone
# messages. team_log already posts "reviewed an artifact (N finding(s))" for the
# same review round, so the channel loses nothing it did not already have.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_blocking_alert_produces_no_message() -> None:
    store.advance_cursor("C-nb", "")
    deps = _deps(signals=[
        _signal("s1", created_at="2026-01-01T00:00:00",
                title="Rounding method not specified", blocking=False)])
    poster = SyncFakePoster()

    assert outbound.poll_once("C-nb", "p1", deps=deps, poster=poster) == []
    assert poster.messages == []


@pytest.mark.asyncio
async def test_blocking_signal_still_posts_as_a_decision() -> None:
    store.advance_cursor("C-b", "")
    deps = _deps(signals=[
        _signal("s2", created_at="2026-01-01T00:00:00",
                title="Run can't complete", blocking=True)])
    poster = SyncFakePoster()

    assert outbound.poll_once("C-b", "p1", deps=deps, poster=poster) == ["attn:s2"]
    assert poster.messages[0]["blocks"]


@pytest.mark.asyncio
async def test_blocking_signal_still_ignores_the_mute() -> None:
    store.advance_cursor("C-bm", "")
    store.set_updates("C-bm", enabled=False)
    deps = _deps(signals=[
        _signal("s3", created_at="2026-01-01T00:00:00",
                title="Run can't complete", blocking=True)])
    poster = SyncFakePoster()

    assert outbound.poll_once("C-bm", "p1", deps=deps, poster=poster) == ["attn:s3"]


# --------------------------------------------------------------------------
# Slice: the live-run supervisor as a fifth content source (spec §3.7).
#
# The supervisor writes its whole narrative to `runs/<id>/events.jsonl` and
# nothing else reads it, so without this source a live run launches, stalls,
# gets torn down and pauses awaiting a human entirely off-channel.
# --------------------------------------------------------------------------


def _liverun_fixture(project_id: str = "proj-lr"):
    from errorta_liverun.state import RunState, RunStore

    rs = RunStore()
    rid = rs.new_run_id()
    state = RunState(
        run_id=rid, profile_name="osrs", project_id=project_id, phase="watching",
        reason=None, session_id="s", step_index=2,
        started_at="2026-08-21T00:00:00Z", launched_at="t", ended_at=None,
        evidence_dir=str(rs.evidence_dir(rid)),
    )
    rs.save(state)
    rs.append_event(rid, "phase", {"to": "launching", "reason": None})
    rs.append_event(rid, "launch_step",
                    {"name": "rebuild-jar", "ok": True, "exit_code": 0,
                     "stdout": "", "stderr": ""})
    rs.append_event(rid, "probe_warn", {"id": "xp", "stalled_s": 900})
    return rs, state


def test_liverun_items_have_stable_markers_and_mandatory_flags() -> None:
    rs, state = _liverun_fixture()
    rs.append_event(state.run_id, "stall", {"id": "journal-seq", "stalled_s": 181})
    rs.append_event(state.run_id, "literals", {"logoff_verified": "ABSENT"})
    rs.append_event(state.run_id, "phase", {"to": "stopped", "reason": "stall:journal-seq"})

    items = outbound._liverun_items(outbound.OutboundDeps(), "proj-lr")

    assert [it.marker for it in items] == [
        f"liverun:{state.run_id}:{i}" for i in range(1, 7)]
    assert all(it.kind == "fyi" for it in items)
    # phase -> launching / a successful launch step / a probe WARNING are
    # routine progress: a muted channel is entitled to miss them.
    assert [it.mandatory for it in items[:3]] == [False, False, False]
    assert items[3].mandatory is True                              # stall
    assert items[4].mandatory is True and "ABSENT" in items[4].detail
    assert items[5].mandatory is True and "stopped" in items[5].detail
    assert "osrs" in items[5].detail
    # Another project's runs are not this channel's business.
    assert outbound._liverun_items(outbound.OutboundDeps(), "other") == []


def test_liverun_items_flow_through_poll_once_and_dedupe() -> None:
    rs, state = _liverun_fixture()
    store.bind_channel("C-lr", "proj-lr")
    poster = SyncFakePoster()

    first = outbound.poll_once("C-lr", "proj-lr", deps=outbound.OutboundDeps(), poster=poster)

    assert first == [f"liverun:{state.run_id}:{i}" for i in (1, 2, 3)]
    assert outbound.poll_once(
        "C-lr", "proj-lr", deps=outbound.OutboundDeps(), poster=poster) == []


def test_muted_channel_still_gets_the_stall() -> None:
    rs, state = _liverun_fixture()
    rs.append_event(state.run_id, "stall", {"id": "brain-alive", "stalled_s": 46})
    store.bind_channel("C-lr", "proj-lr")
    store.set_updates("C-lr", enabled=False)
    poster = SyncFakePoster()

    posted = outbound.poll_once(
        "C-lr", "proj-lr", deps=outbound.OutboundDeps(), poster=poster)

    assert posted == [f"liverun:{state.run_id}:4"]


def test_liverun_detail_escapes_engine_authored_text() -> None:
    """Event details carry profile names, stderr tails and matched ban-signal
    patterns -- none of it operator-typed, and `render.fyi_message` does NOT
    escape what it is handed. Unescaped, a "<!channel>" in a stack trace pings
    the whole workspace."""
    rs, state = _liverun_fixture()
    rs.append_event(state.run_id, "ban_signal", {"pattern": "<!channel> banned"})

    items = outbound._liverun_items(outbound.OutboundDeps(), "proj-lr")

    assert "<!channel>" not in items[3].detail
    assert "&lt;!channel&gt;" in items[3].detail


def test_liverun_detail_is_capped() -> None:
    rs, state = _liverun_fixture()
    rs.append_event(state.run_id, "launch_step",
                    {"name": "rebuild-jar", "ok": False, "exit_code": 1,
                     "stdout": "", "stderr": "boom " * 5000})

    items = outbound._liverun_items(outbound.OutboundDeps(), "proj-lr")

    assert len(items[3].detail) < 1000


def test_a_broken_run_store_does_not_wedge_the_poll() -> None:
    """The supervisor's state directory is written by another process. An
    unreadable one must degrade to "no live-run items", not kill the loop that
    also carries the coding team's progress."""
    def _boom(project_id: str):
        raise OSError("half-written state.json")

    deps = outbound.OutboundDeps(liverun_events_fn=_boom)

    assert outbound._liverun_items(deps, "proj-lr") == []


def test_evidence_png_is_uploaded_when_the_poster_supports_files() -> None:
    rs, state = _liverun_fixture()
    evidence = Path(state.evidence_dir)
    evidence.mkdir(parents=True, exist_ok=True)
    shot = evidence / "window-1.png"
    shot.write_bytes(b"\x89PNG")
    rs.append_event(state.run_id, "evidence",
                    {"id": "client-window", "ok": True, "refs": [str(shot)], "detail": ""})
    store.bind_channel("C-lr", "proj-lr")

    class FilePoster(SyncFakePoster):
        def __init__(self) -> None:
            super().__init__()
            self.files: list[tuple[Any, ...]] = []

        def post_file(self, channel_id, thread_ts, path, title) -> dict[str, Any]:
            self.files.append((channel_id, path, title))
            return {"ok": True}

    poster = FilePoster()
    outbound.poll_once("C-lr", "proj-lr", deps=outbound.OutboundDeps(), poster=poster)

    assert poster.files == [("C-lr", str(shot), "client-window")]


def test_a_poster_without_post_file_still_posts_the_evidence_text() -> None:
    rs, state = _liverun_fixture()
    evidence = Path(state.evidence_dir)
    evidence.mkdir(parents=True, exist_ok=True)
    shot = evidence / "window-1.png"
    shot.write_bytes(b"\x89PNG")
    rs.append_event(state.run_id, "evidence",
                    {"id": "client-window", "ok": True, "refs": [str(shot)], "detail": ""})
    store.bind_channel("C-lr", "proj-lr")
    poster = SyncFakePoster()

    posted = outbound.poll_once(
        "C-lr", "proj-lr", deps=outbound.OutboundDeps(), poster=poster)

    assert f"liverun:{state.run_id}:4" in posted
    assert any("client-window" in m["text"] for m in poster.messages)


def test_a_failed_upload_never_blocks_the_stream() -> None:
    rs, state = _liverun_fixture()
    evidence = Path(state.evidence_dir)
    evidence.mkdir(parents=True, exist_ok=True)
    shot = evidence / "window-1.png"
    shot.write_bytes(b"\x89PNG")
    rs.append_event(state.run_id, "evidence",
                    {"id": "client-window", "ok": True, "refs": [str(shot)], "detail": ""})
    rs.append_event(state.run_id, "phase", {"to": "stopped", "reason": None})
    store.bind_channel("C-lr", "proj-lr")

    class BrokenFilePoster(SyncFakePoster):
        def post_file(self, channel_id, thread_ts, path, title) -> dict[str, Any]:
            raise RuntimeError("slack said no")

    posted = outbound.poll_once(
        "C-lr", "proj-lr", deps=outbound.OutboundDeps(), poster=BrokenFilePoster())

    # The upload failed, but its own text AND everything after it still posted.
    assert posted[-1] == f"liverun:{state.run_id}:5"


def test_terminal_phases_do_not_drift_from_the_supervisor() -> None:
    """`outbound` keeps a local copy of the supervisor's terminal-phase set so
    that importing the bridge never imports the supervisor. A new terminal
    phase added there and not here would silently stop posting through a
    channel mute -- exactly the event a mute is not allowed to hide."""
    from errorta_liverun.state import TERMINAL_PHASES

    assert outbound._LIVERUN_TERMINAL_PHASES == frozenset(TERMINAL_PHASES)


# --------------------------------------------------------------------------
# Slice 2 (fix loop): the acceptance button the SUPERVISOR staged, the fix
# events a mute may not hide, and the autopilot sweep.
#
# The fix cycle stages its own confirmation from the supervisor's thread —
# there is no chat turn to carry it, so `connection._handle_staged_
# confirmations` never sees it. Without a poller branch the button is never
# posted and the cycle times out; without the sweep, autopilot silently does
# not apply to the one loop it was built for.
# --------------------------------------------------------------------------


def _fix_event_deps(kind: str, detail: dict[str, Any], **kw: Any) -> outbound.OutboundDeps:
    state = SimpleNamespace(profile_name="osrs", run_id="r-1", project_id="p1")
    deps = _deps()
    deps.liverun_events_fn = lambda project_id: [
        (state, [{"seq": 1, "at": "2026-08-22T00:00:00", "kind": kind, "detail": detail}])]
    for key, value in kw.items():
        setattr(deps, key, value)
    return deps


def test_fix_accept_staged_reuses_the_staged_confirmation() -> None:
    """Staging a SECOND confirmation here would post a button whose approval
    the fix cycle is not waiting on: it polls the id it staged, so the tap
    would resolve a record nobody reads and the cycle would still time out."""
    store.advance_cursor("C-fix", "")
    deps = _fix_event_deps("fix_accept_staged",
                           {"cid": "abc123", "repo_id": "brain",
                            "human_only": False, "n_paths": 2})
    poster = SyncFakePoster()

    outbound.poll_once("C-fix", "p1", deps=deps, poster=poster)

    assert "abc123" in json.dumps(poster.messages[0]["blocks"])
    # ...and nothing new was staged: the only record is the supervisor's own.
    assert store._load_confirmations() == {}


def test_a_human_only_accept_says_so_on_the_button() -> None:
    store.advance_cursor("C-fix2", "")
    deps = _fix_event_deps("fix_accept_staged",
                           {"cid": "abc123", "repo_id": "brain",
                            "human_only": True, "n_paths": 1})
    poster = SyncFakePoster()

    outbound.poll_once("C-fix2", "p1", deps=deps, poster=poster)

    assert "human" in poster.messages[0]["text"].lower()


@pytest.mark.parametrize("kind,detail", [
    ("fix_idle_cancel", {"idle_s": 1300, "project_id": "p1"}),
    ("fix_accept_staged", {"cid": "abc123", "repo_id": "brain",
                           "human_only": False, "n_paths": 2}),
    ("fix_accepted", {"repo_id": "brain", "delivered_to": "/repo", "head": "abc"}),
    ("fix_cycle_cap", {"cycles_today": 3, "cap": 3}),
    ("relaunch_refused", {"code": "cap_gap"}),
    # A cycle abandoned mid-flight and the merge button that was taken back
    # with it: the operator asked for a stop and is owed the answer to "did
    # anything land?", mute or no mute.
    ("fix_aborted", {"reason": "operator_stop", "repo_id": "brain", "at": "await",
                     "run_cancelled": True, "accept_withdrawn": True, "task_id": "t-1"}),
    ("fix_accept_withdrawn", {"cid": "abc123", "claimed": True, "decision": "declined"}),
])
def test_new_fix_kinds_post_even_when_muted(kind: str, detail: dict[str, Any]) -> None:
    """"Stop updating me" quiets routine progress. It does not get to hide an
    autonomous merge, the loop giving up, or the client not coming back."""
    channel = f"C-mute-{kind}"
    store.advance_cursor(channel, "")
    store.set_updates(channel, enabled=False)
    deps = _fix_event_deps(kind, detail)
    poster = SyncFakePoster()

    posted = outbound.poll_once(channel, "p1", deps=deps, poster=poster)

    assert posted == ["liverun:r-1:1"], f"{kind} must be mandatory"
    assert poster.messages


@pytest.mark.parametrize("kind,detail,wanted", [
    ("fix_aborted", {"reason": "operator_stop", "repo_id": "brain", "at": "await",
                     "run_cancelled": True, "accept_withdrawn": True, "task_id": "t-1"},
     ["operator_stop", "brain"]),
    ("fix_accept_withdrawn", {"cid": "abc123", "claimed": True, "decision": "declined"},
     ["abc123"]),
])
def test_every_fix_kind_the_driver_emits_has_a_written_line(
        kind: str, detail: dict[str, Any], wanted: list[str]) -> None:
    """The fallback renderer dumps raw JSON into the channel. That is a floor
    for a kind nobody has taught it yet — it is not what a kind the driver
    emits on the operator's own stop path should look like."""
    line = outbound._liverun_detail(kind, detail, "fake")

    assert "{" not in line, f"{kind} fell through to the JSON fallback"
    for token in wanted:
        assert token in line


def test_caps_disabled_by_operator_says_it_proceeded_not_paused() -> None:
    """The generic `caps` line says "paused awaiting human" — true for every
    real cap hit, but false for the operator debug switch: that run PROCEEDED.
    A misleading line here is exactly the kind of thing an operator debugging
    a live loop would trust and act on wrong."""
    line = outbound._liverun_detail("caps", {"code": "caps_disabled_by_operator"}, "fake")

    assert "paused" not in line.lower()
    assert "ERRORTA_LIVERUN_CAPS_OFF" in line


def test_fix_team_model_line_names_the_swap() -> None:
    line = outbound._liverun_detail(
        "fix_team_model",
        {"project_id": "senditai-ng", "role": "dev",
         "from": ["claude_cli.sonnet"], "to": "claude_cli.opus"}, "fake")

    assert line == "dev seat → claude_cli.opus (was claude_cli.sonnet)"


def test_fix_cycle_hygiene_line_names_the_counts() -> None:
    line = outbound._liverun_detail(
        "fix_cycle_hygiene",
        {"focuses_archived": 1, "tasks_dropped": 2, "prs_abandoned": 3}, "fake")

    assert line == ("🧹 Retired the previous fix cycle: 1 focus, 2 task(s), "
                     "3 PR(s)")


def test_fix_cycle_hygiene_error_line_names_the_exception() -> None:
    line = outbound._liverun_detail(
        "fix_cycle_hygiene", {"error": "RuntimeError"}, "fake")

    assert line == "🧹 Could not retire the previous fix cycle (RuntimeError) — continuing"


def test_fix_gate_baseline_line_names_the_head_and_verdict() -> None:
    line = outbound._liverun_detail(
        "fix_gate_baseline",
        {"head": "abcdef0123456789", "passed": True, "sandbox": "seatbelt"}, "fake")

    assert line == "🧪 Gate baseline on the clean tree (abcdef012345): PASSED"


def test_fix_gate_baseline_error_line_names_the_exception() -> None:
    line = outbound._liverun_detail(
        "fix_gate_baseline", {"error": "RuntimeError"}, "fake")

    assert line == ("🧪 Could not run the gate baseline on the clean tree "
                     "(RuntimeError) — continuing")


def test_fix_workspace_seeded_line_names_the_source() -> None:
    line = outbound._liverun_detail(
        "fix_workspace_seeded",
        {"project_id": "senditai-ng", "repo_path": "/r/senditai-ng"}, "fake")

    assert line == "seeded the project worktree from /r/senditai-ng"


def test_every_fix_detail_is_escaped_and_capped() -> None:
    """Repo ids and pause codes come off disk and out of the engine, and
    `fyi_message` escapes nothing it is handed."""
    deps = _fix_event_deps("fix_triage",
                           {"repo_id": "<!channel>", "confidence": "deterministic",
                            "rationale": "x" * 5000, "classes": ["brain_log_stall"]})

    items = outbound._liverun_items(deps, "p1")

    assert "<!channel>" not in items[0].detail
    assert len(items[0].detail) <= outbound._LIVERUN_DETAIL_CAP


# --- the autopilot sweep ---------------------------------------------------


class _FireSpy:
    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result = result or {"status": "accepted", "delivered_to": "/repo"}

    def __call__(self, record, *, channel_id, thread_ts, verb, decision, approved):
        self.calls.append({"verb": verb, "cid": record.get("id"), "approved": approved,
                           "channel_id": channel_id, "decision": decision})
        return self._result


def test_sweep_autopilot_claims_once_and_fires_through_the_bridge() -> None:
    spy = _FireSpy()
    deps = _deps()
    deps.fire_effect_fn = spy
    cid = store.stage_confirmation(
        "accept_live_fix", {"project_id": "p1", "run_id": "r-1", "human_only": False,
                            "changed_paths": ["app.py"]}, "", channel_id="C1")
    poster = SyncFakePoster()

    first = outbound.sweep_autopilot("C1", "p1", deps=deps, poster=poster,
                                     config={"autopilot": True})
    second = outbound.sweep_autopilot("C1", "p1", deps=deps, poster=poster,
                                      config={"autopilot": True})

    assert first == [cid] and second == []
    assert len(spy.calls) == 1
    assert spy.calls[0] == {"verb": "accept_live_fix", "cid": cid, "approved": True,
                            "channel_id": "C1", "decision": "approved"}
    assert store.get_confirmation(cid)["state"] == "approved"
    assert poster.messages, "the channel must be told what autopilot did"


def test_sweep_autopilot_is_inert_when_autopilot_is_off() -> None:
    spy = _FireSpy()
    deps = _deps()
    deps.fire_effect_fn = spy
    cid = store.stage_confirmation("accept_live_fix", {"human_only": False}, "",
                                   channel_id="C1")

    assert outbound.sweep_autopilot("C1", "p1", deps=deps, poster=SyncFakePoster(),
                                    config={"autopilot": False}) == []
    assert store.get_confirmation(cid)["state"] == "pending"
    assert spy.calls == []


def test_sweep_autopilot_never_fires_a_human_only_accept() -> None:
    spy = _FireSpy()
    deps = _deps()
    deps.fire_effect_fn = spy
    cid = store.stage_confirmation(
        "accept_live_fix",
        {"project_id": "p1", "run_id": "r-1", "human_only": False,
         "changed_paths": ["senditai_ng/safety/limits.py"]}, "", channel_id="C1")

    assert outbound.sweep_autopilot("C1", "p1", deps=deps, poster=SyncFakePoster(),
                                    config={"autopilot": True}) == []
    assert store.get_confirmation(cid)["state"] == "pending"
    assert spy.calls == []


def test_sweep_autopilot_ignores_every_other_staged_verb() -> None:
    """The sweep exists because the fix cycle has no chat turn to carry its
    confirmation. Every other C-class verb DOES, and was already decided on
    the inbound path — sweeping them here would approve, from a background
    timer, actions a human declined to answer."""
    spy = _FireSpy()
    deps = _deps()
    deps.fire_effect_fn = spy
    for verb in ("start_run", "publish_pr", "spend_cloud", "resume_live_run",
                 "resume_fix_loop", "start_live_run"):
        store.stage_confirmation(verb, {}, "", channel_id="C1")

    assert outbound.sweep_autopilot("C1", "p1", deps=deps, poster=SyncFakePoster(),
                                    config={"autopilot": True}) == []
    assert spy.calls == []
    assert outbound.AUTOPILOT_SWEEP_VERBS == frozenset({"accept_live_fix"})


def test_sweep_autopilot_only_sweeps_its_own_channel() -> None:
    """One tick sweeps every bound channel in turn; a confirmation staged for
    another project's channel must not be fired into this one."""
    spy = _FireSpy()
    deps = _deps()
    deps.fire_effect_fn = spy
    cid = store.stage_confirmation(
        "accept_live_fix", {"project_id": "p2", "run_id": "r-9", "human_only": False},
        "", channel_id="C-other")

    assert outbound.sweep_autopilot("C1", "p1", deps=deps, poster=SyncFakePoster(),
                                    config={"autopilot": True}) == []
    assert store.get_confirmation(cid)["state"] == "pending"


def test_sweep_autopilot_without_a_fire_path_stages_nothing() -> None:
    """No bridge, no verified effect path — and a claim with nothing to run it
    would leave the cycle reading `approved` for a merge that never happened."""
    deps = _deps()
    cid = store.stage_confirmation(
        "accept_live_fix", {"project_id": "p1", "run_id": "r-1", "human_only": False},
        "", channel_id="C1")

    assert outbound.sweep_autopilot("C1", "p1", deps=deps, poster=SyncFakePoster(),
                                    config={"autopilot": True}) == []
    assert store.get_confirmation(cid)["state"] == "pending"


def test_a_failing_effect_still_leaves_the_claim_resolved_and_says_so() -> None:
    def _boom(record, **kw):
        raise RuntimeError("the merge blew up")

    deps = _deps()
    deps.fire_effect_fn = _boom
    cid = store.stage_confirmation(
        "accept_live_fix", {"project_id": "p1", "run_id": "r-1", "human_only": False},
        "", channel_id="C1")
    poster = SyncFakePoster()

    assert outbound.sweep_autopilot("C1", "p1", deps=deps, poster=poster,
                                    config={"autopilot": True}) == [cid]
    assert store.get_confirmation(cid)["state"] == "approved"
    assert any("fail" in m["text"].lower() or "error" in m["text"].lower()
               for m in poster.messages)


@pytest.mark.asyncio
async def test_run_loop_sweeps_autopilot_on_every_tick() -> None:
    spy = _FireSpy()
    deps = _deps()
    deps.fire_effect_fn = spy
    store.bind_channel("C-loop", "p1")
    cid = store.stage_confirmation(
        "accept_live_fix", {"project_id": "p1", "run_id": "r-1", "human_only": False},
        "", channel_id="C-loop")
    stop = asyncio.Event()

    async def _sleep(_s: float) -> None:
        stop.set()

    await outbound.run_loop(
        bindings_provider=lambda: [{"channel_id": "C-loop", "project_id": "p1"}],
        deps=deps, poster=SyncFakePoster(), sleep_fn=_sleep, stop_event=stop,
        config_fn=lambda: {"autopilot": True})

    assert [c["cid"] for c in spy.calls] == [cid]
