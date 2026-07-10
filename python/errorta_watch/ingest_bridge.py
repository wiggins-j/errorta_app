"""Bridge from the F005 folder poller into the F004 ingest pipeline.

The poller calls ``ingest_hook(corpus, path)`` and expects a dict of fields
that get merged into the ``ManifestEntry`` for the watcher's own bookkeeping.
This bridge does double duty:

  1. Hash the file and reserve a F004 manifest entry (so the corpus' Files UI
     immediately sees the new file as "queued").
  2. Copy the file into the corpus' ``files/`` directory and enqueue the F004
     pipeline worker, which emits the same SSE events drag-and-drop uploads do.
  3. Return mtime / size / sha256 so the watcher's manifest stays consistent.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from errorta_corpus.manifest import FileEntry, reserve_or_get_duplicate
from errorta_corpus.pipeline import copied_path_for, enqueue, new_file_id


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def ingest_via_pipeline(corpus: str, path: str) -> dict:
    """Watcher ingest hook: copy file into corpus, enqueue F004 pipeline.

    Returns the dict the poller merges into its own ``ManifestEntry``.
    """
    try:
        st = os.stat(path)
    except OSError:
        return {}

    digest = _sha256(path)
    if not digest:
        return {"mtime": st.st_mtime, "size": st.st_size}

    src = Path(path)
    ext = src.suffix.lower()
    target = copied_path_for(corpus, src.name)
    try:
        with src.open("rb") as inp, target.open("wb") as out:
            for chunk in iter(lambda: inp.read(1 << 20), b""):
                out.write(chunk)
    except OSError:
        return {
            "mtime": st.st_mtime,
            "size": st.st_size,
            "sha256": digest,
        }

    file_id = new_file_id()
    new_entry = FileEntry(
        file_id=file_id,
        original_path=str(src),
        copied_path=str(target),
        sha256=digest,
        size_bytes=st.st_size,
        mime_ext=ext,
        status="queued",
    )
    inserted, prior = reserve_or_get_duplicate(
        corpus, digest, new_entry, overwrite=False
    )
    if inserted is None:
        # Duplicate of an already-tracked file: drop the freshly written copy.
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        return {
            "mtime": st.st_mtime,
            "size": st.st_size,
            "sha256": digest,
            "file_id": prior.file_id if prior else "",
        }

    enqueue(corpus, file_id)
    return {
        "mtime": st.st_mtime,
        "size": st.st_size,
        "sha256": digest,
        "file_id": file_id,
        "chunk_ids": [],
        "source_missing": False,
    }
