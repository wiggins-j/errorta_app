"""F007 — welcome corpus router.

Endpoints:
  * ``GET /welcome/options`` — list available welcome corpora (v0.1: one).
  * ``POST /welcome/install`` — download + verify + ingest in one shot.
  * ``GET  /welcome/status``  — current phase / progress / ETA.
  * ``GET  /welcome/download`` — download-only (verification, no ingest).
  * ``POST /welcome/ingest``   — ingest an already-downloaded tarball.

The pinned SHA-256 hash lives at ``errorta_welcome/pinned_hash.json``. We
refuse to ingest anything that does not match.
"""
from __future__ import annotations

import asyncio
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from errorta_welcome import downloader as dl
from errorta_welcome import ingest_bridge

router = APIRouter(prefix="/welcome", tags=["welcome"])

SUGGESTED_PROMPT = "What does Errorta do, and how do I add my own files?"

WELCOME_OPTION_ID = "welcome-to-errorta"


# ---------------------------------------------------------------------------
# Shared in-memory progress state
# ---------------------------------------------------------------------------

@dataclass
class _Progress:
    phase: str = "idle"  # idle | downloading | verifying | extracting | ingesting | done | error
    progress: float = 0.0  # 0..1
    bytes_downloaded: int = 0
    bytes_total: Optional[int] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: Optional[str] = None
    corpus_name: Optional[str] = None
    suggested_prompt: Optional[str] = None

    def eta_seconds(self) -> Optional[float]:
        if self.phase not in {"downloading", "verifying", "extracting", "ingesting"}:
            return None
        if not self.started_at or self.progress <= 0:
            return None
        elapsed = time.monotonic() - self.started_at
        if elapsed <= 0:
            return None
        total = elapsed / self.progress
        return max(0.0, total - elapsed)


_state = _Progress()
_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class WelcomeOption(BaseModel):
    id: str
    name: str
    description: str
    source_url: str
    license: str
    fully_deletable: bool
    approx_size_mb: float


class OptionsResponse(BaseModel):
    options: list[WelcomeOption]


class StatusResponse(BaseModel):
    phase: str
    progress: float
    bytes_downloaded: int
    bytes_total: Optional[int]
    eta_seconds: Optional[float]
    corpus_name: Optional[str]
    suggested_prompt: Optional[str]
    error: Optional[str]


class InstallResponse(BaseModel):
    corpus_name: str
    suggested_prompt: str
    files_ingested: int
    bytes_downloaded: int
    sha256: str
    f004_invoked: bool
    f004_error: Optional[str] = None


class IngestRequest(BaseModel):
    tarball_path: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_option() -> WelcomeOption:
    pin = dl.load_pinned_hash()
    return WelcomeOption(
        id=WELCOME_OPTION_ID,
        name="Welcome to Errorta",
        description=(
            "Errorta's own documentation, asked of itself. Small download "
            "(under 5 MB). Fully deletable from the standard Corpora UI."
        ),
        source_url=pin.source_url,
        license="MIT",
        fully_deletable=True,
        approx_size_mb=round(pin.max_bytes / (1024 * 1024), 2),
    )


def _reset_state() -> None:
    global _state
    _state = _Progress(phase="downloading", started_at=time.monotonic())


def _set_progress(bytes_downloaded: int, bytes_total: Optional[int]) -> None:
    _state.bytes_downloaded = bytes_downloaded
    _state.bytes_total = bytes_total
    if bytes_total and bytes_total > 0:
        # Download accounts for ~70% of the progress bar; ingest is the rest.
        _state.progress = min(0.7, 0.7 * (bytes_downloaded / bytes_total))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/options", response_model=OptionsResponse)
def list_options() -> OptionsResponse:
    """List available welcome corpora. v0.1 ships exactly one."""
    from errorta_app.routes._residency_proxy import proxy_json_if_remote

    proxied = proxy_json_if_remote("GET", "/welcome/options")
    if proxied is not None:
        return OptionsResponse.model_validate(proxied)
    return OptionsResponse(options=[_build_option()])


@router.get("/status", response_model=StatusResponse)
def status() -> StatusResponse:
    from errorta_app.routes._residency_proxy import proxy_json_if_remote

    proxied = proxy_json_if_remote("GET", "/welcome/status")
    if proxied is not None:
        return StatusResponse.model_validate(proxied)
    return StatusResponse(
        phase=_state.phase,
        progress=round(_state.progress, 4),
        bytes_downloaded=_state.bytes_downloaded,
        bytes_total=_state.bytes_total,
        eta_seconds=_state.eta_seconds(),
        corpus_name=_state.corpus_name,
        suggested_prompt=_state.suggested_prompt,
        error=_state.error,
    )


