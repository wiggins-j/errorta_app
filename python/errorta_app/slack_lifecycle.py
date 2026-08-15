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


def _stop_locked() -> None:
    global _thread, _loop, _bridge, _start_future
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

    bridge = slack_connection.SlackBridge(sdk_client, poster, deps, caller, config=cfg)
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
