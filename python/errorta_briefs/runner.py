"""BriefRunner orchestrator for F008e.

One-at-a-time queued execution of brief-driven corpus collection runs. The
runner instantiates registered ``SourceConnector`` plugins, paginates through
``search``, gates each candidate through ``ComplianceGate``, dedupes against a
corpus-scoped canonical-id index, and hands accepted docs to an injected
ingest callback.

Lifecycle FSM and ``CollectState`` persistence live in
:mod:`errorta_briefs.lifecycle` and :mod:`errorta_briefs.state` (the F008d
modules). This module consumes those public surfaces directly. Runner-only
extras that are out of scope for the canonical ``CollectState`` schema
(compliance refusals, ingested-id ledger, per-source ``docs_refused`` /
``last_canonical_id`` / ``last_error``) are persisted to a sidecar
``run-extras.json`` so the canonical schema stays small and stable.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from errorta_briefs.compliance import ComplianceGate
from errorta_briefs.connector import (
    FatalError,
    RetryableError,
    SourceConnector,
    SourceDoc,
)
from errorta_briefs.lifecycle import BriefState, assert_transition
from errorta_briefs.schema import BriefConfig, SourceSpec
from errorta_briefs.state import (
    CollectState,
    FailureRecord,
    LastCheckpoint,
    SourceState,
    load_collect_state as _load_collect_state_path,
    save_collect_state as _save_collect_state_path,
)


# ---------------------------------------------------------------------------
# Brief-dir convenience wrappers around the canonical state I/O
# ---------------------------------------------------------------------------


_COLLECT_STATE_FILENAME = "collect-state.json"
_DEDUP_FILENAME = "dedup-index.json"
_EXTRAS_FILENAME = "run-extras.json"


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def save_collect_state(brief_dir: Path, cs: CollectState) -> None:
    """Persist ``cs`` under ``brief_dir/collect-state.json``."""
    _save_collect_state_path(cs, brief_dir / _COLLECT_STATE_FILENAME)


def load_collect_state(brief_dir: Path) -> Optional[CollectState]:
    """Load the canonical CollectState from ``brief_dir`` (None if missing)."""
    try:
        return _load_collect_state_path(brief_dir / _COLLECT_STATE_FILENAME)
    except Exception:
        return None


def load_dedup_index(brief_dir: Path) -> set[str]:
    path = brief_dir / _DEDUP_FILENAME
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data.get("canonical_ids", []))
    except Exception:
        return set()


def save_dedup_index(brief_dir: Path, ids: set[str]) -> None:
    _atomic_write_json(
        brief_dir / _DEDUP_FILENAME,
        {"canonical_ids": sorted(ids)},
    )


# ---------------------------------------------------------------------------
# Run-extras: sidecar fields the canonical CollectState does not carry
# ---------------------------------------------------------------------------


@dataclass
class SourceExtras:
    """Per-source fields outside the canonical ``SourceState`` schema."""

    docs_refused: int = 0
    last_canonical_id: Optional[str] = None
    last_error: Optional[str] = None
    corpus_file_ids: list[str] = field(default_factory=list)


@dataclass
class RunExtras:
    """Sidecar to ``CollectState`` for runner-only bookkeeping.

    Persisted next to ``collect-state.json`` as ``run-extras.json``. Kept here
    rather than forking the canonical state schema (F008d) so that any
    future schema migration in state.py does not have to chase runner state.
    """

    compliance_refusals: list[dict[str, Any]] = field(default_factory=list)
    ingested_canonical_ids: list[str] = field(default_factory=list)
    per_source: dict[str, SourceExtras] = field(default_factory=dict)

    def get_source(self, name: str) -> SourceExtras:
        return self.per_source.setdefault(name, SourceExtras())

    def to_dict(self) -> dict[str, Any]:
        return {
            "compliance_refusals": list(self.compliance_refusals),
            "ingested_canonical_ids": list(self.ingested_canonical_ids),
            "per_source": {k: asdict(v) for k, v in self.per_source.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunExtras":
        return cls(
            compliance_refusals=list(data.get("compliance_refusals") or []),
            ingested_canonical_ids=list(data.get("ingested_canonical_ids") or []),
            per_source={
                k: SourceExtras(
                    docs_refused=int(v.get("docs_refused", 0) or 0),
                    last_canonical_id=v.get("last_canonical_id"),
                    last_error=v.get("last_error"),
                    corpus_file_ids=list(v.get("corpus_file_ids") or []),
                )
                for k, v in (data.get("per_source") or {}).items()
            },
        )


def load_run_extras(brief_dir: Path) -> RunExtras:
    path = brief_dir / _EXTRAS_FILENAME
    if not path.exists():
        return RunExtras()
    try:
        return RunExtras.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return RunExtras()


def save_run_extras(brief_dir: Path, extras: RunExtras) -> None:
    _atomic_write_json(brief_dir / _EXTRAS_FILENAME, extras.to_dict())


# ---------------------------------------------------------------------------
# Connector registry
# ---------------------------------------------------------------------------


# Populated by tests / production code via register_connector or by setting
# the dict directly. The runner looks up connectors by SourceSpec.name.
CONNECTOR_REGISTRY: dict[str, type[SourceConnector]] = {}


def register_connector(name: str, cls: type[SourceConnector]) -> None:
    CONNECTOR_REGISTRY[name] = cls


# ---------------------------------------------------------------------------
# BriefRunner — single-active-run orchestration
# ---------------------------------------------------------------------------


# Module-level singleton — enforces "one brief at a time" per process.
_ACTIVE_RUN = threading.Lock()
_CURRENT_RUN_ID: Optional[str] = None
_CURRENT_BRIEF_ID: Optional[str] = None
_CURRENT_RUNNER: Optional["BriefRunner"] = None


# Ingest callback contract:
#   def ingest(doc: SourceDoc, payload: bytes, metadata: dict) -> None
IngestCallback = Callable[[SourceDoc, bytes, dict], None]


def _default_publish(event: dict[str, Any]) -> None:
    """Default event sink: reuse the corpus pipeline event bus if available."""
    try:
        from errorta_corpus.pipeline import publish as _pub  # local import

        _pub(event)
    except Exception:
        pass


def _new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ-") + uuid.uuid4().hex[:6]


def _brief_dir_for(corpus_root: Path, corpus_slug: str) -> Path:
    d = corpus_root / corpus_slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "run-logs").mkdir(parents=True, exist_ok=True)
    return d


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter: 1, 2, 4, 8, 16 seconds."""
    base = min(2 ** attempt, 16)
    return base + random.uniform(0, base * 0.1)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class BriefRunner:
    """Drives a single brief-driven collection run to completion or pause.

    Use :meth:`submit` to start a run on a daemon thread. Only one run may be
    active across the process at a time — additional submissions raise.
    """

    CHECKPOINT_EVERY = 5
    MAX_RETRIES = 5

    def __init__(
        self,
        *,
        ingest: Optional[IngestCallback] = None,
        publish: Callable[[dict[str, Any]], None] = _default_publish,
        compliance_gate: Optional[ComplianceGate] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._ingest = ingest if ingest is not None else self._default_corpus_ingest
        self._publish = publish
        self._gate = compliance_gate or ComplianceGate()
        self._sleep = sleep
        self._paused_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._brief_id: Optional[str] = None
        self._run_id: Optional[str] = None
        self._brief_dir: Optional[Path] = None
        self._corpus_name: Optional[str] = None
        # Counter for tests: callback invocation count.
        self.ingest_call_count = 0
        # Most-recent corpus file_id from the default ingest callback; the
        # run loop reads + clears this after a successful ingest so it can
        # attribute the file to the right source in RunExtras.
        self.last_corpus_file_id: Optional[str] = None
        # Marker raised by the default ingest callback to signal "the corpus
        # already has this sha256 — skip without incrementing counters". The
        # run loop checks this before counting an ingest.
        self.last_ingest_was_duplicate: bool = False
        self._ingest_lock = threading.Lock()

    # --- default ingest (F004 corpus promotion) -----------------------------

    @staticmethod
    def _ext_for(doc: SourceDoc) -> str:
        """Resolve mime extension for the corpus copy.

        Connectors populate ``doc.extra['file_ext']`` (e.g. ``.pdf``,
        ``.html``). Default is ``.bin`` so an unset extra never crashes the
        ingest path — the audit-flagged connectors all set this explicitly.
        """
        ext = (doc.extra or {}).get("file_ext")
        if isinstance(ext, str) and ext:
            return ext if ext.startswith(".") else f".{ext}"
        return ".bin"

    def _default_corpus_ingest(
        self, doc: SourceDoc, payload: bytes, metadata: dict
    ) -> None:
        """Promote ``payload`` into the F004 corpus.

        Steps:
          1. sha256 the payload.
          2. Short-circuit when the corpus manifest already has this sha
             (overwrite=False semantics — fetched bytes are discarded).
          3. Allocate file_id + copied path via ``errorta_corpus.pipeline``.
          4. Atomically write bytes (temp file + rename).
          5. ``upsert_entry`` a queued ``FileEntry``.
          6. ``enqueue`` the file for extract/chunk/embed.
          7. On any failure unlink the copied file and raise ``FatalError``.

        The runner's outer counter (``ingest_call_count``) is also bumped on
        every accepted (non-duplicate) write so legacy tests keep observing
        a true "callback invocations" count.
        """
        # Lazy imports keep the brief subsystem usable when errorta_corpus is
        # unavailable (e.g. a stripped test environment); production always
        # has both packages installed.
        from errorta_corpus import corpus_dir
        from errorta_corpus.manifest import FileEntry, find_by_sha256, upsert_entry
        from errorta_corpus.pipeline import copied_path_for, enqueue, new_file_id

        if self._corpus_name is None:
            raise FatalError("default ingest invoked outside an active run")
        corpus_name = self._corpus_name

        sha = hashlib.sha256(payload).hexdigest()

        # Reset bookkeeping flags so a stale duplicate marker from the prior
        # ingest doesn't bleed into this one.
        self.last_corpus_file_id = None
        self.last_ingest_was_duplicate = False

        existing = find_by_sha256(corpus_name, sha)
        if existing is not None:
            # Don't write bytes, don't enqueue, don't bump docs_ingested.
            self.last_ingest_was_duplicate = True
            return

        ext = self._ext_for(doc)
        original_name = (
            (doc.extra or {}).get("original_filename")
            or f"{doc.canonical_id.replace(':', '_').replace('/', '_')}{ext}"
        )
        # Make sure the corpus directory tree exists before allocating paths.
        corpus_dir(corpus_name)
        file_id = new_file_id()
        copied = copied_path_for(corpus_name, original_name)
        copied.parent.mkdir(parents=True, exist_ok=True)

        tmp = copied.with_suffix(copied.suffix + ".tmp")
        wrote_copied = False
        try:
            tmp.write_bytes(payload)
            os.replace(tmp, copied)
            wrote_copied = True

            entry = FileEntry(
                file_id=file_id,
                original_path=(doc.extra or {}).get("source_url") or doc.source_url or "",
                copied_path=str(copied),
                sha256=sha,
                size_bytes=len(payload),
                mime_ext=ext,
                status="queued",
            )
            upsert_entry(corpus_name, entry)
            enqueue(corpus_name, file_id)
        except BaseException as exc:
            # Roll back the byte write; never leave an orphan file under the
            # corpus tree when the manifest upsert / enqueue failed.
            try:
                if wrote_copied and copied.exists():
                    copied.unlink()
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            if isinstance(exc, FatalError):
                raise
            raise FatalError(f"corpus ingest failed: {exc}") from exc

        with self._ingest_lock:
            self.ingest_call_count += 1
        self.last_corpus_file_id = file_id

    # --- lifecycle ----------------------------------------------------------

    def submit(
        self,
        brief_id: str,
        config: BriefConfig,
        corpus_root: Path,
        *,
        resume: bool = False,
    ) -> str:
        """Start a run. Returns the run_id. Raises if a run is already active."""
        global _CURRENT_RUN_ID, _CURRENT_BRIEF_ID, _CURRENT_RUNNER

        if not _ACTIVE_RUN.acquire(blocking=False):
            raise RuntimeError(
                f"another brief run is already active (run_id={_CURRENT_RUN_ID})"
            )

        try:
            self._brief_id = brief_id
            self._corpus_name = config.corpus
            self._brief_dir = _brief_dir_for(corpus_root, config.corpus)
            existing = load_collect_state(self._brief_dir) if resume else None
            extras = load_run_extras(self._brief_dir) if resume else RunExtras()
            if existing is not None and resume:
                self._run_id = existing.run_id
                cs = existing
                cs.state = BriefState.RUNNING
            else:
                self._run_id = _new_run_id()
                cs = CollectState(
                    brief_id=brief_id,
                    corpus_name=config.corpus,
                    run_id=self._run_id,
                    started_at=_utcnow_iso(),
                    updated_at=_utcnow_iso(),
                    state=BriefState.RUNNING,
                    per_source={s.name: SourceState() for s in config.sources},
                )
                extras = RunExtras(
                    per_source={s.name: SourceExtras() for s in config.sources}
                )
            save_collect_state(self._brief_dir, cs)
            save_run_extras(self._brief_dir, extras)

            _CURRENT_RUN_ID = self._run_id
            _CURRENT_BRIEF_ID = brief_id
            _CURRENT_RUNNER = self
            self._paused_event.clear()
            self._stop_event.clear()
            t = threading.Thread(
                target=self._run_loop,
                name=f"errorta-brief-{brief_id}",
                args=(config, cs, extras),
                daemon=True,
            )
            self._thread = t
            t.start()
            return self._run_id
        except BaseException:
            _ACTIVE_RUN.release()
            _CURRENT_RUN_ID = None
            _CURRENT_BRIEF_ID = None
            _CURRENT_RUNNER = None
            raise

    def pause(self) -> None:
        self._paused_event.set()

    def stop(self) -> None:
        """Forcibly stop the run loop (used by interrupt tests + delete)."""
        self._stop_event.set()
        self._paused_event.set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until the run thread exits. Returns True if exited."""
        if self._thread is None:
            return True
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    # --- main loop ----------------------------------------------------------

    def _run_loop(
        self,
        config: BriefConfig,
        cs: CollectState,
        extras: RunExtras,
    ) -> None:
        global _CURRENT_RUN_ID, _CURRENT_BRIEF_ID, _CURRENT_RUNNER
        assert self._brief_dir is not None
        brief_dir = self._brief_dir
        dedup = load_dedup_index(brief_dir)
        any_source_completed = False
        try:
            for source in config.sources:
                if self._stop_event.is_set() or self._paused_event.is_set():
                    assert_transition(cs.state, BriefState.PAUSED)
                    cs.state = BriefState.PAUSED
                    save_collect_state(brief_dir, cs)
                    save_run_extras(brief_dir, extras)
                    return

                ps = cs.per_source.setdefault(source.name, SourceState())
                extras.get_source(source.name)
                if ps.state == "completed":
                    continue
                ps.state = "running"
                save_collect_state(brief_dir, cs)

                ok = self._run_source(source, cs, extras, dedup, brief_dir)
                if ok:
                    ps.state = "completed"
                    any_source_completed = True
                else:
                    ps.state = "failed"
                save_collect_state(brief_dir, cs)
                save_run_extras(brief_dir, extras)
                save_dedup_index(brief_dir, dedup)

                if self._stop_event.is_set() or self._paused_event.is_set():
                    assert_transition(cs.state, BriefState.PAUSED)
                    cs.state = BriefState.PAUSED
                    save_collect_state(brief_dir, cs)
                    return

            # Determine final state
            all_failed = all(
                ps.state == "failed" for ps in cs.per_source.values()
            )
            if all_failed and not any_source_completed:
                assert_transition(cs.state, BriefState.FAILED)
                cs.state = BriefState.FAILED
            else:
                assert_transition(cs.state, BriefState.COMPLETED)
                cs.state = BriefState.COMPLETED
            save_collect_state(brief_dir, cs)
            save_run_extras(brief_dir, extras)
            save_dedup_index(brief_dir, dedup)
            self._publish(
                {
                    "type": "brief.run",
                    "brief_id": self._brief_id,
                    "run_id": self._run_id,
                    "state": cs.state.value,
                }
            )
            total_docs_ingested = sum(
                ps.docs_ingested_to_corpus for ps in cs.per_source.values()
            )
            self._publish(
                {
                    "type": "brief_run_completed",
                    "brief_id": self._brief_id,
                    "corpus_name": cs.corpus_name,
                    "total_docs_ingested": total_docs_ingested,
                }
            )
        finally:
            try:
                _ACTIVE_RUN.release()
            except RuntimeError:
                pass
            _CURRENT_RUN_ID = None
            _CURRENT_BRIEF_ID = None
            _CURRENT_RUNNER = None

    def _record_failure(
        self,
        cs: CollectState,
        extras: RunExtras,
        source_name: str,
        error_class: str,
        message: str,
        *,
        retry_count: int = 0,
    ) -> None:
        rec = FailureRecord(
            error_class=error_class,
            message=message,
            occurred_at=_utcnow_iso(),
            retry_count=retry_count,
        )
        # Stash the source_name on the runner extras' per-source slot for UI.
        cs.failures.append(rec)
        se = extras.get_source(source_name)
        se.last_error = message

    def _run_source(
        self,
        source: SourceSpec,
        cs: CollectState,
        extras: RunExtras,
        dedup: set[str],
        brief_dir: Path,
    ) -> bool:
        """Drive one source's pagination. Returns True on natural completion."""
        cls = CONNECTOR_REGISTRY.get(source.name)
        if cls is None:
            self._record_failure(
                cs,
                extras,
                source.name,
                "FatalError",
                f"no connector registered for '{source.name}'",
            )
            return False
        try:
            connector = cls(source.config)
        except Exception as exc:
            self._record_failure(
                cs, extras, source.name, "FatalError", f"connector init failed: {exc}"
            )
            return False

        ps = cs.per_source[source.name]
        se = extras.get_source(source.name)
        page = ps.page_or_offset or 0
        docs_since_checkpoint = 0

        while True:
            if self._stop_event.is_set() or self._paused_event.is_set():
                return False  # treated as not-completed; resume picks up page

            attempt = 0
            page_iter: Optional[Any] = None
            while True:
                try:
                    page_iter = connector.search(page=page)
                    break
                except RetryableError as exc:
                    attempt += 1
                    self._record_failure(
                        cs,
                        extras,
                        source.name,
                        "RetryableError",
                        str(exc),
                        retry_count=attempt,
                    )
                    if attempt >= self.MAX_RETRIES:
                        return False
                    self._sleep(_backoff_delay(attempt - 1))
                except FatalError as exc:
                    self._record_failure(
                        cs, extras, source.name, "FatalError", str(exc)
                    )
                    return False
                except Exception as exc:
                    self._record_failure(
                        cs, extras, source.name, "FatalError", f"unexpected: {exc}"
                    )
                    return False

            page_had_results = False
            try:
                for doc in page_iter or []:
                    page_had_results = True
                    if self._stop_event.is_set() or self._paused_event.is_set():
                        return False

                    cid = doc.canonical_id
                    if cid in dedup:
                        se.last_canonical_id = cid
                        continue

                    ok, reason = self._gate.accepts(doc)
                    if not ok:
                        refusal = self._gate.refusal(doc, reason or "refused")
                        extras.compliance_refusals.append(
                            {
                                "canonical_id": refusal.canonical_id,
                                "refusal_reason": refusal.reason,
                                "source_name": source.name,
                                "occurred_at": refusal.occurred_at,
                            }
                        )
                        se.docs_refused += 1
                        self._publish(
                            {
                                "type": "brief.doc",
                                "brief_id": self._brief_id,
                                "source": source.name,
                                "canonical_id": cid,
                                "status": "refused",
                                "reason": reason,
                            }
                        )
                        continue

                    # Fetch + ingest (with retry on RetryableError).
                    payload, metadata, fetch_ok = self._fetch_with_retry(
                        connector, doc, source.name, cs, extras
                    )
                    if not fetch_ok:
                        # exhausted retries; abandon source
                        return False
                    # Reset duplicate marker before each call so a stale flag
                    # from the prior doc can't gate this one.
                    self.last_ingest_was_duplicate = False
                    self.last_corpus_file_id = None
                    try:
                        self._ingest(doc, payload, metadata)
                    except Exception as exc:
                        self._record_failure(
                            cs,
                            extras,
                            source.name,
                            "FatalError",
                            f"ingest failed: {exc}",
                        )
                        return False

                    if self.last_ingest_was_duplicate:
                        # Corpus already had this sha256 — record canonical id
                        # in dedup index so the run doesn't re-fetch, but do
                        # not count this as a corpus ingest.
                        dedup.add(cid)
                        se.last_canonical_id = cid
                        self._publish(
                            {
                                "type": "brief.doc",
                                "brief_id": self._brief_id,
                                "source": source.name,
                                "canonical_id": cid,
                                "status": "duplicate_in_corpus",
                            }
                        )
                        continue

                    dedup.add(cid)
                    extras.ingested_canonical_ids.append(cid)
                    ps.docs_ingested_to_corpus += 1
                    if self.last_corpus_file_id is not None:
                        se.corpus_file_ids.append(self.last_corpus_file_id)
                    se.last_canonical_id = cid
                    cs.last_checkpoint = LastCheckpoint(
                        source_name=source.name,
                        page_or_offset=page,
                        docs_collected=ps.docs_ingested_to_corpus,
                        last_canonical_id=cid,
                    )
                    docs_since_checkpoint += 1
                    self._publish(
                        {
                            "type": "brief.doc",
                            "brief_id": self._brief_id,
                            "source": source.name,
                            "canonical_id": cid,
                            "status": "ingested",
                            "file_id": self.last_corpus_file_id,
                        }
                    )
                    if docs_since_checkpoint >= self.CHECKPOINT_EVERY:
                        save_collect_state(brief_dir, cs)
                        save_run_extras(brief_dir, extras)
                        save_dedup_index(brief_dir, dedup)
                        docs_since_checkpoint = 0
            except RetryableError as exc:
                self._record_failure(
                    cs, extras, source.name, "RetryableError", str(exc)
                )
                self._sleep(_backoff_delay(0))
                continue  # retry same page
            except FatalError as exc:
                self._record_failure(
                    cs, extras, source.name, "FatalError", str(exc)
                )
                return False

            if not page_had_results:
                # natural end of stream
                save_collect_state(brief_dir, cs)
                save_run_extras(brief_dir, extras)
                save_dedup_index(brief_dir, dedup)
                return True

            page += 1
            ps.page_or_offset = page
            # Always flush at source-page boundary (cheap source transition).
            save_collect_state(brief_dir, cs)
            save_run_extras(brief_dir, extras)

    def _fetch_with_retry(
        self,
        connector: SourceConnector,
        doc: SourceDoc,
        source_name: str,
        cs: CollectState,
        extras: RunExtras,
    ) -> tuple[bytes, dict, bool]:
        attempt = 0
        while True:
            try:
                payload = connector.fetch(doc)
                metadata = connector.metadata(doc)
                return payload, metadata, True
            except RetryableError as exc:
                attempt += 1
                self._record_failure(
                    cs,
                    extras,
                    source_name,
                    "RetryableError",
                    str(exc),
                    retry_count=attempt,
                )
                if attempt >= self.MAX_RETRIES:
                    return b"", {}, False
                self._sleep(_backoff_delay(attempt - 1))
            except FatalError as exc:
                self._record_failure(
                    cs, extras, source_name, "FatalError", str(exc)
                )
                return b"", {}, False
            except Exception as exc:
                self._record_failure(
                    cs, extras, source_name, "FatalError", f"unexpected: {exc}"
                )
                return b"", {}, False


# ---------------------------------------------------------------------------
# Process-wide helpers used by routes
# ---------------------------------------------------------------------------


def current_run() -> tuple[Optional[str], Optional[str], Optional[BriefRunner]]:
    """Return ``(brief_id, run_id, runner)`` for the active run, or ``(None,None,None)``."""
    return _CURRENT_BRIEF_ID, _CURRENT_RUN_ID, _CURRENT_RUNNER


def reset_active_run() -> None:
    """Test-only: forcibly release the active-run lock.

    Use sparingly. The interrupt-simulation test calls this after killing the
    previous runner to model "process died, lock would have been freed".
    """
    global _CURRENT_RUN_ID, _CURRENT_BRIEF_ID, _CURRENT_RUNNER
    if _ACTIVE_RUN.locked():
        try:
            _ACTIVE_RUN.release()
        except RuntimeError:
            pass
    _CURRENT_RUN_ID = None
    _CURRENT_BRIEF_ID = None
    _CURRENT_RUNNER = None
