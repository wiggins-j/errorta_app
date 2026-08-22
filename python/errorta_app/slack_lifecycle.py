"""Task 10 — reconcile the Slack PM bridge's live connection to config.

Mirrors ``errorta_app.mobile_lifecycle``'s process-singleton ``sync()``/
``stop()`` pattern. ``sync()`` is the single entry point: it stops any
running bridge and (re)starts one, but ONLY when ALL three gates pass:

1. ``errorta_slack.config.is_enabled()`` -- off by default.
2. ``slack-sdk`` is importable -- an optional extra
   (``pip install errorta-app[slack]``); a sidecar build that never
   installed it must keep booting normally.
3. Both Slack tokens are on disk (``errorta_slack.secrets.load_tokens()``).

Any gate failing returns ``{"running": False, "reason": <why>}`` WITHOUT
raising -- this is the whole optionality guarantee (see
``tests/slack/test_optionality.py``): the sidecar must boot and run
identically whether or not this bridge is installed, configured, or
enabled.

Called from the sidecar lifespan on boot (next to
``mobile_lifecycle.sync()``) and from shutdown (``stop()``, next to
``mobile_lifecycle.stop()``). Deliberately NOT called from
``errorta_slack.routes``'s ``POST /slack/enable``/``disable`` -- those
routes only persist the config flag; reconciling the live connection is
this module's job alone, exercised at boot/shutdown, so that flipping the
flag never blocks an HTTP request on live Slack network I/O.

Both ``import errorta_slack...`` and ``import slack_sdk`` happen INSIDE
``sync()`` (lazy), never at this module's top level -- importing
``errorta_app.slack_lifecycle`` (and therefore ``errorta_app.server``,
which imports it inside the lifespan function body, itself never at
module top level either) must never require either package to be
installed.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

_LOG = logging.getLogger("errorta_app.slack_lifecycle")

_lock = threading.Lock()
_thread: threading.Thread | None = None
_loop: "asyncio.AbstractEventLoop | None" = None
_bridge: Any = None
_start_future: "asyncio.Future | None" = None
_outbound_thread: "threading.Thread | None" = None
_outbound_stop: "threading.Event | None" = None


def _stop_locked() -> None:
    global _thread, _loop, _bridge, _start_future
    _stop_outbound()
    if _start_future is not None and not _start_future.done():
        _start_future.cancel()
    if _bridge is not None and _loop is not None and _loop.is_running():
        try:
            stop_future = asyncio.run_coroutine_threadsafe(_bridge.stop(), _loop)
            stop_future.result(timeout=5)
        except Exception as exc:  # pragma: no cover - defensive
            _LOG.warning("error stopping slack bridge: %s", exc)
    if _loop is not None:
        try:
            _loop.call_soon_threadsafe(_loop.stop)
        except Exception:  # pragma: no cover - defensive
            pass
    if _thread is not None:
        _thread.join(timeout=5)
    _thread = None
    _loop = None
    _bridge = None
    _start_future = None


def _build_sync_poster(bot_token: str) -> Any:
    """A SYNCHRONOUS poster for the outbound progress loop.

    Deliberately not ``_build_poster``. ``outbound.poll_once`` calls
    ``poster.post_message(...)`` without awaiting it, so handing it the bridge's
    async poster would build a coroutine, drop it on the floor, and post
    nothing at all -- a silent failure whose only symptom is a
    "coroutine was never awaited" warning. ``slack_sdk``'s ``WebClient`` is
    itself synchronous, so this is the natural shape; the async poster is the
    adapted one.
    """
    from slack_sdk import WebClient

    web_client = WebClient(token=bot_token)

    class _SyncWebClientPoster:
        def post_message(
            self, channel_id: Any, thread_ts: Any, text: str, blocks: Any = None,
        ) -> dict[str, Any]:
            kwargs: dict[str, Any] = {"channel": channel_id, "text": text}
            if thread_ts:
                kwargs["thread_ts"] = thread_ts
            if blocks:
                kwargs["blocks"] = blocks
            return dict(web_client.chat_postMessage(**kwargs).data)

        def post_file(
            self, channel_id: Any, thread_ts: Any, path: str, title: str,
        ) -> dict[str, Any]:
            """The OPTIONAL half of the poster duck-type (see
            `outbound._post_attachment`): uploads a live-run evidence
            screenshot. A poster without this method still posts every item's
            text."""
            kwargs: dict[str, Any] = {"channel": channel_id, "file": path, "title": title}
            if thread_ts:
                kwargs["thread_ts"] = thread_ts
            return dict(web_client.files_upload_v2(**kwargs).data)

    return _SyncWebClientPoster()


def _start_outbound(
    poster: Any, *, run_loop_fn: Any = None,
    interval_s: float | None = None, timeout_minutes: float | None = None,
    fire_effect_fn: Any = None,
) -> None:
    """Run the outbound progress loop on its own thread.

    Its own thread, not the bridge's event loop: ``poll_once`` is fully
    synchronous and does ledger file I/O plus one blocking HTTP post per item.
    Running that on the ingress loop would stall Socket Mode acks (Slack wants
    one within 3s) every tick. ``run_loop``'s only await is its sleep, so a
    private loop on a private thread costs nothing and isolates the blocking.

    ``stop_event`` is a ``threading.Event``, not an ``asyncio.Event``:
    ``run_loop`` only ever calls ``.is_set()`` on it, and the setter lives on
    another thread -- ``asyncio.Event.set()`` is not threadsafe, while
    ``threading.Event.set()`` is.

    ``fire_effect_fn`` is the bridge's own ``_fire_confirmed_effect`` -- the
    verified path a button tap takes. The outbound autopilot sweep
    (``outbound.sweep_autopilot``) needs it to fire a confirmation the live-run
    fix cycle staged off-channel; without it the sweep claims nothing at all.
    Passing the bound method (rather than letting ``outbound`` call
    ``tools.dispatch`` itself) keeps ``confirmed_via="block_actions"`` set in
    exactly ONE place in this bridge. It is called from the outbound thread,
    which is safe: the method is synchronous, touches no event loop, and the
    store's own RLock covers the claim.
    """
    global _outbound_thread, _outbound_stop

    _stop_outbound()

    from errorta_slack import config as slack_config
    from errorta_slack import outbound as slack_outbound
    from errorta_slack import store as slack_store

    run_loop = run_loop_fn or slack_outbound.run_loop
    stop_event = threading.Event()
    # Explicit arguments win; otherwise the operator's config does. Passing
    # NEITHER (what this did before) meant `run_loop`'s signature defaults won
    # and both config keys were inert -- a `timeout_minutes` the owner set was
    # read by the sweep nowhere.
    cfg = slack_config.load()
    interval = float(
        interval_s if interval_s is not None else cfg.get("interval_s", 15))
    timeout = float(
        timeout_minutes if timeout_minutes is not None else cfg.get("timeout_minutes", 30))

    def _target() -> None:
        try:
            asyncio.run(run_loop(
                bindings_provider=slack_store.list_bindings,
                deps=slack_outbound.OutboundDeps(fire_effect_fn=fire_effect_fn),
                poster=poster,
                stop_event=stop_event,
                interval_s=interval,
                timeout_minutes=timeout,
                config_fn=slack_config.load,
            ))
        except Exception:  # pragma: no cover - defensive
            _LOG.warning("slack outbound loop exited", exc_info=True)

    thread = threading.Thread(
        target=_target, name="slack-outbound-loop", daemon=True)
    _outbound_stop = stop_event
    _outbound_thread = thread
    thread.start()


def _stop_outbound() -> None:
    """Signal the outbound loop and wait briefly for it to notice.

    It checks the flag at the top of each tick, so a loop mid-sleep exits
    WITHOUT polling once more -- a restart cannot double-post. The join is
    short and the thread is a daemon: a slow in-flight HTTP post must not hold
    up a bridge restart or sidecar shutdown.
    """
    global _outbound_thread, _outbound_stop

    if _outbound_stop is not None:
        _outbound_stop.set()
    if _outbound_thread is not None:
        _outbound_thread.join(timeout=5)
    _outbound_thread = None
    _outbound_stop = None


def _build_poster(bot_token: str) -> Any:
    """A ``connection.SlackBridge``-shaped poster backed by a real
    ``slack_sdk.WebClient``. ``post_message``/``add_reaction`` are the only
    two methods the bridge calls on it."""
    from slack_sdk import WebClient

    web_client = WebClient(token=bot_token)

    class _WebClientPoster:
        async def post_message(
            self, channel_id: Any, thread_ts: str, text: str, *, blocks: Any = None,
        ) -> dict[str, Any]:
            kwargs: dict[str, Any] = {"channel": channel_id, "text": text}
            if thread_ts:
                kwargs["thread_ts"] = thread_ts
            if blocks:
                kwargs["blocks"] = blocks
            result = await asyncio.to_thread(web_client.chat_postMessage, **kwargs)
            return dict(result.data)

        async def add_reaction(self, channel_id: Any, ts: Any, name: str) -> None:
            if not ts:
                return
            await asyncio.to_thread(
                web_client.reactions_add, channel=channel_id, timestamp=ts, name=name,
            )

    return _WebClientPoster()


def _build_studio_deps_factory(bot_token: str, tool_deps: Any) -> Any:
    """A callable that lazily builds a ``studio_tools.StudioDeps`` wired
    with a real ``slack_sdk.WebClient`` for channel provisioning (the
    studio manager's ``create_project`` calls ``conversations.create`` /
    ``invite`` / ``setTopic`` through it). Mirrors ``_build_poster``'s
    guard: ``slack_sdk`` is imported only inside this function, never at
    this module's top level. ``tool_deps.store`` is reused (rather than a
    second import of ``errorta_slack.store``) so this always agrees with
    the same store the bridge's own project-channel deps read/write."""
    from slack_sdk import WebClient

    from errorta_slack import studio_tools as slack_studio_tools

    web_client = WebClient(token=bot_token)

    def _factory() -> Any:
        return slack_studio_tools.StudioDeps(store=tool_deps.store, web_client=web_client)

    return _factory


