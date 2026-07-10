from __future__ import annotations

import asyncio
import logging

import pytest

from errorta_app.routes import diagnostics as diagnostics_routes


class _FakeState:
    pass


class _FakeApp:
    state = _FakeState()


class _FakeRequest:
    app = _FakeApp()

    async def is_disconnected(self) -> bool:
        return False


def _chunk_to_text(chunk: object) -> str:
    if isinstance(chunk, bytes):
        return chunk.decode("utf-8")
    return str(chunk)


@pytest.mark.asyncio
async def test_log_stream_yields_sse_data_and_cleans_handler() -> None:
    root = logging.getLogger()
    before = [
        h
        for h in root.handlers
        if isinstance(h, diagnostics_routes._QueueLogHandler)
    ]
    response = await diagnostics_routes.log_stream(_FakeRequest())
    body_iterator = response.body_iterator
    marker = "F032_STREAM_MARKER"

    async def emit_marker() -> None:
        await asyncio.sleep(0)
        logging.getLogger("f032.stream").warning(marker)

    emit_task = asyncio.create_task(emit_marker())
    try:
        chunk = await asyncio.wait_for(body_iterator.__anext__(), timeout=1)
    finally:
        await body_iterator.aclose()
        await emit_task

    assert response.media_type == "text/event-stream"
    text = _chunk_to_text(chunk)
    assert text.startswith("data: ")
    assert marker in text
    after = [
        h
        for h in root.handlers
        if isinstance(h, diagnostics_routes._QueueLogHandler)
    ]
    assert after == before
