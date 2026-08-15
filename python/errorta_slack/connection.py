"""Socket Mode ingress and async orchestration (Task 8).

``SlackBridge`` is the seam between an injected Socket Mode client and the
rest of the bridge (``concierge``, ``tools``, ``store``, ``auth``,
``render``). It owns:

* Event ingress (``handle_event``): ack, dedupe via ``store.seen_event``,
  binding lookup, and enqueue onto a per-``thread_ts`` FIFO.
* Per-thread serialization (``_drain``): exactly one turn in flight per
  thread at a time, processed strictly in arrival order, with a BOUNDED
  look-ahead cancel scan — see the module docstring section below.
* The verified interaction callback (``handle_interaction``): the ONLY
  place in this bridge that may call ``tools.dispatch(...,
  confirmed_via="block_actions")``. The one exception to "dispatch" as the
  effect: an ``outbound.ATTENTION_VERB`` confirmation (staged by
  ``outbound.poll_once`` for a blocking coding-team attention signal, not a
  real ``tools.TOOL_CATALOG`` verb) resolves through the injected
  ``deps.attention_resolve_fn`` instead — see the "Verified interaction"
  section below.
* Connection lifecycle (``start``/``stop``) with capped exponential
  backoff on connect failure.

CRITICAL INVARIANT (carried from Tasks 4/5's reviews — enforced by
construction here, not just documented): ``handle_event`` never calls
``tools.dispatch`` itself. It only ever reaches the engine through
``concierge.run_turn``, which always dispatches with ``confirmed_via=None``
(Task 5's invariant). So a crafted Slack message whose TEXT says "approve"
can, at most, cause a C-class verb to be *staged* — it can never reach a
real effect with ``confirmed_via="block_actions"``. That marker is set in
exactly one place in this whole bridge: inside ``handle_interaction``,
after verifying the payload is a real ``block_actions`` interaction and
resolving a staged confirmation record via ``store``.

Cancel look-ahead: a per-thread worker (``_drain``) runs one turn at a
time via ``asyncio.to_thread`` (since ``concierge.run_turn`` is
synchronous). While that turn is in flight, the worker also watches its
queue (an ``asyncio.Event`` set by ``handle_event`` on every enqueue) so
it can react the instant something new arrives — WITHOUT dequeuing it
early. On wake it does a bounded scan: drain whatever is *currently*
buffered (never waits for more), look for exactly one cancel token (a
message whose text is "stop"/"cancel"/"abort"), and either (a) restore
everything untouched and keep waiting on the same in-flight turn (a
normal message arrived — FIFO order for it is preserved, nothing is
skipped or reordered), or (b) cancel the in-flight turn's task, invoke a
cancel hook (default: best-effort ``stop_runtime``), and consume — not
re-enqueue — the cancel token, since it was an instruction to the bridge,
not a message that itself deserves a full concierge turn.

This module MAY import ``slack_sdk`` types, but only behind a guarded
import (see below) — merely importing ``errorta_slack.connection`` must
not fail when ``slack-sdk`` isn't installed. Every actual Slack client is
injected (``sdk_client``, ``poster``); this module never constructs one
itself, so in practice it does not need the real types at runtime — the
guarded import exists so a future caller can safely add type-checked
helpers here without breaking that guarantee.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Awaitable, Callable

from errorta_slack import auth, concierge, outbound, render, tools

_LOGGER = logging.getLogger(__name__)

_TURN_ERROR_TEXT = "⚠️ couldn't process that — try again"
_EFFECT_ERROR_TEXT = "⚠️ couldn't complete that action — please check the project directly"

try:  # pragma: no cover - exercised by test_connection_module_import_is_safe_without_slack_sdk
    from slack_sdk.socket_mode.request import SocketModeRequest  # noqa: F401
except ImportError:  # slack-sdk is an optional dependency of this optional bridge.
    SocketModeRequest = None  # type: ignore[assignment,misc]


# A private sentinel used to tell a per-thread worker to stop draining and
# exit (pushed by ``stop()``). Never observable outside this module.
_STOP = object()

_CANCEL_TEXTS = {"stop", "cancel", "abort"}


def _is_cancel_item(item: Any) -> bool:
    """Whether a queued item is an explicit cancel token (look-ahead only —
    never used to decide how a message is answered, only whether it should
    interrupt an in-flight turn)."""
    if not isinstance(item, dict):
        return False
    text = str(item.get("text", "")).strip().lower()
    return text in _CANCEL_TEXTS


def _is_valid_block_actions_payload(payload: Any) -> bool:
    """Structural check that ``payload`` is a real Slack ``block_actions``
    interaction with exactly the shape ``render.decision_message`` produces
    (``action_id`` in {``slack_approve``, ``slack_decline``}, a non-empty
    ``value`` carrying the confirmation id). Fails closed on anything else —
    a malformed or foreign payload never reaches ``tools.dispatch``."""
    if not isinstance(payload, dict):
        return False
    if payload.get("type") != "block_actions":
        return False
    actions = payload.get("actions")
    if not isinstance(actions, list) or not actions:
        return False
    action = actions[0]
    if not isinstance(action, dict):
        return False
    if action.get("action_id") not in ("slack_approve", "slack_decline"):
        return False
    if not action.get("value"):
        return False
    return True


def _swallow_task_result(task: "asyncio.Task[Any]") -> None:
    """``done_callback`` for a cancelled-and-abandoned in-flight task — a
    background thread we told the world we no longer care about must never
    surface an 'exception was never retrieved' warning."""
    with contextlib.suppress(BaseException):
        task.exception()


class SlackBridge:
    """Socket Mode ingress + per-thread FIFO orchestration.

    ``sdk_client`` and ``poster`` are injected so tests run with fakes and
    no real network. Neither is connected at construction — call
    ``await start()`` for that.
    """

    def __init__(
        self,
        sdk_client: Any,
        poster: Any,
        deps: "tools.ToolDeps",
        caller: Any,
        *,
        config: dict[str, Any] | None = None,
        backoff_base: float = 1.0,
        backoff_cap: float = 30.0,
        backoff_max_attempts: int | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
        cancel_hook: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._sdk_client = sdk_client
        self._poster = poster
        self._deps = deps
        self._caller = caller
        self._config = config or {}
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._backoff_max_attempts = backoff_max_attempts
        self._sleep_fn = sleep_fn
        self._cancel_hook = cancel_hook

        self._queues: dict[str, "asyncio.Queue[Any]"] = {}
        self._workers: dict[str, "asyncio.Task[Any]"] = {}
        self._new_item_events: dict[str, asyncio.Event] = {}
        self._inflight_tasks: dict[str, "asyncio.Task[Any]"] = {}
        self._thread_history: dict[str, list[dict[str, Any]]] = {}
        self._running = False

    # ----------------------------------------------------------------
    # Event ingress
    # ----------------------------------------------------------------

    async def handle_event(self, envelope: dict[str, Any]) -> None:
        """Ack, dedupe, resolve binding, enqueue. Never dispatches a tool
        itself — every effect this path can trigger runs through
        ``concierge.run_turn``, which always passes ``confirmed_via=None``."""
        await self._ack(envelope.get("envelope_id"))

        payload = envelope.get("payload") or {}
        event_id = payload.get("event_id")
        if event_id is not None and self._deps.store.seen_event(str(event_id)):
            return

        event = payload.get("event") or {}
        channel_id = event.get("channel")
        binding = self._deps.store.binding_for(channel_id) if channel_id else None
        project_id = binding.get("project_id") if binding else None
        thread_ts = str(event.get("thread_ts") or event.get("ts") or "")

        item = {
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "text": str(event.get("text", "")),
            "user": event.get("user"),
            "project_id": project_id,
        }
        await self._enqueue(thread_ts, item)

    async def _ack(self, envelope_id: Any) -> None:
        if envelope_id is None or self._sdk_client is None:
            return
        ack_fn = getattr(self._sdk_client, "ack", None)
        if ack_fn is None:
            return
        result = ack_fn(envelope_id)
        if asyncio.iscoroutine(result):
            await result

    async def _enqueue(self, thread_ts: str, item: dict[str, Any]) -> None:
        queue = self._queues.setdefault(thread_ts, asyncio.Queue())
        event = self._new_item_events.setdefault(thread_ts, asyncio.Event())
        await queue.put(item)
        event.set()
        worker = self._workers.get(thread_ts)
        if worker is None or worker.done():
            self._workers[thread_ts] = asyncio.create_task(self._drain(thread_ts))

    # ----------------------------------------------------------------
    # Per-thread FIFO worker + bounded cancel look-ahead
    # ----------------------------------------------------------------

    async def _drain(self, thread_ts: str) -> None:
        queue = self._queues[thread_ts]
        event = self._new_item_events.setdefault(thread_ts, asyncio.Event())
        while True:
            item = await self._next_item(queue)
            if item is _STOP:
                return
            event.clear()
            task: "asyncio.Task[Any]" = asyncio.create_task(self._process(thread_ts, item))
            self._inflight_tasks[thread_ts] = task
            await self._await_with_lookahead(task, queue, event, thread_ts)
            self._inflight_tasks.pop(thread_ts, None)

    @staticmethod
    async def _next_item(queue: "asyncio.Queue[Any]") -> Any:
        try:
            return queue.get_nowait()
        except asyncio.QueueEmpty:
            return await queue.get()

    async def _await_with_lookahead(
        self,
        task: "asyncio.Task[Any]",
        queue: "asyncio.Queue[Any]",
        event: asyncio.Event,
        thread_ts: str,
    ) -> None:
        """Wait for ``task`` to finish while watching for a cancel token.

        This is the ONLY place this bridge peeks ahead in a thread's
        queue. It never dequeues a normal message early: a wake that turns
        out not to be a cancel token restores the queue exactly as found
        and goes back to waiting on the same in-flight task.
        """
        while True:
            wait_new = asyncio.create_task(event.wait())
            done, _pending = await asyncio.wait(
                {task, wait_new}, return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
                if not wait_new.done():
                    wait_new.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await wait_new
                # `_process` catches and handles every exception it can
                # anticipate (logs + posts a user-facing error) and never
                # lets one propagate out under normal operation. This is a
                # last-resort backstop: if something still escaped (a bug in
                # the handling above, not cancellation), log it so it is
                # never silently dropped.
                if not task.cancelled():
                    exc = task.exception()
                    if exc is not None:
                        _LOGGER.error(
                            "slack bridge: unhandled worker exception on thread_ts=%s: %r",
                            thread_ts, exc,
                        )
                return

            event.clear()
            cancel_item = self._scan_and_consume_cancel(queue)
            if cancel_item is not None:
                task.cancel()
                task.add_done_callback(_swallow_task_result)
                self._fire_cancel_hook(thread_ts, cancel_item)
                return
            # False alarm: something new arrived but it wasn't a cancel
            # token. Leave it queued (already restored by the scan) and
            # keep waiting on the same in-flight task — FIFO is untouched.

    @staticmethod
    def _scan_and_consume_cancel(queue: "asyncio.Queue[Any]") -> dict[str, Any] | None:
        """Bounded (non-blocking) scan of whatever is CURRENTLY buffered in
        ``queue`` for the first cancel token. If found, it is removed
        (consumed) and everything else is put back in its original order.
        If not found, the queue is restored untouched."""
        buffered: list[Any] = []
        cancel_item: dict[str, Any] | None = None
        while True:
            try:
                candidate = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if cancel_item is None and candidate is not _STOP and _is_cancel_item(candidate):
                cancel_item = candidate
                continue
            buffered.append(candidate)
        for entry in buffered:
            queue.put_nowait(entry)
        return cancel_item

    def _fire_cancel_hook(self, thread_ts: str, cancel_item: dict[str, Any]) -> None:
        hook = self._cancel_hook or self._default_cancel_hook
        hook(thread_ts, cancel_item)

    def _default_cancel_hook(self, thread_ts: str, cancel_item: dict[str, Any]) -> None:
        """Best-effort default: ask the bound project's runtime to stop.
        Never raises — a cancel token must never crash the worker."""
        channel_id = cancel_item.get("channel_id")
        project_id = cancel_item.get("project_id")
        if not channel_id or not project_id:
            return
        try:
            tools.dispatch(
                "stop_runtime", {},
                channel_id=channel_id, thread_ts=thread_ts,
                confirmed_via=None, deps=self._deps,
            )
        except tools.ToolError:
            pass

    async def wait_idle(self, thread_ts: str, *, timeout: float = 2.0) -> None:
        """Test/ops helper: block until ``thread_ts``'s queue is empty and
        nothing is in flight, or raise ``TimeoutError``."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            queue = self._queues.get(thread_ts)
            idle = (queue is None or queue.empty()) and self._inflight_tasks.get(thread_ts) is None
            if idle:
                return
            if loop.time() > deadline:
                raise TimeoutError(f"thread {thread_ts!r} did not go idle within {timeout}s")
            await asyncio.sleep(0.01)

    # ----------------------------------------------------------------
    # Turn execution + posting
    # ----------------------------------------------------------------

    async def _process(self, thread_ts: str, item: dict[str, Any]) -> None:
        channel_id = item.get("channel_id")
        project_id = item.get("project_id")
        if not project_id:
            await self._post_unbound(channel_id, thread_ts)
            return

        history = self._thread_history.setdefault(thread_ts, [])
        try:
            result = await asyncio.to_thread(
                concierge.run_turn,
                item.get("text", ""),
                list(history),
                project_id=project_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                deps=self._deps,
                caller=self._caller,
            )
        except asyncio.CancelledError:
            # A cancel look-ahead intentionally interrupted this turn -- not
            # a failure. Never surface a user-facing error for it.
            raise
        except Exception:
            # Never log the raw item/prompt here -- it may carry a user's
            # Slack message text. Metadata only (thread/channel + the
            # exception's own type/message) — no tokens, no secrets, no
            # message content.
            _LOGGER.exception(
                "slack bridge: turn failed (thread_ts=%s, channel_id=%s)",
                thread_ts, channel_id,
            )
            await self._post_turn_error(channel_id, thread_ts)
            return

        history.append({"role": "user", "text": item.get("text", "")})
        reply = result.get("reply")
        if reply:
            history.append({"role": "assistant", "text": reply})

        try:
            await self._post_result(channel_id, thread_ts, result)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception(
                "slack bridge: posting the reply failed (thread_ts=%s, channel_id=%s)",
                thread_ts, channel_id,
            )
            await self._post_turn_error(channel_id, thread_ts)

    async def _post_turn_error(self, channel_id: Any, thread_ts: str) -> None:
        if self._poster is None:
            return
        # Best-effort: if even posting the error fails, there's nothing
        # further to do -- swallow rather than let it crash the worker.
        # `Exception` (not `BaseException`) so a genuine CancelledError from
        # an overlapping cancel still propagates.
        with contextlib.suppress(Exception):
            await self._poster.post_message(channel_id, thread_ts, _TURN_ERROR_TEXT, blocks=None)

    async def _post_result(self, channel_id: Any, thread_ts: str, result: dict[str, Any]) -> None:
        if self._poster is None:
            return
        text = str(result.get("reply", ""))
        blocks = render.fyi_message(text) if text else None
        posted = await self._poster.post_message(channel_id, thread_ts, text, blocks=blocks)
        ts = posted.get("ts") if isinstance(posted, dict) else None
        for name in render.reactions_for(result):
            await self._poster.add_reaction(channel_id, ts or thread_ts, name)

    async def _post_unbound(self, channel_id: Any, thread_ts: str) -> None:
        if self._poster is None:
            return
        await self._poster.post_message(
            channel_id, thread_ts,
            "This channel isn't bound to a project yet.",
            blocks=None,
        )

    async def _post_decision_outcome(
        self, channel_id: Any, thread_ts: str, verb: str, approved: bool,
    ) -> None:
        if self._poster is None:
            return
        text = (
            f"Approved — {verb} executed." if approved
            else f"Declined — {verb} was not executed."
        )
        await self._poster.post_message(channel_id, thread_ts, text, blocks=None)

    async def _post_effect_error(self, channel_id: Any, thread_ts: str) -> None:
        if self._poster is None:
            return
        # Best-effort, mirrors _post_turn_error: if even posting the error
        # fails, there's nothing further to do.
        with contextlib.suppress(Exception):
            await self._poster.post_message(channel_id, thread_ts, _EFFECT_ERROR_TEXT, blocks=None)

    def _fire_confirmed_effect(
        self, record: dict[str, Any], *, channel_id: str, thread_ts: str,
        verb: str, decision: str, approved: bool,
    ) -> None:
        """Run the effect a claimed confirmation authorizes. Two routes:

        * ``outbound.ATTENTION_VERB`` — not a ``tools.TOOL_CATALOG`` verb, so
          it never reaches ``tools.dispatch``. Both Approve AND Decline do
          real work here (unlike a tool verb, where Decline is a pure no-op):
          the decision is written through to the real attention signal via
          ``deps.attention_resolve_fn``, using the same decision -> action
          mapping the Task 9 timeout sweep uses
          (``outbound.attention_decision_action``) so approve/decline/timeout
          all resolve identically. Raises if no safe action exists for the
          signal's kind (e.g. a "problem", whose only actions are
          accept/correct) or if the resolve call itself fails — caught by
          the caller, never left to crash the callback.
        * every real tool verb — unchanged: ``tools.dispatch(...,
          confirmed_via="block_actions")``, and ONLY on Approve (Decline is
          still a pure no-op for a real tool).
        """
        if verb == outbound.ATTENTION_VERB:
            args = record.get("args") or {}
            project_id = str(args.get("project_id") or "")
            signal_id = str(args.get("signal_id") or "")
            signal_kind = str(args.get("signal_kind") or "")
            action = outbound.attention_decision_action(signal_kind, decision)
            if action is None:
                raise RuntimeError(
                    f"no safe attention.resolve action for signal {signal_id!r} "
                    f"(kind={signal_kind!r}, decision={decision!r})"
                )
            self._deps.attention_resolve_fn(
                project_id, signal_id, action, by="slack",
                store=self._deps.ledger_factory(project_id),
            )
        elif approved:
            tools.dispatch(
                verb, dict(record.get("args") or {}),
                channel_id=channel_id, thread_ts=thread_ts,
                confirmed_via="block_actions", deps=self._deps,
            )

    # ----------------------------------------------------------------
    # Verified interaction (button) callback
    #
    # This is the ONLY method in this bridge allowed to call
    # tools.dispatch(..., confirmed_via="block_actions"). It only reaches
    # that call after: (1) the payload structurally is a real block_actions
    # interaction with a recognized action_id and non-empty value: (2) the
    # team/user pass auth.is_allowed's fail-closed allowlist check; (3) it
    # WON the atomic claim on a confirmation record staged earlier by
    # tools.dispatch's confirmed_via=None path; and only for Approve — a
    # Decline resolves the record without ever calling tools.dispatch. The
    # ONE exception is an outbound.ATTENTION_VERB confirmation (see
    # _fire_confirmed_effect) — confirmed_via="block_actions" is reserved
    # for real tool verbs only, so that marker never appears on an
    # attention-signal resolution.
    #
    # CLAIM, not check-then-act (Task 9 review carry-over): resolving is the
    # single atomic operation that authorizes the effect. This method used to
    # read get_confirmation, check state == "pending", and only THEN resolve
    # — a window in which the Task 9 background timeout sweep
    # (store.pop_pending_older_than) could resolve the very same confirmation
    # first, and both paths would fire the effect. Now the only existence
    # check before the claim is "does this id exist at all" (get_confirmation
    # is None); whether it's still eligible to fire is decided entirely by
    # store.resolve_confirmation's own atomic pending->decision transition,
    # which reports back whether THIS caller won it.
    #
    # The effect itself (dispatch OR attention_resolve_fn) runs inside a
    # try/except: an unlisted verb (tools.ToolError) or a signal kind with no
    # safe action (RuntimeError from _fire_confirmed_effect) must never
    # escape this callback uncaught — the confirmation is already claimed at
    # that point (state changed, so it can't be retried), so silently
    # crashing would leave it permanently stuck with no visible outcome. A
    # clean, generic error is posted instead (Task 9 review Critical 2).
    # ----------------------------------------------------------------

    async def handle_interaction(self, payload: dict[str, Any]) -> None:
        if not _is_valid_block_actions_payload(payload):
            return

        team_id = str((payload.get("team") or {}).get("id") or "")
        user_id = str((payload.get("user") or {}).get("id") or "")
        if not auth.is_allowed(team_id, user_id, self._config):
            return

        action = payload["actions"][0]
        action_id = action.get("action_id")
        confirmation_id = action.get("value")

        record = self._deps.store.get_confirmation(confirmation_id)
        if record is None:
            return

        channel_id = str((payload.get("channel") or {}).get("id") or "")
        thread_ts = str(record.get("thread_ts") or "")
        verb = str(record["verb"])
        approved = action_id == "slack_approve"
        decision = "approved" if approved else "declined"

        _, claimed = self._deps.store.resolve_confirmation(confirmation_id, decision)
        if not claimed:
            # Someone else already resolved it first -- a double-click/replayed
            # interaction, or the background timeout sweep won the race. The
            # effect must fire at most once, so this call is a silent no-op.
            return

        try:
            self._fire_confirmed_effect(
                record, channel_id=channel_id, thread_ts=thread_ts,
                verb=verb, decision=decision, approved=approved,
            )
        except Exception:
            _LOGGER.exception(
                "slack bridge: failed to execute the confirmed effect for "
                "verb=%s (confirmation_id=%s)", verb, confirmation_id,
            )
            await self._post_effect_error(channel_id, thread_ts)
            return

        await self._post_decision_outcome(channel_id, thread_ts, verb, approved)

    # ----------------------------------------------------------------
    # Connection lifecycle
    # ----------------------------------------------------------------

    async def start(self) -> None:
        """Connect the injected client, retrying with capped exponential
        backoff on failure. Does nothing if already running."""
        if self._running:
            return
        self._running = True
        attempt = 0
        while True:
            try:
                if self._sdk_client is not None:
                    result = self._sdk_client.connect()
                    if asyncio.iscoroutine(result):
                        await result
                return
            except Exception:
                attempt += 1
                if self._backoff_max_attempts is not None and attempt >= self._backoff_max_attempts:
                    self._running = False
                    raise
                delay = min(self._backoff_base * (2 ** (attempt - 1)), self._backoff_cap)
                await self._sleep_fn(delay)

    async def stop(self) -> None:
        """Stop every per-thread worker and disconnect the injected client."""
        self._running = False
        for thread_ts, queue in list(self._queues.items()):
            await queue.put(_STOP)
        workers = list(self._workers.values())
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        if self._sdk_client is not None:
            result = self._sdk_client.disconnect()
            if asyncio.iscoroutine(result):
                await result


__all__ = ["SlackBridge"]