def _build_sdk_client(app_token: str, bot_token: str, bridge_holder: dict[str, Any],
                       loop: "asyncio.AbstractEventLoop") -> Any:
    """A ``connection.SlackBridge``-shaped ``sdk_client`` backed by a real
    ``slack_sdk.socket_mode.SocketModeClient``. Incoming Socket Mode
    envelopes are forwarded onto ``bridge_holder["bridge"]`` on ``loop`` --
    ``bridge_holder`` is a one-item mutable box because the bridge instance
    doesn't exist yet at the point this listener is registered (the bridge
    is constructed with this very adapter as one of its arguments)."""
    from slack_sdk import WebClient
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.response import SocketModeResponse

    web_client = WebClient(token=bot_token)
    raw_client = SocketModeClient(app_token=app_token, web_client=web_client)

    def _on_request(client: Any, req: Any) -> None:
        bridge = bridge_holder.get("bridge")
        if bridge is None:
            return
        if req.type == "interactive":
            asyncio.run_coroutine_threadsafe(bridge.handle_interaction(req.payload), loop)
            # `handle_interaction` never acks (only `handle_event` does, via
            # the adapter's `ack()` below) -- every Socket Mode envelope
            # needs exactly one ack, so this path owns it.
            try:
                client.send_socket_mode_response(
                    SocketModeResponse(envelope_id=req.envelope_id))
            except Exception:  # pragma: no cover - defensive, best-effort
                _LOG.debug("slack bridge: ack failed for envelope %s", req.envelope_id)
        else:
            envelope = {"envelope_id": req.envelope_id, "payload": req.payload}
            asyncio.run_coroutine_threadsafe(bridge.handle_event(envelope), loop)

    raw_client.socket_mode_request_listeners.append(_on_request)

    class _SdkClientAdapter:
        def connect(self) -> None:
            raw_client.connect()

        def disconnect(self) -> None:
            raw_client.disconnect()

        def ack(self, envelope_id: Any) -> None:
            if not envelope_id:
                return
            raw_client.send_socket_mode_response(
                SocketModeResponse(envelope_id=envelope_id))

    return _SdkClientAdapter()


