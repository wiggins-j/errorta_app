"""F-DIST-01 slice 7 — crash breadcrumbs + feedback bundle assembly.

Two flows (spec §10):

1. **Automatic crash breadcrumb** (opt-out extra): on an uncaught exception,
   record a breadcrumb built from **module + line + exception class ONLY** —
   never a raw stack frame, file path, or local value. This is the review
   blocker fix: ``redact.py`` does NOT strip corpus filenames, so a raw
   traceback string would leak document names past redaction. The breadcrumb is
   a compact content-free descriptor (dotted module names + a line number +
   the exception class), enqueued as a ``crash_breadcrumb`` extra that rides
   ``/v1/metrics`` on the next check-in.

2. **Manual feedback bundle**: build the F-INFRA-06 *redacted* diagnostic bundle,
   passing the **live corpus roots** so their path prefixes are stripped. The
   route shows the tester exactly what's in it before anything is sent.

No network here — sending is ``client.py`` (invariant 1).
"""
from __future__ import annotations

import logging
import sys
import threading
import uuid
from types import TracebackType
from typing import Any

from errorta_app.paths import corpora_dir, errorta_home
from errorta_diagnostics.bundle import build_bundle

from . import telemetry

log = logging.getLogger(__name__)


# ---- crash breadcrumb -------------------------------------------------------

def _last_app_frame(tb: TracebackType | None) -> tuple[str | None, int]:
    """The deepest ``errorta*`` frame's dotted module name + line — code
    identifiers only, no file path or locals."""
    module: str | None = None
    line = 0
    while tb is not None:
        name = tb.tb_frame.f_globals.get("__name__", "")
        if isinstance(name, str) and name.startswith("errorta"):
            module, line = name, tb.tb_lineno
        tb = tb.tb_next
    return module, line


def build_crash_breadcrumb(exc: BaseException) -> str:
    """A content-free breadcrumb, e.g. ``ValueError@errorta_council.engine:412``.

    NEVER includes a file path (dotted module names use ``.``, not ``/``), local
    variable values, or a raw traceback string.
    """
    cls = type(exc).__name__
    module, line = _last_app_frame(getattr(exc, "__traceback__", None))
    return f"{cls}@{module}:{line}" if module else cls


def record_crash(exc: BaseException) -> None:
    """Enqueue a crash breadcrumb (no-op unless the gate is on + extras opted in)."""
    try:
        telemetry.record_crash_breadcrumb(build_crash_breadcrumb(exc))
    except Exception:  # pragma: no cover — recording a crash must never re-raise
        pass


def install_crash_hook() -> None:
    """Chain sys/threading excepthooks to record a breadcrumb on any uncaught
    exception, then defer to the previous handler. Called from the lifespan only
    when the gate is on. Idempotent — repeated calls (uvicorn --reload, a test
    constructing the app twice) must not stack hooks and multiply records."""
    if getattr(install_crash_hook, "_done", False):
        return
    install_crash_hook._done = True  # type: ignore[attr-defined]
    prev_sys = sys.excepthook

    def _sys_hook(exc_type, exc, tb):  # type: ignore[no-untyped-def]
        record_crash(exc)
        prev_sys(exc_type, exc, tb)

    sys.excepthook = _sys_hook

    prev_thread = threading.excepthook

    def _thread_hook(args):  # type: ignore[no-untyped-def]
        if args.exc_value is not None:
            record_crash(args.exc_value)
        prev_thread(args)

    threading.excepthook = _thread_hook


# ---- feedback bundle --------------------------------------------------------

def _live_corpus_roots() -> list[str]:
    """The current corpus directories, so ``build_bundle`` strips their full path
    prefixes (corpus id included) from the redacted bundle."""
    roots: list[str] = []
    try:
        base = corpora_dir()
        roots.append(str(base))
        for child in base.iterdir():
            if child.is_dir():
                roots.append(str(child))
    except Exception:  # pragma: no cover — a missing corpora dir is fine
        pass
    return roots


def prepare_feedback_bundle(*, user_note: str, log_buffer: Any = None) -> dict[str, Any]:
    """Build the redacted diagnostic bundle to a temp file and return
    ``{path, sha256, redaction_manifest, files}``. The caller shows this to the
    tester and only sends it on explicit confirm."""
    dest_dir = errorta_home() / "feedback"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{uuid.uuid4().hex}.zip"
    return build_bundle(
        dest,
        user_note=user_note,
        log_buffer=log_buffer,
        corpus_roots=_live_corpus_roots(),
    )
