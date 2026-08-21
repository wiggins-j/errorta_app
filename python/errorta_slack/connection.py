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

Studio routing (Task 6): a message in the configured studio channel
(``store.is_studio(channel_id)``) is tagged ``item["route"] = "studio"`` in
``handle_event`` and, in ``_process``, answered by
``studio_concierge.run_turn`` instead of the per-project ``concierge.run_turn``
-- through the exact same ack/bot-filter/subtype/allowlist gate every other
inbound message passes first. Like the per-project concierge,
``studio_concierge.run_turn`` always dispatches with ``confirmed_via=None``
(its own module invariant); the ONE place this bridge may pass
``confirmed_via="block_actions"`` to ``studio_tools.dispatch`` is, symmetric
with the per-project case, inside ``handle_interaction`` -- for a resolved
confirmation whose verb is in ``studio_tools.TOOL_CATALOG`` (``create_project``,
``archive_project``, ...; see ``_fire_confirmed_effect``). A studio message on
a bridge with no ``studio_caller`` configured degrades to a plain "not
configured" reply rather than crashing; the per-project path is entirely
unaffected either way.

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
import re
from typing import Any, Awaitable, Callable

from errorta_slack import (
    auth,
    concierge,
    config,
    outbound,
    render,
    studio_concierge,
    studio_tools,
    tools,
)

_LOGGER = logging.getLogger(__name__)

