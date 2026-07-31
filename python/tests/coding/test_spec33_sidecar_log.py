"""SPEC-33 — the sidecar log handler must never raise from emit.

Run 10 crashed in `RedactingSidecarLogHandler.emit` with
`AttributeError: 'NoneType' object has no attribute 'write'` (the stream was None
after close(), but a background _sync_grounding task still emitted), and the
default handleError re-raised into the same missing stream ("Logging error").
"""
from __future__ import annotations

import logging
from pathlib import Path

from errorta_app.sidecar_log import RedactingSidecarLogHandler


def _record(msg: str = "hello") -> logging.LogRecord:
    return logging.LogRecord("t", logging.INFO, __file__, 1, msg, None, None)


def test_emit_reopens_a_none_stream(tmp_path: Path) -> None:
    log = tmp_path / "sidecar.log"
    h = RedactingSidecarLogHandler(log)
    h.stream = None  # simulate a closed/teardown stream
    h.emit(_record("after close"))  # must not raise
    assert "after close" in log.read_text()


def test_emit_never_raises_when_stream_unreopenable(tmp_path: Path) -> None:
    log = tmp_path / "sidecar.log"
    h = RedactingSidecarLogHandler(log)
    h.stream = None
    # Make reopen impossible (baseFilename points at a directory).
    h.baseFilename = str(tmp_path)  # opening a dir for append raises
    h.emit(_record("dropped, not crashed"))  # must not raise -> record dropped


def test_close_then_emit_does_not_crash(tmp_path: Path) -> None:
    log = tmp_path / "sidecar.log"
    h = RedactingSidecarLogHandler(log)
    h.emit(_record("first"))
    h.close()  # sets self.stream = None
    h.emit(_record("second after close"))  # must not raise
    assert "second after close" in log.read_text()


def test_handleError_is_inert(tmp_path: Path, capsys) -> None:
    h = RedactingSidecarLogHandler(tmp_path / "sidecar.log")
    h.handleError(_record())  # must not raise, must not write anywhere
    err = capsys.readouterr().err
    assert "Logging error" not in err


def test_healthy_stream_still_writes_and_redacts(tmp_path: Path) -> None:
    log = tmp_path / "sidecar.log"
    h = RedactingSidecarLogHandler(log)
    h.emit(_record("normal line"))
    assert "normal line" in log.read_text()
