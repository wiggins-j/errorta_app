"""Spec 22 Item 1 — the sidecar's own on-disk log sink.

Two halves cover the two ways a sidecar is started, and they are deliberately
NOT the same mechanism:

* **CLI-spawned** (``errorta_cli.sidecar._launch``) — the CLI opens
  ``${ERRORTA_HOME}/logs/sidecar.log`` and hands the fd to the child as both
  stdout and stderr, so a crash traceback, uvicorn's banner, and any stray
  third-party ``print`` land on disk even when the process dies before any
  handler could run. A raw fd bypasses ``logging`` entirely, so it cannot
  redact; that is what this module's handler is for.
* **This module** — a redacting, byte-capped ``logging`` handler on the root and
  ``uvicorn.*`` loggers writing to the *same* file. It carries every structured
  line through the same redaction pipeline ``/diagnostics/log-tail`` uses, so
  ``sk-ant-…``-shaped tokens never reach disk. It is the only half that covers
  an ADOPTED sidecar (the desktop app's), whose fds belong to whoever spawned it
  and cannot be re-pointed.

When both halves are live (the CLI case) the console handlers are detached
while the file handler is installed, so each structured line is written exactly
once — and once *redacted*, which a raw stderr copy would not be.

Rotation: the CLI rotates at spawn (see ``errorta_cli.sidecar``); a single
long-lived process caps its own writes here — on crossing the budget it logs one
WARNING and stops writing. Truncating in place is explicitly rejected: the CLI's
fd points at the inode, so a truncate would yield a sparse file and a rename
would orphan the live fd.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from errorta_diagnostics import redact as _redact

# Matches the CLI's ``_LOG_ROTATE_BYTES``: two generations, 16 MB total, forever.
MAX_BYTES = 8 * 1024 * 1024

_CAPPED_NOTICE = (
    "sidecar log budget reached; this process stops writing to the file sink "
    "(the next CLI spawn rotates it)\n"
)


def redact_line(line: str) -> str:
    """Redact one log line with the pipeline ``/diagnostics/log-tail`` uses."""
    redacted, _counts = _redact.apply_pipeline(
        str(line),
        home=os.environ.get("HOME"),
        username=os.environ.get("USER"),
    )
    return redacted


class RedactingSidecarLogHandler(logging.FileHandler):
    """Append redacted, byte-budgeted log lines to ``logs/sidecar.log``."""

    def __init__(self, path: str | Path, *, max_bytes: int = MAX_BYTES) -> None:
        super().__init__(str(path), mode="a", encoding="utf-8")
        self.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        self._max_bytes = max(0, int(max_bytes))
        self._capped = False
        try:
            self._written = os.path.getsize(str(path))
        except OSError:  # pragma: no cover — defensive
            self._written = 0

    @property
    def capped(self) -> bool:
        return self._capped

    def format(self, record: logging.LogRecord) -> str:
        return redact_line(super().format(record))

    def emit(self, record: logging.LogRecord) -> None:
        if self._capped:
            return
        try:
            msg = self.format(record) + self.terminator
        except Exception:  # pragma: no cover — never raise from logging
            self.handleError(record)
            return
        size = len(msg.encode("utf-8", errors="replace"))
        if self._max_bytes and self._written + size > self._max_bytes:
            self._capped = True
            try:
                self.stream.write(_CAPPED_NOTICE)
                self.flush()
            except Exception:  # pragma: no cover — defensive
                pass
            return
        try:
            self.stream.write(msg)
            self.flush()
        except Exception:  # pragma: no cover — never raise from logging
            self.handleError(record)
            return
        self._written += size


def _is_console_handler(handler: logging.Handler) -> bool:
    """True for a handler writing to this process's stdout/stderr.

    ``logging.FileHandler`` subclasses ``StreamHandler``, so the stream identity
    check (not ``isinstance``) is what keeps the ``ERRORTA_LOG_FILE`` sink and
    our own handler out of the detach set. The in-memory ``LogBuffer`` handler is
    a plain ``logging.Handler`` and never matches.
    """
    if not isinstance(handler, logging.StreamHandler):
        return False
    return getattr(handler, "stream", None) in (sys.stdout, sys.stderr)


def install(
    loggers: list[logging.Logger],
    path: str | Path,
    *,
    max_bytes: int = MAX_BYTES,
    detach_console: bool = False,
) -> tuple[RedactingSidecarLogHandler, list[tuple[logging.Logger, logging.Handler]]]:
    """Attach the file sink to ``loggers``; return it plus any detached handlers.

    ``detach_console`` is the CLI-spawned case: our stdout/stderr ARE this file,
    so leaving the console handlers attached would write every line twice — and
    the stderr copy would be UNREDACTED, which is the whole thing this sink
    exists to prevent. Detaching happens only after the handler opened
    successfully, so a failure never leaves the process with no log at all.
    """
    handler = RedactingSidecarLogHandler(path, max_bytes=max_bytes)
    for logger in loggers:
        logger.addHandler(handler)
    detached: list[tuple[logging.Logger, logging.Handler]] = []
    if detach_console:
        for logger in loggers:
            for existing in list(logger.handlers):
                if existing is handler or not _is_console_handler(existing):
                    continue
                logger.removeHandler(existing)
                detached.append((logger, existing))
    return handler, detached


def uninstall(
    loggers: list[logging.Logger],
    handler: RedactingSidecarLogHandler | None,
    detached: list[tuple[logging.Logger, logging.Handler]],
) -> None:
    """Undo :func:`install` (shutdown path). Best-effort, never raises."""
    for logger, existing in detached:
        if existing not in logger.handlers:
            logger.addHandler(existing)
    detached.clear()
    if handler is None:
        return
    for logger in loggers:
        if handler in logger.handlers:
            logger.removeHandler(handler)
    try:
        handler.close()
    except Exception:  # pragma: no cover — defensive
        pass


__all__ = [
    "MAX_BYTES",
    "RedactingSidecarLogHandler",
    "install",
    "redact_line",
    "uninstall",
]
