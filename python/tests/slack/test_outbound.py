from __future__ import annotations

import asyncio
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


# --- poll_once: non-blocking attention signal -> plain FYI, no staging -----


@pytest.mark.asyncio
async def test_poll_once_non_blocking_attention_signal_is_fyi_only() -> None:
    signals = [
        _signal("sig-2", created_at="2026-01-01T00:00:00", title="Reviewer note",
                summary="a save button has no autosave guidance", blocking=False),
    ]
    deps = _deps(signals=signals)
    poster = SyncFakePoster()

    markers = outbound.poll_once("C1", "proj-a", deps=deps, poster=poster)

    assert markers == ["attn:sig-2"]
    assert len(poster.messages) == 1
    blocks = poster.messages[0]["blocks"]
    # A plain fyi_message has no "actions" block (no buttons).
    assert all(b.get("type") != "actions" for b in blocks)


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
