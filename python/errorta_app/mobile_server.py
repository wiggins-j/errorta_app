"""F065 — the dedicated LAN listener for the mobile companion.

A SEPARATE ASGI app + uvicorn server that serves ONLY the `/mobile/v1/*` API
(all auth-gated) plus a bare `/healthz`, over TLS, bound to a SPECIFIC LAN IP.
The main sidecar (``server.py``) stays loopback — so the LAN attack surface is
just the mobile API, never council/corpus/gateway/diagnostics.

Threading: uvicorn installs signal handlers (main-thread only), so the listener
runs ``Server.serve()`` on a dedicated daemon thread + its own event loop with
signal handlers disabled. Mobile-driven ``create_run`` therefore executes on
this second loop, sharing ``${ERRORTA_HOME}`` state with the main loop through
the same file-locked RunStore (the writer-token model serializes writes).
"""
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

from fastapi import FastAPI

from errorta_app.routes import mobile as mobile_routes

# Cap a request body on the LAN surface (the mobile API only ever sends small
# JSON; large bodies are rejected before handlers run).
MAX_BODY_BYTES = 64 * 1024
DEFAULT_LIMIT_CONCURRENCY = 64


def build_mobile_app() -> FastAPI:
    """The LAN ASGI app: ONLY the mobile router + a bare liveness probe.

    No CORS middleware (the iOS client is not a browser; a permissive CORS on a
    LAN surface would be a foothold). The health endpoint returns bare liveness
    — no version / config / device count (that stays on loopback /settings).
    """
    app = FastAPI(
        title="Errorta mobile LAN",
        description="LAN-facing mobile companion API (auth-gated).",
        # No docs on the LAN surface.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(_BodySizeLimitMiddleware, max_bytes=MAX_BODY_BYTES)
    app.include_router(mobile_routes.router)

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:  # noqa: D401 - bare liveness only
        return {"ok": True}

    return app


class _BodySizeLimitMiddleware:
    """Reject requests whose body exceeds ``max_bytes`` (Content-Length or
    streamed) before they reach a handler."""

    def __init__(self, app, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        cl = headers.get(b"content-length")
        if cl is not None:
            try:
                if int(cl) > self.max_bytes:
                    await _too_large(send)
                    return
            except ValueError:
                await _too_large(send)
                return

        total = 0

        async def guarded_receive():
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b"") or b"")
                if total > self.max_bytes:
                    return {"type": "http.disconnect"}
            return message

        await self.app(scope, guarded_receive, send)


async def _too_large(send) -> None:
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": b'{"detail":"payload_too_large"}'})


class LanListener:
    def __init__(self, server, thread: threading.Thread, *, host: str, port: int) -> None:
        self._server = server
        self._thread = thread
        self.host = host
        self.port = port

    def stop(self, timeout: float = 5.0) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        return self._thread.is_alive()


def start_lan_listener(
    *,
    host: str,
    port: int,
    certfile: str | Path,
    keyfile: str | Path,
    limit_concurrency: int = DEFAULT_LIMIT_CONCURRENCY,
    startup_timeout: float = 5.0,
) -> LanListener:
    """Start the TLS LAN listener on a dedicated daemon thread.

    Fails closed: refuses ``0.0.0.0`` (would expose every interface) and refuses
    to start without both cert + key present (real-TLS-or-refuse).
    """
    import uvicorn

    if host in ("0.0.0.0", "::", ""):
        # Never bind all interfaces — bind the specific chosen LAN IP only.
        raise ValueError("mobile_lan_bind_must_be_specific")
    certfile, keyfile = Path(certfile), Path(keyfile)
    if not certfile.exists() or not keyfile.exists():
        raise FileNotFoundError("mobile_lan_tls_missing")

    app = build_mobile_app()
    config = uvicorn.Config(
        app, host=host, port=port,
        ssl_certfile=str(certfile), ssl_keyfile=str(keyfile),
        log_level="warning", limit_concurrency=limit_concurrency,
        # We manage shutdown via should_exit on a background thread.
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # not the main thread

    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve()),
        name="errorta-mobile-lan",
        daemon=True,
    )
    thread.start()

    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        if getattr(server, "started", False):
            break
        if not thread.is_alive():
            raise RuntimeError("mobile_lan_listener_exited_on_start")
        time.sleep(0.02)
    return LanListener(server, thread, host=host, port=port)


__all__ = [
    "DEFAULT_LIMIT_CONCURRENCY",
    "MAX_BODY_BYTES",
    "LanListener",
    "build_mobile_app",
    "start_lan_listener",
]
