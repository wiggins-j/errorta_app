"""F-INFRA-06 — Local diagnostic bundle export route.

POST /diagnostics/export — write a redacted zip to ``dest_path`` and return
its sha256, file list, and per-rule redaction counts. ``dest_path`` must be
an absolute path; relative paths are rejected with HTTP 400.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from errorta_diagnostics import build_bundle
from errorta_diagnostics import redact as _redact

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])


def _redact_line(line: str) -> str:
    """F086 Slice B: redact a single live log line before it leaves the process.

    The live log-tail / log-stream endpoints previously returned RAW lines —
    tokens, home paths, and SSH hosts leaked through the diagnostics UI. Route
    every line through the shared redaction pipeline (same rules the bundle
    export uses), so these surfaces carry no secrets.
    """
    redacted, _counts = _redact.apply_pipeline(
        str(line),
        home=os.environ.get("HOME"),
        username=os.environ.get("USER"),
    )
    return redacted


class ExportBody(BaseModel):
    dest_path: str = Field(min_length=1)
    user_note: str = ""


class ExportResponse(BaseModel):
    path: str
    sha256: str
    redaction_manifest: dict[str, Any]
    files: list[str]


class LogTailResponse(BaseModel):
    lines: list[str]


class _QueueLogHandler(logging.Handler):
    def __init__(self, queue: asyncio.Queue[str], loop: asyncio.AbstractEventLoop) -> None:
        super().__init__()
        self._queue = queue
        self._loop = loop
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - logging guard
        try:
            line = self.format(record)
            self._loop.call_soon_threadsafe(self._put_nowait_drop_oldest, line)
        except Exception:
            pass

    def _put_nowait_drop_oldest(self, line: str) -> None:
        try:
            self._queue.put_nowait(line)
            return
        except asyncio.QueueFull:
            pass

        try:
            self._queue.get_nowait()
        except asyncio.QueueEmpty:
            pass

        try:
            self._queue.put_nowait(line)
        except asyncio.QueueFull:
            pass


def _sse_data(line: str) -> str:
    cleaned = line.replace("\r", " ").replace("\n", " ")
    return f"data: {cleaned}\n\n"


@router.post("/export", response_model=ExportResponse)
def export_bundle(body: ExportBody, request: Request) -> ExportResponse:
    dest = Path(body.dest_path)
    if not dest.is_absolute():
        raise HTTPException(status_code=400, detail="dest_path must be absolute")

    # Refuse to write into a directory that does not exist and cannot be created.
    parent = dest.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"dest_path parent not writable: {e}") from e

    log_buffer = getattr(request.app.state, "log_buffer", None)

    try:
        result = build_bundle(dest, user_note=body.user_note, log_buffer=log_buffer)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"bundle write failed: {e}") from e

    return ExportResponse(**result)


@router.get("/log-tail", response_model=LogTailResponse)
def log_tail(
    request: Request,
    lines: int = Query(default=200, ge=1),
) -> LogTailResponse:
    capped = min(lines, 5000)
    log_buffer = getattr(request.app.state, "log_buffer", None)
    if log_buffer is None:
        return LogTailResponse(lines=[])
    tail = getattr(log_buffer, "tail", None)
    if callable(tail):
        return LogTailResponse(lines=[_redact_line(ln) for ln in tail(capped)])
    snapshot = getattr(log_buffer, "snapshot", lambda: [])()
    return LogTailResponse(lines=[_redact_line(ln) for ln in list(snapshot)[-capped:]])


@router.get("/lifecycle")
def lifecycle(request: Request, tail_lines: int = Query(default=40, ge=0, le=500)) -> dict[str, Any]:
    """F048 — sidecar lifecycle: pid, version, residency, config signature, and
    an optional capped+redacted recent log tail. The frontend captures the
    first ``config_signature`` it sees and recommends a restart if a later poll
    shows a different one (a restart-relevant setting changed)."""
    from errorta_diagnostics import lifecycle as _lifecycle

    out: dict[str, Any] = _lifecycle.sidecar_lifecycle()
    try:
        from errorta_mobile import config as mobile_config

        out["mobile_connector"] = mobile_config.public_status()
    except Exception:
        out["mobile_connector"] = {"available": False}
    log_buffer = getattr(request.app.state, "log_buffer", None)
    if log_buffer is not None and tail_lines > 0:
        out["recent_log_tail"] = _lifecycle.redacted_log_tail(
            log_buffer, lines=tail_lines
        )
    return out


@router.get("/log-stream")
async def log_stream(request: Request) -> StreamingResponse:
    async def events():
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1024)
        loop = asyncio.get_running_loop()
        handler = _QueueLogHandler(queue, loop)
        handler.setLevel(logging.NOTSET)
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield _sse_data(_redact_line(line))
        finally:
            root.removeHandler(handler)
            handler.close()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