def _start_locked(cfg: dict[str, Any], app_token: str, bot_token: str) -> dict[str, Any]:
    global _thread, _loop, _bridge, _start_future

    from errorta_council.coding.runner import gateway_member_caller
    from errorta_council.gateway_local import LocalGateway
    from errorta_slack import connection as slack_connection
    from errorta_slack import tools as slack_tools

    ready = threading.Event()
    loop_box: dict[str, Any] = {}

    def _run_loop() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop_box["loop"] = loop
        ready.set()
        try:
            loop.run_forever()
        finally:
            loop.close()

    thread = threading.Thread(target=_run_loop, name="slack-bridge-loop", daemon=True)
    thread.start()
    if not ready.wait(timeout=5):
        raise RuntimeError("slack bridge event loop did not start in time")
    loop = loop_box["loop"]

    bridge_holder: dict[str, Any] = {}
    poster = _build_poster(bot_token)
    sdk_client = _build_sdk_client(app_token, bot_token, bridge_holder, loop)
    deps = slack_tools.ToolDeps()
    caller = gateway_member_caller(LocalGateway())
    # Studio manager (Task 6/7): its own member/route, resolved like a PM,
    # and its own provisioning WebClient (channel creation/invite/topic) --
    # both entirely optional from the bridge's point of view (a bridge with
    # no studio channel bound still serves project channels unaffected; see
    # connection.py's `_process_studio` degrade-to-"not configured" path).
    studio_caller = gateway_member_caller(LocalGateway())
    studio_deps_factory = _build_studio_deps_factory(bot_token, deps)

    bridge = slack_connection.SlackBridge(
        sdk_client, poster, deps, caller, config=cfg,
        studio_caller=studio_caller,
        studio_deps_factory=studio_deps_factory,
    )
    bridge_holder["bridge"] = bridge

    # Fire-and-forget: `bridge.start()` connects (and, on failure, retries
    # with capped exponential backoff -- see connection.py) entirely on the
    # background loop. `sync()` never blocks on live Slack network I/O, so
    # it stays fast and deterministic for callers (sidecar boot). Any
    # eventual connect failure is logged, not raised here.
    start_future = asyncio.run_coroutine_threadsafe(bridge.start(), loop)

    def _on_start_done(fut: "asyncio.Future") -> None:
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is not None:
            _LOG.warning("slack bridge: background connect failed: %s", exc)

    start_future.add_done_callback(_on_start_done)

    # The progress stream. Built and shipped long before this line existed:
    # `outbound.run_loop` had no production caller at all, so nothing was ever
    # posted into a bound channel unprompted.
    _start_outbound(_build_sync_poster(bot_token),
                    fire_effect_fn=bridge._fire_confirmed_effect)

    _thread = thread
    _loop = loop
    _bridge = bridge
    _start_future = start_future
    return {"running": True}


def sync() -> dict[str, Any]:
    """(Re)start or stop the Slack bridge to match config. Returns a status
    dict; never raises."""
    with _lock:
        _stop_locked()

        from errorta_slack import config as slack_config

        cfg = slack_config.load()
        if not cfg.get("enabled"):
            return {"running": False, "reason": "disabled"}

        try:
            import slack_sdk  # noqa: F401
        except ImportError:
            return {"running": False, "reason": "sdk_missing"}

        from errorta_slack import secrets as slack_secrets

        tokens = slack_secrets.load_tokens()
        app_token = (tokens or {}).get("app_token")
        bot_token = (tokens or {}).get("bot_token")
        if not app_token or not bot_token:
            return {"running": False, "reason": "no_tokens"}

        try:
            return _start_locked(cfg, app_token, bot_token)
        except Exception as exc:  # pragma: no cover - defensive
            _LOG.warning("slack bridge failed to start: %s", exc)
            return {"running": False, "reason": "start_failed", "error": str(exc)}


def stop() -> None:
    with _lock:
        _stop_locked()


__all__ = ["sync", "stop"]