@router.get("/download")
async def download_only() -> dict:
    """Download + SHA-256 verify, no ingest. Returns the temp file path."""
    from errorta_app.routes._residency_proxy import proxy_json_if_remote

    proxied = proxy_json_if_remote("GET", "/welcome/download", timeout_s=600)
    if proxied is not None:
        return proxied
    if _lock.locked():
        raise HTTPException(status_code=409, detail="welcome install already running")
    async with _lock:
        _reset_state()
        try:
            tmpdir = Path(tempfile.mkdtemp(prefix="errorta-welcome-"))
            tarball = tmpdir / "welcome-corpus.tar.gz"
            result = await dl.stream_download(tarball, progress_cb=_set_progress)
            _state.phase = "verifying"
            _state.progress = 0.72
            dl.verify_sha256(result)
            _state.phase = "done"
            _state.progress = 1.0
            _state.finished_at = time.monotonic()
            return {
                "tarball_path": str(tarball),
                "bytes": result.bytes_downloaded,
                "sha256": result.sha256,
            }
        except dl.HashMismatchError as exc:
            _state.phase = "error"
            _state.error = str(exc)
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            _state.phase = "error"
            _state.error = repr(exc)
            raise HTTPException(status_code=502, detail=repr(exc)) from exc


@router.post("/ingest", response_model=InstallResponse)
async def ingest(req: IngestRequest) -> InstallResponse:
    """Extract a verified tarball and hand it to the F004 ingest pipeline."""
    from errorta_app.routes._residency_proxy import proxy_json_if_remote

    proxied = proxy_json_if_remote(
        "POST",
        "/welcome/ingest",
        json_body=req.model_dump(),
        timeout_s=600,
    )
    if proxied is not None:
        return InstallResponse.model_validate(proxied)
    tarball = Path(req.tarball_path)
    if not tarball.is_file():
        raise HTTPException(status_code=404, detail=f"tarball not found: {tarball}")
    if _lock.locked():
        raise HTTPException(status_code=409, detail="welcome install already running")
    async with _lock:
        try:
            extract_root = Path(tempfile.mkdtemp(prefix="errorta-welcome-extract-"))
            _state.phase = "extracting"
            _state.progress = 0.85
            extracted = ingest_bridge.extract_tarball(tarball, extract_root)
            _state.phase = "ingesting"
            _state.progress = 0.92
            result = ingest_bridge.ingest_extracted(extracted)
        except Exception as exc:
            _state.phase = "error"
            _state.error = repr(exc)
            raise HTTPException(status_code=500, detail=repr(exc)) from exc

        _state.phase = "done"
        _state.progress = 1.0
        _state.corpus_name = result.corpus_name
        _state.suggested_prompt = SUGGESTED_PROMPT
        _state.finished_at = time.monotonic()

        return InstallResponse(
            corpus_name=result.corpus_name,
            suggested_prompt=SUGGESTED_PROMPT,
            files_ingested=len(result.files),
            bytes_downloaded=_state.bytes_downloaded,
            sha256=dl.load_pinned_hash().sha256,
            f004_invoked=result.f004_invoked,
            f004_error=result.f004_error,
        )


@router.post("/install", response_model=InstallResponse)
async def install() -> InstallResponse:
    """One-shot: download + verify + extract + ingest. Used by the empty-state button."""
    from errorta_app.routes._residency_proxy import proxy_json_if_remote

    proxied = proxy_json_if_remote("POST", "/welcome/install", timeout_s=600)
    if proxied is not None:
        return InstallResponse.model_validate(proxied)
    if _lock.locked():
        raise HTTPException(status_code=409, detail="welcome install already running")
    async with _lock:
        _reset_state()
        tmpdir = Path(tempfile.mkdtemp(prefix="errorta-welcome-"))
        tarball = tmpdir / "welcome-corpus.tar.gz"
        try:
            download = await dl.stream_download(tarball, progress_cb=_set_progress)
            _state.phase = "verifying"
            _state.progress = 0.72
            dl.verify_sha256(download)
            _state.phase = "extracting"
            _state.progress = 0.82
            extract_root = Path(tempfile.mkdtemp(prefix="errorta-welcome-extract-"))
            extracted = ingest_bridge.extract_tarball(tarball, extract_root)
            _state.phase = "ingesting"
            _state.progress = 0.92
            ingest_result = ingest_bridge.ingest_extracted(extracted)
        except dl.HashMismatchError as exc:
            _state.phase = "error"
            _state.error = str(exc)
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except dl.DownloadTooLargeError as exc:
            _state.phase = "error"
            _state.error = str(exc)
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except Exception as exc:
            _state.phase = "error"
            _state.error = repr(exc)
            raise HTTPException(status_code=502, detail=repr(exc)) from exc

        _state.phase = "done"
        _state.progress = 1.0
        _state.corpus_name = ingest_result.corpus_name
        _state.suggested_prompt = SUGGESTED_PROMPT
        _state.finished_at = time.monotonic()

        return InstallResponse(
            corpus_name=ingest_result.corpus_name,
            suggested_prompt=SUGGESTED_PROMPT,
            files_ingested=len(ingest_result.files),
            bytes_downloaded=download.bytes_downloaded,
            sha256=download.sha256,
            f004_invoked=ingest_result.f004_invoked,
            f004_error=ingest_result.f004_error,
        )