_TURN_ERROR_TEXT = "⚠️ couldn't process that — try again"
_EFFECT_ERROR_TEXT = "⚠️ couldn't complete that action — please check the project directly"
_STUDIO_NOT_CONFIGURED_TEXT = "The studio manager isn't configured on this bridge yet."

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
        studio_caller: Any = None,
        studio_deps_factory: Callable[[], "studio_tools.StudioDeps"] | None = None,
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
        # Studio config is entirely optional (Task 6): a bridge built with
        # neither still routes project-channel traffic exactly as before.
        # ``studio_caller`` mirrors ``caller`` (member dict, prompt) -> text,
        # but bound to whatever model the studio surface uses. When
        # ``studio_deps_factory`` is None, a fresh default ``StudioDeps`` is
        # built per-turn from this bridge's own ``self._deps.store`` -- so
        # ``store.is_studio`` (the routing check) and the deps that later
        # stage/execute a studio confirmation always agree on which store
        # they're reading/writing.
        self._studio_caller = studio_caller
        self._studio_deps_factory = studio_deps_factory

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
        """Ack, filter, dedupe, resolve binding, enqueue. Never dispatches a
        tool itself — every effect this path can trigger runs through
        ``concierge.run_turn``, which always passes ``confirmed_via=None``.

        After the ack (which must stay first — Slack's <=3s requirement —
        and unconditional, regardless of what's dropped below), this drops
        anything that isn't a genuine allowlisted human chat message:

        * non-``message`` events,
        * bot-authored posts (``bot_id`` set) — this INCLUDES the bridge's
          own messages, which Slack echoes back as events; without this
          drop the bridge answers itself forever (the feedback-loop bug),
        * any event carrying a ``subtype`` (``bot_message``,
          ``message_changed``, ``message_deleted``, ``channel_join``,
          etc.) — genuine user messages never have one,
        * events with no human ``user``,
        * a team/user pair that fails ``auth.is_allowed``'s fail-closed
          allowlist check — mirrors the exact discipline
          ``handle_interaction`` already applies before it will act on a
          button click.
        """
        await self._ack(envelope.get("envelope_id"))

        payload = envelope.get("payload") or {}
        event = payload.get("event") or {}
        channel_id = event.get("channel")

        if event.get("type") != "message":
            return
        if event.get("bot_id"):
            return
        if event.get("subtype"):
            return
        user_id = event.get("user")
        if not user_id:
            return

        team_id = str((payload.get("team") or {}).get("id") or payload.get("team_id") or "")
        if not auth.is_allowed(team_id, str(user_id), self._config):
            _LOGGER.warning(
                "slack bridge: dropped inbound message from disallowed "
                "team/user (channel=%s user=%s)", channel_id, user_id,
            )
            return

        event_id = payload.get("event_id")
        if event_id is not None and self._deps.store.seen_event(str(event_id)):
            return

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
        # Studio routing (Task 6): checked AFTER every filter above (ack,
        # bot-filter, subtype, allowlist) has already run -- a studio-channel
        # message gets no less scrutiny than a project-channel one. Tagging
        # the item rather than branching here keeps this method's only job
        # "ack, filter, dedupe, resolve, enqueue" -- the routing decision
        # itself is made once, in _process.
        if channel_id and self._deps.store.is_studio(channel_id):
            item["route"] = "studio"
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
        # Two independent teardowns, each best-effort: a project with no
        # runtime profile makes the first refuse, and that must not swallow
        # the second. A live run left launched after the operator typed "stop"
        # is a real client still playing with nobody watching.
        for verb in ("stop_runtime", "stop_live_run"):
            try:
                tools.dispatch(
                    verb, {},
                    channel_id=channel_id, thread_ts=thread_ts,
                    confirmed_via=None, deps=self._deps,
                )
            except tools.ToolError:
                continue

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
        if item.get("route") == "studio":
            await self._process_studio(thread_ts, item)
            return

        project_id = item.get("project_id")
        if not project_id:
            await self._post_unbound(channel_id, thread_ts)
            return

        autopilot = bool(config.load().get("autopilot"))
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
                autopilot=autopilot,
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

    async def _process_studio(self, thread_ts: str, item: dict[str, Any]) -> None:
        """The studio-channel counterpart of the block above. Same shape
        (to_thread the synchronous run_turn, append history, _post_result on
        success, log+post a generic error on failure) but through
        ``studio_concierge.run_turn`` -- which, like ``concierge.run_turn``,
        always dispatches with ``confirmed_via=None``. Degrades to a clear
        "not configured" reply, rather than crashing, when this bridge has no
        ``studio_caller`` wired up.

        The studio manager has no per-project ledger to resolve a PM route
        from (unlike ``concierge.run_turn``'s ``_resolve_pm_member``), so its
        model route is resolved here, from config, on every turn --
        ``config.load()["studio_model_route"]`` (persisted, user-overridable;
        normalizes to the known-good ``"claude_cli.opus"`` default when
        unset) -- and threaded through as ``model_route``. Without this,
        ``studio_concierge.run_turn`` would fall back to its own default,
        but a turn here should always reflect whatever the user has actually
        configured."""
        channel_id = item.get("channel_id")
        if self._studio_caller is None:
            await self._post_studio_not_configured(channel_id, thread_ts)
            return

        cfg = config.load()
        model_route = str(cfg.get("studio_model_route") or "claude_cli.opus")
        autopilot = bool(cfg.get("autopilot"))

        history = self._thread_history.setdefault(thread_ts, [])
        try:
            result = await asyncio.to_thread(
                studio_concierge.run_turn,
                item.get("text", ""),
                list(history),
                channel_id=channel_id,
                thread_ts=thread_ts,
                deps=self._build_studio_deps(),
                caller=self._studio_caller,
                model_route=model_route,
                autopilot=autopilot,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Same no-message-content-in-logs discipline as the per-project
            # path above.
            _LOGGER.exception(
                "slack bridge: studio turn failed (thread_ts=%s, channel_id=%s)",
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
                "slack bridge: posting the studio reply failed (thread_ts=%s, channel_id=%s)",
                thread_ts, channel_id,
            )
            await self._post_turn_error(channel_id, thread_ts)

    def _build_studio_deps(self) -> "studio_tools.StudioDeps":
        """The one seam both the studio message path (``_process_studio``)
        and the verified studio button path (``handle_interaction``) use to
        get a ``StudioDeps``. Prefers the injected factory (so tests, or a
        real caller, can fully control every engine seam); with none
        configured, falls back to a plain ``StudioDeps`` built on this
        bridge's own ``self._deps.store`` -- the same store
        ``store.is_studio``/the routing check already reads, so a studio
        confirmation staged here is read back consistently."""
        if self._studio_deps_factory is not None:
            return self._studio_deps_factory()
        return studio_tools.StudioDeps(store=self._deps.store)

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
            await self._add_reaction_best_effort(channel_id, ts or thread_ts, name)
        await self._handle_staged_confirmations(channel_id, thread_ts, result)

    # ----------------------------------------------------------------
    # Slice 5: inbound decision rendering + autopilot auto-fire
    #
    # A C-class turn stages a confirmation and returns, inside the turn's
    # ``tool_results``, an entry ``{"verb", "args", "result": {"status":
    # "needs_confirmation", "confirmation_id": cid}}`` (see
    # ``concierge._dispatch_calls`` / ``studio_concierge``). This is the ONE
    # seam that decides what happens to that staged confirmation on the
    # INBOUND (chat) path:
    #
    #   * autopilot OFF (default) -> post ``render.decision_message`` so the
    #     owner gets a real Approve/Decline button. Before Slice 5 the inbound
    #     path posted no button at all -- a chat-staged C-class action was
    #     un-approvable (latent bug). The verified button callback
    #     (``handle_interaction``) then fires it exactly as before.
    #   * autopilot ON -> claim the confirmation with the SAME atomic
    #     ``resolve_confirmation`` the button/timeout paths use, then run the
    #     SAME ``_fire_confirmed_effect``. Autopilot supplies the verified
    #     approval for a *structurally staged* action; it never dispatches
    #     from chat text, and the tool-layer injection guard is untouched
    #     (``dispatch`` still gets ``confirmed_via="block_actions"``).
    # ----------------------------------------------------------------

    @staticmethod
    def _staged_confirmations(result: dict[str, Any]) -> list[dict[str, Any]]:
        """The ``{cid, verb}`` of every needs_confirmation staged this turn."""
        staged: list[dict[str, Any]] = []
        for entry in result.get("tool_results") or []:
            if not isinstance(entry, dict):
                continue
            res = entry.get("result")
            if not isinstance(res, dict) or res.get("status") != "needs_confirmation":
                continue
            cid = res.get("confirmation_id")
            if cid:
                staged.append({"cid": str(cid), "verb": entry.get("verb")})
        return staged

    _CONFIRMATION_COPY: dict[str, tuple[str, str]] = {
        "create_project": (
            "Create project", "Approve to create the project and its Slack channel."),
        "archive_project": (
            "Spin project down", "Approve to pause the project and archive its channel."),
        "start_run": (
            "Start the coding run", "Approve to start the team building (spends model calls)."),
        "spend_cloud": (
            "Spend on a cloud model call", "Approve to spend on this cloud call."),
        "publish_pr": ("Open a public PR", "Approve to open the pull request."),
        "set_next_goal": (
            "Set the team's next goal",
            "Approve to make this the scope the team plans and builds against."),
        "set_north_star": (
            "Rewrite the North Star",
            "Approve to replace the project's durable charter with this."),
        "adopt_project": (
            "Adopt an existing project",
            "Approve to create a PUBLIC Slack channel for this project and bind it."),
        "start_live_run": (
            "Start live run",
            "Approve to launch and supervise the live run from this profile."),
        "resume_live_run": (
            "Resume live run (human only)",
            "Approve to clear the paused-awaiting-human hold on this profile."),
    }

    # Which arg fields each verb must SHOW before it can be approved, in order.
    # Without these the button renders a verb name and nothing else, and for
    # `set_next_goal` that is load-bearing rather than cosmetic: the
    # `propose_next_goal` docstring justifies reading untrusted repo content
    # on the grounds that "the proposal reaches the ledger only through
    # set_next_goal, whose confirmation renders the full title and body so a
    # human reads the exact text before it becomes the team's scope". If the
    # body is never rendered, that second control does not exist and the chain
    # "hostile repo file -> proposed body -> approved unseen -> stored Focus"
    # completes with only the title inspected.
    _CONFIRMATION_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
        "set_next_goal": (("title", "Goal"), ("body", "Scope")),
        "set_north_star": (
            ("north_star", "North Star"), ("definition_of_done", "Definition of done")),
        "adopt_project": (("project_id", "Project"),),
        "publish_pr": (("title", "PR title"), ("body", "PR description")),
        "spend_cloud": (("reason", "Reason"),),
    }

    # Slack rejects a section over 3000 characters outright, which would mean a
    # staged C-class action with NO approve button at all -- strictly worse
    # than a shortened one. Budget: the title line (<= ~160 escaped) plus two
    # capped fields plus their labels plus the base copy stays comfortably
    # under the limit at 1100 each.
    _SLACK_SECTION_LIMIT = 3000
    _CONFIRMATION_FIELD_CAP = 1100
    _CONFIRMATION_LABEL_CAP = 120
    _TRUNCATION_NOTE = ("…\n_(truncated — open the project to read the full text "
                        "before approving)_")
    # A cut can land mid-entity ("&amp;" -> "&am"), which renders as literal
    # junk. Drop any dangling entity prefix left at the tail.
    _PARTIAL_ENTITY_RE = re.compile(r"&[A-Za-z]{0,4}$")

    @classmethod
    def _cap_escaped(cls, text: str, cap: int, note: str = "…") -> str:
        """Truncate ALREADY-ESCAPED text so the result is at most ``cap``.

        The order matters and is the whole point: ``escape_mrkdwn`` EXPANDS
        (one ``<`` becomes four characters, one ``&`` five), so capping the raw
        text and escaping afterwards bounds the wrong string. A body of mrkdwn
        control characters -- HTML, XML, or generics in a repo file, i.e. the
        exact input the escaping exists for -- came out ~5x over budget that
        way and Slack rejected the whole message with ``invalid_blocks``,
        leaving a staged C-class action with no Approve button. Escape first,
        then cap what Slack actually receives.
        """
        if len(text) <= cap:
            return text
        kept = cls._PARTIAL_ENTITY_RE.sub("", text[:max(cap - len(note), 0)])
        return kept + note

    @classmethod
    def _confirmation_field(cls, label: str, value: Any) -> str:
        text = str(value if value is not None else "").strip()
        if not text:
            return ""
        capped = cls._cap_escaped(
            render.escape_mrkdwn(text), cls._CONFIRMATION_FIELD_CAP,
            cls._TRUNCATION_NOTE)
        return f"*{label}:*\n{capped}"

    def _confirmation_title(self, record: dict[str, Any]) -> tuple[str, str]:
        """A (title, detail) for the decision button, from the staged record's
        verb + args. The detail renders the actual content being approved --
        the goal title AND body, the proposed North Star text, the project
        being adopted -- because "Confirm set_north_star / Approve to run this
        action" asks a human to verify a durable-charter rewrite while showing
        them nothing but a verb name.

        Field text is escaped (it can carry model- or repo-derived content;
        see ``render.escape_mrkdwn``) and length-capped, and only ever
        contains args the owner or the concierge already put in this channel
        -- no secrets, no tokens."""
        verb = str(record.get("verb") or "")
        args = record.get("args") or {}
        base_title, detail = self._CONFIRMATION_COPY.get(
            verb, (f"Confirm {verb}", "Approve to run this action."),
        )
        # Same order here: escape, then cap the escaped result.
        label = self._cap_escaped(
            render.escape_mrkdwn(
                str(args.get("title") or args.get("profile")
                    or args.get("project_id") or "").strip()),
            self._CONFIRMATION_LABEL_CAP)
        title = f"{base_title} — {label}" if label else base_title
        fields = [
            rendered for key, field_label in self._CONFIRMATION_FIELDS.get(verb, ())
            if (rendered := self._confirmation_field(field_label, args.get(key)))
        ]
        if fields:
            detail = "\n\n".join([*fields, detail])
        return title, detail

    async def _handle_staged_confirmations(
        self, channel_id: Any, thread_ts: str, result: dict[str, Any],
    ) -> None:
        if self._poster is None:
            return
        staged = self._staged_confirmations(result)
        if not staged:
            return
        autopilot = bool(config.load().get("autopilot"))
        for item in staged:
            cid = item["cid"]
            # The verb is read back off the STAGED RECORD, not off the turn
            # result: the record is what `_autopilot_fire` would actually
            # execute, so gating on anything else could carve out one verb
            # while firing another.
            record = self._deps.store.get_confirmation(cid)
            verb = str((record or {}).get("verb") or "")
            if autopilot and verb not in tools.HUMAN_ONLY_VERBS:
                await self._autopilot_fire(channel_id, thread_ts, cid)
            else:
                # A human-only verb falls through to the button even under
                # autopilot -- see tools.HUMAN_ONLY_VERBS.
                await self._post_decision_button(channel_id, thread_ts, cid)

    async def _post_decision_button(self, channel_id: Any, thread_ts: str, cid: str) -> None:
        record = self._deps.store.get_confirmation(cid)
        if record is None:
            # Already resolved/expired between staging and now -- nothing to
            # approve, so nothing to render.
            return
        title, detail = self._confirmation_title(record)
        await self._poster.post_message(
            channel_id, thread_ts, title, blocks=render.decision_message(title, detail, cid),
        )

    async def _autopilot_fire(self, channel_id: Any, thread_ts: str, cid: str) -> None:
        # Same atomic claim the button + timeout sweep share: only the winner
        # fires. A concurrent tap/sweep having already claimed it -> no-op.
        record, claimed = self._deps.store.resolve_confirmation(cid, "approved")
        if not claimed:
            return
        verb = str(record["verb"])
        try:
            effect_result = self._fire_confirmed_effect(
                record, channel_id=str(channel_id or ""), thread_ts=thread_ts,
                verb=verb, decision="approved", approved=True,
            )
        except Exception:
            _LOGGER.exception(
                "slack bridge: autopilot failed to execute the confirmed effect "
                "for verb=%s (confirmation_id=%s)", verb, cid,
            )
            await self._post_effect_error(channel_id, thread_ts)
            return
        await self._post_autopilot_outcome(verb, channel_id, thread_ts, effect_result)

    async def _post_autopilot_outcome(
        self, verb: str, channel_id: Any, thread_ts: str,
        effect_result: dict[str, Any] | None,
    ) -> None:
        if self._poster is None:
            return
        if verb in studio_tools.TOOL_CATALOG:
            # Reuse the verb-specific studio outcome copy (created / archived /
            # partial failure), prefixed so the audit trail is unmistakable.
            if verb == "create_project":
                detail = self._create_project_outcome_text(effect_result)
            elif verb == "archive_project":
                detail = self._archive_project_outcome_text(effect_result)
            else:
                detail = f"{verb}: {(effect_result or {}).get('status', 'done')}."
            text = f"🤖 Autopilot approved — {detail}"
        elif (reason := self._non_execution_reason(effect_result)) is not None:
            # The verb reported failure ("error" -- e.g. a logged-out provider)
            # or was refused outright ("refused" -- start_gate on a project with
            # no operative goal, the EXPECTED outcome for every freshly adopted
            # project). Neither started anything, so neither may be announced
            # as "approved & executed"; say what happened and why.
            text = f"⚠️ Autopilot approved *{verb}*, but it didn't run — {reason}"
        else:
            text = f"🤖 Autopilot approved & executed *{verb}*."
        await self._poster.post_message(channel_id, thread_ts, text, blocks=None)

    async def _add_reaction_best_effort(self, channel_id: Any, ts: Any, name: str) -> None:
        """A reaction is cosmetic, not part of the reply -- the reply has
        already posted successfully by the time this runs. If Slack rejects
        it (e.g. ``invalid_name``) or the call otherwise fails, that must
        never surface as a turn error on top of an already-good reply.
        ``asyncio.CancelledError`` still propagates (it's not a reaction
        failure, it's the worker being torn down)."""
        try:
            await self._poster.add_reaction(channel_id, ts, name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Metadata only (channel/thread + reaction name + exception
            # type) -- never message text/tokens.
            _LOGGER.warning(
                "slack bridge: add_reaction failed (channel_id=%s, thread_ts=%s, "
                "name=%s, exception_type=%s)",
                channel_id, ts, name, type(exc).__name__,
            )

    async def _post_unbound(self, channel_id: Any, thread_ts: str) -> None:
        if self._poster is None:
            return
        await self._poster.post_message(
            channel_id, thread_ts,
            "This channel isn't bound to a project yet.",
            blocks=None,
        )

    async def _post_studio_not_configured(self, channel_id: Any, thread_ts: str) -> None:
        if self._poster is None:
            return
        await self._poster.post_message(
            channel_id, thread_ts, _STUDIO_NOT_CONFIGURED_TEXT, blocks=None,
        )

    # A verb can be approved and still not run. `start_run` refuses outright
    # when the project has no operative goal (`next_goal.start_gate`), and
    # `set_next_goal`/`set_north_star` report a rejected argument the same way
    # -- by RETURNING a status, not by raising. Announcing "executed" for
    # either is the precise failure this slice was written to fix, one layer
    # further up: the owner is told the team is building when nothing started
    # and the computed reason has been thrown away.
    _NON_EXECUTION_STATUSES = tools.NOT_DONE_STATUSES

    # A no-op is not a failure, so "no reason was given" would be its own small
    # untruth: nothing went wrong and nothing needs explaining. Each carries the
    # actual reason it did nothing.
    _NON_EXECUTION_DEFAULTS = {
        "already_running": "the team is already working on it",
        "not_running": "no run was in progress",
        "empty": "there was nothing to do",
    }

    @classmethod
    def _non_execution_reason(cls, result: dict[str, Any] | None) -> str | None:
        status = (result or {}).get("status")
        if status not in cls._NON_EXECUTION_STATUSES:
            return None
        detail = str((result or {}).get("detail") or "").strip()
        if detail:
            return detail
        return cls._NON_EXECUTION_DEFAULTS.get(str(status), "no reason was given")

    async def _post_decision_outcome(
        self, channel_id: Any, thread_ts: str, verb: str, approved: bool,
        result: dict[str, Any] | None = None,
    ) -> None:
        if self._poster is None:
            return
        if not approved:
            text = f"Declined — {verb} was not executed."
        elif (reason := self._non_execution_reason(result)) is not None:
            text = f"⚠️ Approved, but *{verb}* did not run — {reason}"
        else:
            text = f"Approved — {verb} executed."
        await self._poster.post_message(channel_id, thread_ts, text, blocks=None)

    async def _post_studio_outcome(
        self, verb: str, channel_id: Any, thread_ts: str, approved: bool,
        result: dict[str, Any] | None,
    ) -> None:
        """The studio-C-verb counterpart of ``_post_decision_outcome`` --
        unlike a generic per-project tool verb (fire-and-forget, always
        "executed"), a studio verb (``create_project``, ``archive_project``,
        ...) can fail partway through (e.g. channel provisioning/archiving)
        and reports that via its return value's ``status``, not an
        exception. This composes a clear, verb-specific outcome message
        instead of a blanket "executed" that would be wrong on a partial
        failure. Dispatches by ``verb`` to a small per-verb text renderer so
        adding a future studio C-verb only means adding one more renderer
        here, not another hardcoded routing branch upstream."""
        if self._poster is None:
            return
        if not approved:
            text = f"Declined — {verb} was not executed."
        elif verb == "create_project":
            text = self._create_project_outcome_text(result)
        elif verb == "adopt_project":
            text = self._adopt_project_outcome_text(result)
        elif verb == "archive_project":
            text = self._archive_project_outcome_text(result)
        else:
            # A studio C-verb with no bespoke renderer yet still gets a
            # truthful, generic status line rather than silently posting
            # nothing (or crashing on a KeyError).
            status = (result or {}).get("status", "unknown")
            text = f"{verb}: {status}."
        await self._poster.post_message(channel_id, thread_ts, text, blocks=None)

        # The confirmation above lands in the STUDIO channel, where the create
        # was requested. Post it into the newly bound project channel too: that
        # is the channel the operator actually watches, and where this run's
        # progress stream will appear. Top-level (thread_ts=None) -- the new
        # channel has no thread to reply into.
        if approved and verb in ("create_project", "adopt_project"):
            new_channel = (result or {}).get("channel_id")
            if new_channel and new_channel != channel_id:
                try:
                    await self._poster.post_message(
                        new_channel, None, text, blocks=None)
                except Exception:  # noqa: BLE001 - best effort, mirrors _post_effect_error
                    _LOGGER.warning(
                        "could not post the %s confirmation into the new channel",
                        verb, exc_info=True)

    def _create_project_outcome_text(self, result: dict[str, Any] | None) -> str:
        if result is not None and result.get("status") == "created":
            head = (
                f"Created project `{result.get('project_id')}` — "
                f"channel <#{result.get('channel_id')}|{result.get('channel_name')}>."
            )
            return head + self._start_outcome_suffix(result)
        detail = (result or {}).get("detail") or "unknown error"
        return f"⚠️ couldn't create the project — {detail}"

    def _adopt_project_outcome_text(self, result: dict[str, Any] | None) -> str:
        """Adopt had no renderer of its own, so it fell through to the generic
        ``f"{verb}: {status}."`` line -- which silently dropped
        ``start_refused``, leaving the operator believing a run had begun."""
        if result is not None and result.get("status") == "adopted":
            head = (
                f"Adopted project `{result.get('project_id')}` — "
                f"channel <#{result.get('channel_id')}|{result.get('channel_name')}>."
            )
            return head + self._start_outcome_suffix(result)
        detail = (result or {}).get("detail") or "unknown error"
        return f"⚠️ couldn't adopt the project — {detail}"

    def _start_outcome_suffix(self, result: dict[str, Any]) -> str:
        """Report the run's ACTUAL start state, shared by create and adopt.

        Read from the result dict, never from the model's own reply text: on a
        live run the PM claimed "Goal set and run started" when neither had
        happened. A renderer fed by the real return value cannot do that.

        The north star is escaped -- it is operator-authored, but it reaches
        here through a model-parsed charter, and an unescaped ``<!channel>``
        in a message body pings the workspace.
        """
        if result.get("started"):
            north_star = str(result.get("north_star") or "").strip()
            if north_star:
                # _cap_escaped, not a hand-rolled slice: escaping EXPANDS, and a
                # naive cut can land mid-entity ("&amp;" -> "&am"), which renders
                # as literal junk. That helper already trims a dangling entity.
                capped = self._cap_escaped(
                    render.escape_mrkdwn(north_star), 300)
                return f"\nRun started with north star as: {capped}"
            return "\nRun started."
        refused = str(result.get("start_refused") or "").strip()
        if refused:
            return f"\n⚠️ the run did NOT start — {render.escape_mrkdwn(refused)}"
        return "\n⚠️ the run did NOT start."

    def _archive_project_outcome_text(self, result: dict[str, Any] | None) -> str:
        if result is not None and result.get("status") == "archived":
            project_id = result.get("project_id")
            channel_id = result.get("channel_id")
            if channel_id:
                return f"Spun down project `{project_id}` — channel <#{channel_id}> archived."
            return f"Spun down project `{project_id}` (no Slack channel was bound)."
        detail = (result or {}).get("detail") or "unknown error"
        return f"⚠️ couldn't spin the project down — {detail}"

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
    ) -> dict[str, Any] | None:
        """Run the effect a claimed confirmation authorizes. Three routes:

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
        * any verb in ``studio_tools.TOOL_CATALOG`` (e.g. ``create_project``,
          ``archive_project``) — every studio **C**-class verb. None of these
          are in ``tools.TOOL_CATALOG`` at all, so they are routed to
          ``studio_tools.dispatch`` instead of ``tools.dispatch``, using a
          ``StudioDeps`` built via ``_build_studio_deps`` — the SAME
          ``confirmed_via="block_actions"`` marker, but the studio's own
          bounded tool surface. Only on Approve; its ``{"status": ...}``
          result is returned (rather than discarded, unlike the other two
          routes) so ``handle_interaction`` can tell the channel what
          actually happened (created vs. archived vs. failed partway
          through). Checking membership in the catalog — rather than a
          hardcoded ``verb == "create_project"`` string — is deliberate: a
          hardcoded check silently stops routing every NEW studio C-verb to
          ``studio_tools.dispatch``, sending it to the per-project
          ``tools.dispatch`` instead, where it isn't in that catalog either
          and fails closed with ``tool_not_allowed`` — the verb's real,
          verified effect would then simply never fire. This is exactly the
          gap ``archive_project`` shipped into before this fix.
        * every real per-project tool verb — unchanged: ``tools.dispatch(...,
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
            return None
        if verb in studio_tools.TOOL_CATALOG:
            if not approved:
                return None
            return studio_tools.dispatch(
                verb, dict(record.get("args") or {}),
                channel_id=channel_id, thread_ts=thread_ts,
                confirmed_via="block_actions", deps=self._build_studio_deps(),
            )
        if approved:
            # Return the dispatch result (rather than discarding it) so an
            # autopilot audit line can be honest about a verb that "ran" but
            # reported a failure status (e.g. start_run -> provider logged
            # out). handle_interaction's per-project outcome ignores this
            # return, so surfacing it changes nothing on the button path.
            return tools.dispatch(
                verb, dict(record.get("args") or {}),
                channel_id=channel_id, thread_ts=thread_ts,
                confirmed_via="block_actions", deps=self._deps,
            )
        return None

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
    # attention-signal resolution. Any studio C-verb confirmation (e.g.
    # "create_project", "archive_project" — anything in
    # studio_tools.TOOL_CATALOG) is a second, symmetric special case: same
    # claim-then-fire discipline, but routed to studio_tools.dispatch
    # instead of tools.dispatch (see _fire_confirmed_effect) since none of
    # them are per-project verbs at all.
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
            effect_result = self._fire_confirmed_effect(
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

        if verb in studio_tools.TOOL_CATALOG:
            await self._post_studio_outcome(
                verb, channel_id, thread_ts, approved, effect_result,
            )
        else:
            await self._post_decision_outcome(
                channel_id, thread_ts, verb, approved, effect_result,
            )

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
