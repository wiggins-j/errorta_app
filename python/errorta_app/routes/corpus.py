"""F004 — drag-and-drop corpus management router.

Mounted at /corpus by `errorta_app.server`. Paths defined here are therefore
/corpus/... — equivalent to the spec's `/api/corpus/...` family.
"""
from __future__ import annotations

import hashlib
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from errorta_corpus import (
    InvalidCorpusName,
    corpus_dir,
    corpus_root,
    delete_corpus,
    validate_corpus_name,
)
from errorta_corpus.manifest import (
    FileEntry,
    load_manifest,
    remove_entry,
    reserve_or_get_duplicate,
    upsert_entry,
)
from errorta_corpus.pipeline import (
    copied_path_for,
    enqueue,
    event_stream,
    evict_chunks,
    new_file_id,
)
from errorta_corpus.refresh import (
    apply_diff,
    apply_result_to_dict,
    compute_diff,
    diff_from_dict,
    diff_to_dict,
)
from errorta_extract.registry import format_label, supported_extensions

from ._residency_proxy import refuse_local_dataplane_if_remote

router = APIRouter(prefix="/corpus", tags=["corpus"])

LARGE_FILE_BYTES = 100 * 1024 * 1024  # 100 MB

# ---- response models ----------------------------------------------------


class FileOut(BaseModel):
    file_id: str
    original_path: str
    copied_path: str
    sha256: str
    size_bytes: int
    mime_ext: str
    status: str
    error: Optional[str] = None
    chunk_count: int
    chunk_ids: list[str]
    token_count: int
    ingested_at: Optional[str] = None
    progress: float


class FilesListOut(BaseModel):
    corpus: str
    files: list[FileOut]
    stats: dict


class UploadResultItem(BaseModel):
    filename: str
    file_id: Optional[str] = None
    status: str  # accepted | duplicate | rejected | needs_confirm
    reason: Optional[str] = None
    sha256: Optional[str] = None
    size_bytes: Optional[int] = None


class UploadResponse(BaseModel):
    corpus: str
    results: list[UploadResultItem]


# ---- helpers ------------------------------------------------------------


def _require_valid_name(name: str) -> str:
    try:
        return validate_corpus_name(name)
    except InvalidCorpusName as e:
        raise HTTPException(status_code=400, detail=f"invalid corpus name: {e}")


def _entry_to_out(e: FileEntry) -> FileOut:
    return FileOut(**asdict(e))


def _compute_stats(name: str, files: dict[str, FileEntry]) -> dict:
    files_dir = corpus_dir(name) / "files"
    disk_bytes = 0
    if files_dir.exists():
        for p in files_dir.iterdir():
            try:
                disk_bytes += p.stat().st_size
            except OSError:
                pass
    return {
        "file_count": len(files),
        "chunk_count": sum(e.chunk_count for e in files.values()),
        "token_count": sum(e.token_count for e in files.values()),
        "disk_bytes": disk_bytes,
    }


def _require_local_catalog(endpoint: str, *, capability: str) -> None:
    """Reject local corpus file routes while the catalog is non-local.

    F115 makes remote AIAR corpora summary-only until AIAR exposes document
    listing or remote mutation capabilities. Without this guard a direct
    ``/corpus/{remote}/files`` call can create/read an empty local manifest and
    make a healthy remote corpus look empty.
    """
    refuse_local_dataplane_if_remote(endpoint)
    try:
        from errorta_project_grounding.remote_adapter import active_remote_adapter

        remote = active_remote_adapter()
    except Exception:  # pragma: no cover - defensive; local route stays usable
        remote = None
    if remote is None:
        return
    raise HTTPException(
        status_code=409,
        detail={
            "code": "unsupported_corpus_capability",
            "capability": capability,
            "message": (
                "Local corpus file routes are disabled while the corpus catalog "
                "is backed by remote AIAR."
            ),
        },
    )


# ---- endpoints ----------------------------------------------------------


@router.get("/formats")
def list_formats() -> dict:
    return {
        "extensions": supported_extensions(),
        "large_file_bytes": LARGE_FILE_BYTES,
    }


@router.get("/{name}/files", response_model=FilesListOut)
def list_files(name: str) -> FilesListOut:
    _require_local_catalog(f"/corpus/{name}/files", capability="list_files")
    _require_valid_name(name)
    files = load_manifest(name)
    return FilesListOut(
        corpus=name,
        files=[_entry_to_out(e) for e in files.values()],
        stats=_compute_stats(name, files),
    )


