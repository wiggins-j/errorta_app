"""Ingestion pipeline: extract → chunk → embed → manifest update.

AIAR's `aiar.rag.ingest` is the real vector-store backend. v0.1 imports it
lazily; if AIAR isn't installed (test environments), the pipeline still runs
end-to-end and updates the manifest, simply skipping the embed step.
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from errorta_extract import ExtractError
from errorta_extract.registry import get_extractor

from . import corpus_dir
from .manifest import FileEntry, update_status

# ---- in-process event bus (SSE source) ----------------------------------
_subscribers: list[queue.Queue[str]] = []
_sub_lock = threading.Lock()


def subscribe() -> queue.Queue[str]:
    q: queue.Queue[str] = queue.Queue(maxsize=512)
    with _sub_lock:
        _subscribers.append(q)
    return q


def unsubscribe(q: queue.Queue[str]) -> None:
    with _sub_lock:
        if q in _subscribers:
            _subscribers.remove(q)


def publish(event: dict[str, Any]) -> None:
    payload = json.dumps(event)
    with _sub_lock:
        for q in list(_subscribers):
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass


# ---- background worker --------------------------------------------------
_worker_q: "queue.Queue[tuple[str, str]]" = queue.Queue()
_worker_started = False
_worker_lock = threading.Lock()


def _ensure_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        t = threading.Thread(target=_worker_loop, name="errorta-ingest", daemon=True)
        t.start()
        _worker_started = True


def _worker_loop() -> None:
    while True:
        try:
            corpus_name, file_id = _worker_q.get()
        except Exception:
            continue
        try:
            _process(corpus_name, file_id)
        except Exception as e:  # pragma: no cover
            _emit(corpus_name, file_id, status="failed", error=f"worker error: {e}")


def enqueue(corpus_name: str, file_id: str) -> None:
    _ensure_worker()
    _worker_q.put((corpus_name, file_id))


# ---- processing ---------------------------------------------------------
def _emit(
    corpus_name: str,
    file_id: str,
    **changes: Any,
) -> Optional[FileEntry]:
    e = update_status(corpus_name, file_id, **changes)
    if e is not None:
        publish(
            {
                "type": "file.status",
                "corpus": corpus_name,
                "file_id": file_id,
                "status": e.status,
                "error": e.error,
                "chunk_count": e.chunk_count,
                "token_count": e.token_count,
                "progress": e.progress,
            }
        )
    return e


def _count_tokens(text: str) -> int:
    # v0.1: cheap word-count approximation. AIAR's real tokenizer is used at
    # embed time but we don't have it on this side; approximate is fine for
    # the stats footer.
    return max(1, len(text.split()))


def _process(corpus_name: str, file_id: str) -> None:
    from .manifest import load_manifest

    entry = load_manifest(corpus_name).get(file_id)
    if entry is None:
        return
    copied = Path(entry.copied_path)
    ext = Path(entry.original_path).suffix.lower()

    _emit(corpus_name, file_id, status="extracting", progress=0.1, error="")

    try:
        extractor = get_extractor(ext)
        chunks = extractor(copied)
    except ExtractError as e:
        _emit(corpus_name, file_id, status="failed", error=str(e), progress=0.0)
        return
    except Exception as e:
        _emit(corpus_name, file_id, status="failed", error=f"extraction error: {e}", progress=0.0)
        return

    _emit(corpus_name, file_id, status="chunking", progress=0.4)

    token_count = sum(_count_tokens(c["text"]) for c in chunks)
    chunk_ids = [f"{file_id}:{i}" for i in range(len(chunks))]

    _emit(corpus_name, file_id, status="embedding", progress=0.7)

    # Hand to AIAR if available. v0.1 tolerates AIAR-missing for test runs, but
    # installed AIAR 0.1.x exposes store.add rather than ingest.ingest_chunks.
    _ingest_chunks_into_aiar(corpus_name, file_id, chunk_ids, chunks)

    _emit(
        corpus_name,
        file_id,
        status="ready",
        chunk_count=len(chunks),
        chunk_ids=chunk_ids,
        token_count=token_count,
        ingested_at=datetime.now(timezone.utc).isoformat(),
        progress=1.0,
        error="",
    )


def _ingest_chunks_into_aiar(
    corpus_name: str,
    file_id: str,
    chunk_ids: list[str],
    chunks: list[dict[str, Any]],
) -> None:
    """Best-effort bridge from Errorta chunks into the installed AIAR store."""
    try:
        import aiar.rag.ingest as aiar_ingest  # type: ignore

        if hasattr(aiar_ingest, "ingest_chunks"):
            aiar_ingest.ingest_chunks(  # type: ignore[attr-defined]
                corpus=corpus_name,
                file_id=file_id,
                chunks=[
                    {
                        "id": cid,
                        "text": c["text"],
                        "metadata": {**c["meta"], "file_id": file_id},
                    }
                    for cid, c in zip(chunk_ids, chunks)
                ],
            )
            return

        _ingest_chunks_via_aiar_store(corpus_name, file_id, chunk_ids, chunks)
    except Exception:
        # Non-fatal in v0.1: manifest still records chunks; embedding can be
        # retried on reingest once AIAR's API is available/configured.
        pass


def _ingest_chunks_via_aiar_store(
    corpus_name: str,
    file_id: str,
    chunk_ids: list[str],
    chunks: list[dict[str, Any]],
) -> None:
    try:
        import importlib

        aiar_store = importlib.import_module("aiar.rag.store")
        Chunk = importlib.import_module("aiar.rag.ingest").Chunk
    except Exception:
        return

    if not hasattr(aiar_store, "add") or not hasattr(aiar_store, "create_instance"):
        return

    aiar_store.create_instance(corpus_name, display_name=corpus_name)
    aiar_chunks = [
        Chunk(
            source=f"{corpus_name}/{file_id}",
            title=str(c.get("meta", {}).get("title") or file_id),
            chunk_index=i,
            text=str(c["text"]),
            category=corpus_name,
            metadata={**c.get("meta", {}), "file_id": file_id, "chunk_id": cid},
        )
        for i, (cid, c) in enumerate(zip(chunk_ids, chunks))
    ]
    aiar_store.add(aiar_chunks, instance=corpus_name)
    if hasattr(aiar_store, "publish_instance"):
        aiar_store.publish_instance(corpus_name)


# ---- delete -------------------------------------------------------------
def evict_chunks(corpus_name: str, file_id: str, chunk_ids: list[str]) -> None:
    """Best-effort: remove this file's chunks from AIAR's vector store."""
    if not chunk_ids:
        return
    try:
        import aiar.rag.ingest as aiar_ingest  # type: ignore

        if hasattr(aiar_ingest, "evict_chunks"):
            aiar_ingest.evict_chunks(corpus=corpus_name, chunk_ids=chunk_ids)  # type: ignore[attr-defined]
    except Exception:
        pass


# ---- file ID + path helpers --------------------------------------------
def new_file_id() -> str:
    return uuid.uuid4().hex


def copied_path_for(corpus_name: str, original_name: str) -> Path:
    """Resolve a non-clobbering path under corpus files/. Adds -N suffix on collision."""
    base = corpus_dir(corpus_name) / "files"
    candidate = base / original_name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    i = 1
    while True:
        candidate = base / f"{stem}-{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


async def event_stream():
    """Async generator yielding SSE-formatted strings."""
    q = subscribe()
    try:
        # Initial hello so clients confirm connection.
        yield "event: hello\ndata: {}\n\n"
        loop = asyncio.get_event_loop()
        while True:
            payload = await loop.run_in_executor(None, q.get)
            yield f"data: {payload}\n\n"
    finally:
        unsubscribe(q)
