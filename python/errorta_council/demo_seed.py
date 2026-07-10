"""F031-DEMO-CORPUS — Council demo corpus seed helper.

Wraps the existing F007 welcome-corpus install path so the Council demo
affordance can ensure the welcome corpus is on disk before posting a room
with ``corpus_ids=["welcome"]``.

Invariants honored:
- Reuses F004's ``errorta_corpus.directory_ingest`` via F007's
  ``errorta_welcome.ingest_bridge`` (no new ingest path).
- Never imports or initializes a provider SDK
  (no ``anthropic``, no ``openai``, no F030 gateway init).
- Writes only under ``$ERRORTA_HOME`` via the existing
  ``errorta_app.paths.corpora_dir`` helper used by
  ``errorta_corpus.directory_ingest``.
- Fail-loudly on failure: returns ``status="failed"`` with a structured
  error string instead of silently degrading.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional, TypedDict


class DemoSeedResult(TypedDict):
    """Return shape of :func:`ensure_demo_corpus`.

    - ``corpus_id``: the F004 corpus name once present (``"welcome"``) or
      ``None`` if the seed failed.
    - ``status``: one of ``"ready"`` (newly seeded), ``"reused"`` (already
      on disk), ``"failed"`` (download/verify/ingest error).
    - ``error``: structured one-line error string on failure, ``None``
      otherwise.
    """

    corpus_id: Optional[str]
    status: str  # "ready" | "reused" | "failed"
    error: Optional[str]


CORPUS_NAME = "welcome"


def _structured(exc: BaseException) -> str:
    """Render a short, structured error suitable for the UI banner."""
    return f"{type(exc).__name__}: {exc}"


def _has_welcome_corpus() -> bool:
    """True if the welcome corpus directory already exists under ERRORTA_HOME."""
    # Local import keeps ``errorta_app`` out of the import graph until needed,
    # mirroring ``errorta_corpus.__init__.corpus_root``.
    from errorta_app.paths import corpora_dir

    welcome_dir = corpora_dir() / CORPUS_NAME
    # A welcome dir with at least the F004 ``files/`` subdir is "present".
    return welcome_dir.is_dir() and (welcome_dir / "files").is_dir()


def ensure_demo_corpus(
    *,
    downloader: Optional[Any] = None,
    ingest_bridge: Optional[Any] = None,
    has_welcome: Optional[Callable[[], bool]] = None,
) -> DemoSeedResult:
    """Ensure the F007 welcome corpus is on disk under ``$ERRORTA_HOME``.

    Idempotent: if already present, returns ``status="reused"`` without
    invoking the downloader or ingest bridge.

    Injection points (default to the production modules):
    - ``downloader``: module with ``stream_download(dest, *, pin=None)`` +
      ``verify_sha256(result, pin=None)`` + ``load_pinned_hash()`` +
      ``HashMismatchError`` + ``DownloadTooLargeError``.
    - ``ingest_bridge``: module with ``extract_tarball(tarball, dest_root)``
      + ``ingest_extracted(extracted_root)`` returning an ``IngestResult``
      with ``f004_error``.
    - ``has_welcome``: predicate, defaults to disk check under
      ``$ERRORTA_HOME``.
    """
    if has_welcome is None:
        has_welcome = _has_welcome_corpus

    if has_welcome():
        return DemoSeedResult(corpus_id=CORPUS_NAME, status="reused", error=None)

    if downloader is None:
        from errorta_welcome import downloader as downloader_default

        downloader = downloader_default
    if ingest_bridge is None:
        from errorta_welcome import ingest_bridge as ingest_bridge_default

        ingest_bridge = ingest_bridge_default

    try:
        tmpdir = Path(tempfile.mkdtemp(prefix="errorta-demo-corpus-"))
        tarball = tmpdir / "welcome-corpus.tar.gz"
        # ``stream_download`` is async; run synchronously here so the helper
        # has a simple sync contract for the route/test layer. The caller
        # (FastAPI route) already has its own event loop and reuses the
        # underlying ``POST /welcome/install`` async path; this sync helper
        # exists for tests and for any future Council-internal sync caller.
        download_result = asyncio.run(downloader.stream_download(tarball))
        downloader.verify_sha256(download_result)
        extract_root = Path(tempfile.mkdtemp(prefix="errorta-demo-extract-"))
        extracted = ingest_bridge.extract_tarball(tarball, extract_root)
        ingest_result = ingest_bridge.ingest_extracted(extracted)
        if getattr(ingest_result, "f004_error", None):
            return DemoSeedResult(
                corpus_id=None,
                status="failed",
                error=str(ingest_result.f004_error),
            )
        return DemoSeedResult(
            corpus_id=CORPUS_NAME, status="ready", error=None
        )
    except Exception as exc:  # noqa: BLE001 — fail-loudly, surface as structured error
        return DemoSeedResult(
            corpus_id=None, status="failed", error=_structured(exc)
        )