@router.post("/{name}/upload", response_model=UploadResponse)
async def upload_files(
    name: str,
    files: list[UploadFile] = File(...),
    confirm_large: bool = Form(False),
    overwrite_duplicates: bool = Form(False),
) -> UploadResponse:
    _require_local_catalog(f"/corpus/{name}/upload", capability="upload_files")
    _require_valid_name(name)
    if not files:
        raise HTTPException(status_code=400, detail="no files provided")

    results: list[UploadResultItem] = []
    supported = set(supported_extensions())
    files_root = (corpus_dir(name) / "files").resolve()

    for upload in files:
        # Reduce arbitrary client-supplied paths to a basename — prevents
        # `../../etc/passwd`-style uploads from escaping the corpus dir.
        original_name = Path(upload.filename or "unnamed").name or "unnamed"
        ext = Path(original_name).suffix.lower()

        # Read into a temp path so we can hash + size-check.
        target = copied_path_for(name, original_name)
        try:
            resolved_target = target.resolve()
            resolved_target.relative_to(files_root)
        except (OSError, ValueError):
            results.append(
                UploadResultItem(
                    filename=original_name,
                    status="rejected",
                    reason="invalid filename",
                )
            )
            continue
        sha = hashlib.sha256()
        size = 0
        too_big = False
        try:
            with target.open("wb") as out:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > LARGE_FILE_BYTES and not confirm_large:
                        too_big = True
                        break
                    sha.update(chunk)
                    out.write(chunk)
        except Exception as e:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            results.append(
                UploadResultItem(filename=original_name, status="rejected", reason=f"write error: {e}")
            )
            continue
        finally:
            await upload.close()

        if too_big:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            results.append(
                UploadResultItem(
                    filename=original_name,
                    status="needs_confirm",
                    reason=(
                        f"file is larger than {LARGE_FILE_BYTES // (1024 * 1024)} MB; "
                        "re-upload with confirm_large=true to ingest"
                    ),
                    size_bytes=size,
                )
            )
            continue

        if ext not in supported:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            results.append(
                UploadResultItem(
                    filename=original_name,
                    status="rejected",
                    reason=f"unsupported format: {format_label(ext)}",
                    size_bytes=size,
                )
            )
            continue

        digest = sha.hexdigest()
        file_id = new_file_id()
        new_entry = FileEntry(
            file_id=file_id,
            original_path=original_name,
            copied_path=str(target),
            sha256=digest,
            size_bytes=size,
            mime_ext=ext,
            status="queued",
        )
        # Atomic: holds the per-corpus lock across find + insert so two
        # concurrent uploads of the same SHA cannot both win.
        inserted, prior = reserve_or_get_duplicate(
            name, digest, new_entry, overwrite=overwrite_duplicates
        )
        if inserted is None:
            # Duplicate, not overwriting — drop the freshly-written file.
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            assert prior is not None
            results.append(
                UploadResultItem(
                    filename=original_name,
                    file_id=prior.file_id,
                    status="duplicate",
                    reason="this file is already in the corpus (matched by SHA-256)",
                    sha256=digest,
                    size_bytes=size,
                )
            )
            continue

        if prior is not None:
            # Overwrite path: evict the prior entry's chunks + remove its
            # copied file so the corpus doesn't end up with two copies of
            # the same content under different file_ids.
            try:
                evict_chunks(name, prior.file_id, prior.chunk_ids)
            except Exception:
                pass
            try:
                Path(prior.copied_path).unlink(missing_ok=True)
            except OSError:
                pass

        enqueue(name, file_id)
        results.append(
            UploadResultItem(
                filename=original_name,
                file_id=file_id,
                status="accepted",
                sha256=digest,
                size_bytes=size,
            )
        )

    return UploadResponse(corpus=name, results=results)


@router.delete("/{name}/files/{file_id}")
def delete_file(name: str, file_id: str) -> dict:
    _require_local_catalog(
        f"/corpus/{name}/files/{file_id}",
        capability="upload_files",
    )
    _require_valid_name(name)
    entry = remove_entry(name, file_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="file not found")
    # Remove the copied file from disk
    try:
        Path(entry.copied_path).unlink(missing_ok=True)
    except OSError:
        pass
    # Evict chunks from vector store (best-effort)
    evict_chunks(name, file_id, entry.chunk_ids)
    return {"ok": True, "file_id": file_id}


