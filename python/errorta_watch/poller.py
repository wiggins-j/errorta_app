"""Per-corpus polling watcher.

Each ``FolderPoller`` runs in its own thread. Every 60 seconds it scans the
watched folder (walking with ``os.walk``, no symlink-follow), compares against
the manifest in ``WatchState``, and dispatches new / modified / deleted / moved
files.

This module deliberately does the bookkeeping only — the actual extraction +
vector-store calls live in the F004 pipeline (``errorta_extract``), which is
not built yet. For v0.1 we stub the ingest hook so the watcher state machine
is exercisable end-to-end without the heavy pipeline present.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import os
import threading
import time
from typing import Callable, Iterable

from .ignore import is_ignored, is_supported
from .state import ManifestEntry, WatchState, save_state

log = logging.getLogger("errorta.watch.poller")

# 60s per spec acceptance criterion.
DEFAULT_POLL_INTERVAL = 60.0

# F005-PROD: ingest backpressure — bounded retries with exponential backoff.
_INGEST_MAX_ATTEMPTS = 3
_INGEST_BACKOFF_BASE = 2.0  # 2s, 4s, 8s

# Read budget for content hashing — large files use mtime+size only, hashed
# lazily for move detection.
_HASH_READ_CHUNK = 1 << 20  # 1 MiB
_HASH_MAX_BYTES = 8 << 20  # 8 MiB — partial-hash cap for very large files


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _content_hash(path: str) -> str:
    """Return a fast partial sha256 of the file head — enough for move detection.

    A true xxhash would be ideal here (per the brief); we use sha256 of the
    first 8 MiB to avoid adding a runtime dep beyond the stdlib. The hash is
    only used for change/move detection, never for cryptographic purposes.
    """
    h = hashlib.sha256()
    read = 0
    try:
        with open(path, "rb") as fh:
            while read < _HASH_MAX_BYTES:
                chunk = fh.read(_HASH_READ_CHUNK)
                if not chunk:
                    break
                h.update(chunk)
                read += len(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _walk_supported(
    root: str,
    type_filter: Iterable[str],
    extra_ignores: Iterable[str],
) -> list[str]:
    """Return all supported file paths under ``root``, never following symlinks."""
    out: list[str] = []
    if not os.path.isdir(root):
        return out
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Mutate in place to prune ignored / hidden / symlink dirs.
        dirnames[:] = [
            d for d in dirnames
            if not is_ignored(d, extra_ignores)
            and not os.path.islink(os.path.join(dirpath, d))
        ]
        for name in filenames:
            if is_ignored(name, extra_ignores):
                continue
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue
            if not is_supported(full, type_filter):
                continue
            out.append(full)
    return out


# Hook signature: (corpus_name, path) -> ManifestEntry-update fields.
IngestHook = Callable[[str, str], dict]
DeleteHook = Callable[[str, ManifestEntry], None]


def _default_ingest_hook(_corpus: str, path: str) -> dict:
    """Stub ingest. Real implementation lives in F004 ``errorta_extract``.

    For v0.1 we record a content hash and a synthetic file_id so the watcher
    state machine works without the heavy extraction pipeline. The F004 agent
    swaps this for the real chunker.
    """
    try:
        st = os.stat(path)
    except OSError:
        return {}
    return {
        "mtime": st.st_mtime,
        "size": st.st_size,
        "sha256": _content_hash(path),
        "xxhash": "",
        "file_id": f"stub:{os.path.basename(path)}",
        "chunk_ids": [],
        "source_missing": False,
    }


def _default_delete_hook(_corpus: str, _entry: ManifestEntry) -> None:
    """Stub deletion. Real implementation evicts chunks via F004."""
    return None


class FolderPoller:
    """One per watched corpus. Owns a thread; reconciles every interval."""

    def __init__(
        self,
        state: WatchState,
        *,
        interval: float = DEFAULT_POLL_INTERVAL,
        ingest_hook: IngestHook | None = None,
        delete_hook: DeleteHook | None = None,
    ) -> None:
        self.state = state
        self.interval = interval
        self.ingest_hook = ingest_hook or _default_ingest_hook
        self.delete_hook = delete_hook or _default_delete_hook
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        # _lock guards mutations of self.state (manifest, flags). It is held
        # only briefly — never across blocking I/O or sleeps.
        self._lock = threading.Lock()
        # _scan_lock serializes run_once() bodies so concurrent rescans don't
        # interleave. It is held for the duration of a scan (including ingest
        # retries with backoff sleeps), so callers that don't want to block —
        # notably force_rescan from an HTTP handler — should use try_run_once.
        self._scan_lock = threading.Lock()
        self._last_summary: dict | None = None

    # ---- lifecycle -------------------------------------------------------

    def start(self, *, initial_scan: bool = True) -> None:
        if self._thread is not None:
            return
        if initial_scan:
            # Synchronous first scan so the API can return an accurate summary.
            try:
                self.run_once()
            except Exception as exc:  # pragma: no cover — defensive
                log.exception("initial scan failed: %s", exc)
                self.state.last_error = str(exc)
                self.state.last_scan_ok = False
                save_state(self.state)
        self._thread = threading.Thread(
            target=self._loop,
            name=f"watch-{self.state.corpus}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None

    def pause(self) -> None:
        with self._lock:
            self.state.paused = True
            save_state(self.state)

    def resume(self) -> None:
        with self._lock:
            self.state.paused = False
            save_state(self.state)
        self._wake.set()

    def is_alive(self) -> bool:
        t = self._thread
        return bool(t and t.is_alive())

    # ---- core ------------------------------------------------------------

    def _ingest_with_retry(self, corpus: str, path: str) -> tuple[dict | None, str | None]:
        """Call ``ingest_hook`` with bounded exponential backoff.

        Returns ``(fields, error)`` — on success ``fields`` is the hook's
        return dict and ``error`` is ``None``; on final failure ``fields`` is
        ``None`` and ``error`` is the last exception's string. The caller
        must NOT advance manifest mtime/size when the hook fails, so the
        next poll re-attempts.
        """
        last_err: str | None = None
        for attempt in range(_INGEST_MAX_ATTEMPTS):
            try:
                return self.ingest_hook(corpus, path), None
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "ingest_hook failed (attempt %d/%d) for %s: %s",
                    attempt + 1, _INGEST_MAX_ATTEMPTS, path, last_err,
                )
                if attempt + 1 >= _INGEST_MAX_ATTEMPTS:
                    break
                time.sleep(_INGEST_BACKOFF_BASE * (2 ** attempt))
        return None, last_err

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=self.interval)
            self._wake.clear()
            if self._stop.is_set():
                return
            if self.state.paused:
                continue
            try:
                self.run_once()
            except Exception as exc:  # pragma: no cover — defensive
                log.exception("scan failed: %s", exc)
                with self._lock:
                    self.state.last_error = str(exc)
                    self.state.last_scan_ok = False
                    save_state(self.state)

    def run_once(self) -> dict:
        """Single reconciliation pass. Returns a small summary dict.

        Serialized by ``_scan_lock`` so two scans never interleave. The
        narrower ``_lock`` is taken only around state mutations and is
        released across blocking I/O (ingest retries with backoff sleeps),
        so ``pause()``/``resume()``/``status()`` remain responsive while a
        long scan is running.
        """
        with self._scan_lock:
            return self._run_once_locked()

    def try_run_once(self) -> tuple[bool, dict | None]:
        """Non-blocking variant of ``run_once``.

        Returns ``(True, summary)`` if this call ran a scan, or
        ``(False, last_summary_or_None)`` if another scan was already in
        flight. Intended for HTTP-driven force_rescan paths where blocking
        for tens of seconds on backoff sleeps is unacceptable.
        """
        if not self._scan_lock.acquire(blocking=False):
            return False, self._last_summary
        try:
            return True, self._run_once_locked()
        finally:
            self._scan_lock.release()

    def _run_once_locked(self) -> dict:
        """Body of run_once. Caller must hold ``_scan_lock``."""
        state = self.state
        # Snapshot inputs under _lock so concurrent pause/resume/status
        # observe a consistent view; release before any I/O.
        with self._lock:
            paths = _walk_supported(
                state.watched_path,
                state.type_filter,
                state.extra_ignores,
            )
        seen: set[str] = set()
        new_count = 0
        modified_count = 0
        moved_count = 0
        deleted_count = 0
        ingest_failures: list[str] = []

        # First pass: classify into new / modified / unchanged, hashing
        # only when needed. _lock is taken in short bursts around manifest
        # reads/writes; it is NOT held across _ingest_with_retry (which
        # sleeps up to 6s per failing file).
        for path in paths:
            seen.add(path)
            try:
                st = os.stat(path)
            except OSError:
                continue
            with self._lock:
                existing = state.manifest.get(path)
                existing_snapshot = (
                    None
                    if existing is None
                    else (existing.mtime, existing.size, existing.source_missing)
                )
            if existing_snapshot is None:
                # Could be a move from another path with the same hash.
                h = _content_hash(path)
                with self._lock:
                    moved_from = self._find_move_source(h, seen_paths=seen)
                    if moved_from is not None:
                        entry = state.manifest.pop(moved_from)
                        entry.mtime = st.st_mtime
                        entry.size = st.st_size
                        state.manifest[path] = entry
                        moved_count += 1
                        continue
                # Ingest call happens lock-free — the retry loop may sleep
                # up to 6s. pause()/resume()/status() must not block on it.
                fields, err = self._ingest_with_retry(state.corpus, path)
                with self._lock:
                    if fields is None:
                        # Backpressure: record stub entry marked
                        # source_missing=True; do NOT advance mtime/size so
                        # the next poll retries from scratch.
                        ingest_failures.append(f"{path}: {err}")
                        state.manifest[path] = ManifestEntry(
                            mtime=0.0,
                            size=0,
                            sha256="",
                            xxhash="",
                            file_id="",
                            chunk_ids=[],
                            source_missing=True,
                        )
                    else:
                        state.manifest[path] = ManifestEntry(
                            mtime=fields.get("mtime", st.st_mtime),
                            size=fields.get("size", st.st_size),
                            sha256=fields.get("sha256", h),
                            xxhash=fields.get("xxhash", ""),
                            file_id=fields.get("file_id", ""),
                            chunk_ids=list(fields.get("chunk_ids") or []),
                            source_missing=False,
                        )
                        new_count += 1
            else:
                ex_mtime, ex_size, ex_missing = existing_snapshot
                changed = (
                    st.st_mtime > ex_mtime + 1e-6
                    or st.st_size != ex_size
                    or ex_missing
                )
                if changed:
                    fields, err = self._ingest_with_retry(state.corpus, path)
                    with self._lock:
                        # Re-fetch — entry may have been replaced by a concurrent
                        # change_path or restart (it shouldn't, given _scan_lock,
                        # but be defensive).
                        existing = state.manifest.get(path)
                        if existing is None:
                            # Treat as new on next pass.
                            continue
                        if fields is None:
                            # Leave existing.mtime/size untouched so next poll
                            # still sees `changed=True` and retries.
                            existing.source_missing = True
                            ingest_failures.append(f"{path}: {err}")
                        else:
                            existing.mtime = fields.get("mtime", st.st_mtime)
                            existing.size = fields.get("size", st.st_size)
                            if fields.get("sha256"):
                                existing.sha256 = fields["sha256"]
                            if fields.get("file_id"):
                                existing.file_id = fields["file_id"]
                            if fields.get("chunk_ids") is not None:
                                existing.chunk_ids = list(fields["chunk_ids"])
                            existing.source_missing = False
                            modified_count += 1

        with self._lock:
            # Second pass: anything in the manifest not seen → deletion.
            missing = [p for p in state.manifest if p not in seen]
            for path in missing:
                entry = state.manifest[path]
                if state.deletion_policy == "remove":
                    self.delete_hook(state.corpus, entry)
                    del state.manifest[path]
                else:
                    entry.source_missing = True
                deleted_count += 1

            now = _now_iso()
            state.last_scan_at = now
            state.last_heartbeat = now
            if ingest_failures:
                state.last_scan_ok = False
                # Keep the message bounded — surface first failure only.
                state.last_error = ingest_failures[0]
            else:
                state.last_scan_ok = True
                state.last_error = None
            save_state(state)

            summary = {
                "scanned": len(paths),
                "new": new_count,
                "modified": modified_count,
                "moved": moved_count,
                "deleted": deleted_count,
                "ingest_failures": len(ingest_failures),
                "manifest_size": len(state.manifest),
            }
            self._last_summary = summary
            return summary

    # ---- helpers ---------------------------------------------------------

    def _find_move_source(self, content_hash: str, *, seen_paths: set[str]) -> str | None:
        """If a manifest entry has this hash but its old path is gone, return it."""
        if not content_hash:
            return None
        for old_path, entry in self.state.manifest.items():
            if old_path in seen_paths:
                continue
            if entry.sha256 and entry.sha256 == content_hash:
                if not os.path.exists(old_path):
                    return old_path
        return None
