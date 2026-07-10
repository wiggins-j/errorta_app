"""F010 — USB export router.

Two endpoints:

* ``POST /export/plan`` — pure planning, returns counts + dest root, no copy.
* ``POST /export/run``  — SSE stream of copy + verify progress, ending in a
  ``done`` event (or a final ``error`` event on integrity failure).

The router is mounted at ``/export`` and lives alongside the diagnostics
router in ``errorta_app.server``. Filesystem reads are confined to the
existing on-disk corpus files; the plan endpoint never writes anything.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Iterator, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from errorta_export import (
    ChecksumMismatchError,
    CorpusCollisionError,
    ExportIntegrityError,
    ManifestMissingError,
    UnsafeMemberError,
    copy_with_progress,
    import_export_bundle,
    planner,
    verify_checksums,
    write_export_manifest,
)
from errorta_export.safe_path import UnsafePathError

from ._residency_proxy import refuse_local_dataplane_if_remote


router = APIRouter(prefix="/export", tags=["export"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ExportPlanRequest(BaseModel):
    target_dir: str
    corpora_list: list[str] = Field(default_factory=list)
    include_models: bool = False


class ExportPlanResponse(BaseModel):
    files_count: int
    total_size_bytes: int
    corpora: list[str]
    dest_root: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode("utf-8")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/plan", response_model=ExportPlanResponse)
def plan_export(body: ExportPlanRequest) -> ExportPlanResponse:
    """Plan an export without touching the destination filesystem."""
    target = Path(body.target_dir)
    plan = planner(
        target_dir=target,
        corpora_list=body.corpora_list,
        include_models=body.include_models,
    )
    dest_root = target / "Errorta" / "corpora"
    return ExportPlanResponse(
        files_count=len(plan.files),
        total_size_bytes=plan.total_size_bytes,
        corpora=list(plan.corpora_included),
        dest_root=str(dest_root),
    )


@router.post("/run")
def run_export(body: ExportPlanRequest) -> StreamingResponse:
    """Stream copy + verify progress over SSE.

    Frames (each separated by a blank line):

    * ``event: hello\\ndata: {}\\n\\n``
    * ``data: {"event":"phase","phase":"copying"}\\n\\n``
    * one ``data: {"event":"file", ...}`` per progress callback
    * ``data: {"event":"phase","phase":"verifying"}\\n\\n``
    * final ``data: {"event":"done", "summary": {...}}\\n\\n``
    * or ``data: {"event":"error", "error": "..."}\\n\\n`` on integrity failure
    """
    target_dir = Path(body.target_dir)
    corpora_list = list(body.corpora_list)
    include_models = body.include_models

    def gen() -> Iterator[bytes]:
        # Plan up front so any planner failure surfaces as an SSE error rather
        # than a 500 response — the frontend only watches the SSE stream.
        try:
            plan = planner(
                target_dir=target_dir,
                corpora_list=corpora_list,
                include_models=include_models,
            )
        except Exception as exc:  # NotImplementedError, FileNotFoundError, ...
            yield b"event: hello\ndata: {}\n\n"
            yield _sse({"event": "error", "error": str(exc)})
            return

        total_bytes = int(plan.total_size_bytes or 0)
        bytes_done_total = 0
        # Track high-water bytes per file so we can recompute the rolling
        # total when the chunked copy callback emits incremental updates.
        per_file_progress: dict[int, int] = {}

        # ---- hello + phase: copying
        yield b"event: hello\ndata: {}\n\n"
        yield _sse({"event": "phase", "phase": "copying"})

        # Queue of frames produced by the progress callback. We append from
        # the synchronous callback and drain after copy_with_progress returns.
        frames: list[bytes] = []

        def cb(file_idx: int, bytes_done: int, size_bytes: int) -> None:
            nonlocal bytes_done_total
            prev = per_file_progress.get(file_idx, 0)
            if bytes_done > prev:
                bytes_done_total += bytes_done - prev
                per_file_progress[file_idx] = bytes_done
            ef = plan.files[file_idx] if 0 <= file_idx < len(plan.files) else None
            frames.append(
                _sse(
                    {
                        "event": "file",
                        "file_index": file_idx,
                        "file_path": str(ef.dest_path) if ef is not None else None,
                        "bytes_done": bytes_done_total,
                        "bytes_total": total_bytes,
                        "size_bytes": size_bytes,
                    }
                )
            )

        copy_error: Optional[str] = None
        result = None
        try:
            result = copy_with_progress(plan, progress_cb=cb)
        except ExportIntegrityError as exc:
            copy_error = str(exc)

        # Drain any frames emitted by the callback (works even if copy raised
        # mid-stream — partial progress is still surfaced to the client).
        for f in frames:
            yield f

        if copy_error is not None:
            yield _sse({"event": "error", "error": copy_error})
            return

        # ---- phase: verifying
        yield _sse({"event": "phase", "phase": "verifying"})

        manifest_path: Optional[Path] = None
        try:
            manifest_path = write_export_manifest(target_dir, plan)
            verify_results = verify_checksums(manifest_path, target_dir)
            failed = [rel for rel, ok in verify_results.items() if not ok]
            if failed:
                raise ExportIntegrityError(
                    Path(failed[0]),
                    expected_sha="(recorded)",
                    actual_sha="(mismatch)",
                )
        except ExportIntegrityError as exc:
            yield _sse({"event": "error", "error": str(exc)})
            return
        except Exception as exc:  # pragma: no cover — defensive
            yield _sse({"event": "error", "error": str(exc)})
            return

        summary = {
            "files_copied": result.files_copied if result is not None else 0,
            "bytes_written": result.bytes_written if result is not None else 0,
            "duration_s": result.duration_s if result is not None else 0.0,
            "manifest_path": str(manifest_path) if manifest_path is not None else None,
        }
        yield _sse({"event": "done", "summary": summary})

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# /export/import — multipart upload of an Errorta export tarball.
# ---------------------------------------------------------------------------


@router.post("/import")
def import_bundle(
    tarball: UploadFile = File(...),
    rename_corpora: Optional[str] = Form(default=None),  # reserved, accepted but unused for now
) -> dict:
    """Accept an Errorta export tarball and unpack it into ``~/.errorta/corpora/``.

    Returns the import summary (corpora_imported, files_copied, total_bytes).

    HTTP error mapping:
        409 — corpus name collision; ``detail.conflicting_corpora`` lists names.
        422 — manifest entry sha mismatched the verified payload.
        400 — manifest missing/invalid or unsafe tar member encountered.
    """
    refuse_local_dataplane_if_remote("/export/import")
    # Stream the upload to a real on-disk temp file so the tarfile module can
    # rewind for the safety pass + SHA verification pass.
    suffix = Path(tarball.filename or "bundle.tar.gz").suffix or ".tar.gz"
    tmp = tempfile.NamedTemporaryFile(
        prefix="errorta-import-upload-", suffix=suffix, delete=False
    )
    tmp_path = Path(tmp.name)
    try:
        with tmp:
            shutil.copyfileobj(tarball.file, tmp)
        try:
            result = import_export_bundle(tmp_path)
        except CorpusCollisionError as err:
            raise HTTPException(
                status_code=409,
                detail={"conflicting_corpora": err.conflicting_corpora},
            ) from err
        except ChecksumMismatchError as err:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "sha256 mismatch",
                    "path": err.path,
                    "expected": err.expected,
                    "actual": err.actual,
                },
            ) from err
        except UnsafePathError as err:
            # F086: a crafted manifest key tried to escape the staging/target
            # root. Return the offending KEY only — never a resolved path or the
            # hash of an out-of-tree file (that would be a disclosure oracle).
            raise HTTPException(
                status_code=400,
                detail={"code": err.code, "key": err.key},
            ) from err
        except (ManifestMissingError, UnsafeMemberError) as err:
            raise HTTPException(status_code=400, detail=str(err)) from err
        return asdict(result)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