@router.delete("/{name}")
def delete_corpus_endpoint(name: str) -> dict:
    """F114 — delete an entire corpus (manifest + files + chunks).

    404 on an unknown corpus, 400 on an invalid name. Path-safe: the on-disk
    removal resolves under the corpus root and refuses traversal.
    """
    _require_local_catalog(f"/corpus/{name}", capability="upload_files")
    _require_valid_name(name)
    # Existence check without the dir-creating side effect of corpus_dir().
    if not (corpus_root() / name).is_dir():
        raise HTTPException(status_code=404, detail="corpus not found")
    # Best-effort: evict this corpus's chunks from the vector store before the
    # on-disk files go away (mirrors the per-file delete).
    try:
        for entry in load_manifest(name).values():
            evict_chunks(name, entry.file_id, entry.chunk_ids)
    except Exception:
        pass
    delete_corpus(name)
    return {"ok": True, "corpus": name}


@router.post("/{name}/files/{file_id}/reingest")
def reingest_file(name: str, file_id: str) -> dict:
    _require_local_catalog(
        f"/corpus/{name}/files/{file_id}/reingest",
        capability="upload_files",
    )
    _require_valid_name(name)
    files = load_manifest(name)
    entry = files.get(file_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="file not found")
    # Evict prior chunks first.
    evict_chunks(name, file_id, entry.chunk_ids)
    entry.status = "queued"
    entry.error = None
    entry.progress = 0.0
    entry.chunk_count = 0
    entry.chunk_ids = []
    entry.token_count = 0
    upsert_entry(name, entry)
    enqueue(name, file_id)
    return {"ok": True, "file_id": file_id}


@router.post("/{name}/reingest")
def reingest_all(name: str) -> dict:
    _require_local_catalog(f"/corpus/{name}/reingest", capability="upload_files")
    _require_valid_name(name)
    files = load_manifest(name)
    for entry in files.values():
        evict_chunks(name, entry.file_id, entry.chunk_ids)
        entry.status = "queued"
        entry.error = None
        entry.progress = 0.0
        entry.chunk_count = 0
        entry.chunk_ids = []
        entry.token_count = 0
        upsert_entry(name, entry)
        enqueue(name, entry.file_id)
    return {"ok": True, "count": len(files)}


@router.get("/{name}/refresh-preview")
def refresh_preview(name: str, since: Optional[str] = None) -> dict:
    """Preview-only: returns what would change if the corpus were refreshed.

    No mutation, no ingestion. The "apply" path is intentionally deferred to
    a later slice (do not conflate with F015 brief-refresh).
    """
    _require_local_catalog(
        f"/corpus/{name}/refresh-preview",
        capability="refresh_preview",
    )
    _require_valid_name(name)
    # Probe for an existing manifest without creating the corpus dir.
    from errorta_app.paths import errorta_home
    candidate = errorta_home() / "corpora" / name / "manifest.json"
    if not candidate.exists():
        raise HTTPException(status_code=404, detail="corpus not found")
    diff = compute_diff(name, last_snapshot_at=since)
    return {"corpus": name, **diff_to_dict(diff)}


@router.post("/{name}/refresh-apply")
async def refresh_apply(name: str, request: Request) -> dict:
    """Apply a refresh diff to the corpus.

    Accepts an optional JSON body matching the ``diff_to_dict`` payload. If
    the body is omitted (empty / null), the diff is recomputed via
    ``compute_diff()``. Returns 404 if the corpus does not exist, 400 if the
    body is present but malformed.
    """
    _require_local_catalog(
        f"/corpus/{name}/refresh-apply",
        capability="refresh_preview",
    )
    _require_valid_name(name)
    from errorta_app.paths import errorta_home
    candidate = errorta_home() / "corpora" / name / "manifest.json"
    if not candidate.exists():
        raise HTTPException(status_code=404, detail="corpus not found")

    raw = await request.body()
    diff = None
    if raw:
        try:
            import json as _json

            payload = _json.loads(raw)
            if payload is not None and not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="malformed diff body")
            if isinstance(payload, dict) and payload:
                diff = diff_from_dict(payload)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"malformed diff body: {e}")
    if diff is None:
        diff = compute_diff(name)
    result = apply_diff(name, diff)
    return {"corpus": name, **apply_result_to_dict(result)}


@router.get("/events")
async def events() -> StreamingResponse:
    return StreamingResponse(event_stream(), media_type="text/event-stream")
