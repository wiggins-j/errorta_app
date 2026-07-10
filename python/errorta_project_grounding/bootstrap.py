"""Safe build-from-repo corpus bootstrap for F088."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Iterable

from errorta_corpus import corpus_dir, validate_corpus_name
from errorta_corpus.manifest import FileEntry, reserve_or_get_duplicate
from errorta_corpus.pipeline import copied_path_for, enqueue, new_file_id
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.locks import lock_for_dir
from errorta_extract.registry import supported_extensions, text_source_extensions

from . import paths as _paths
from .corpus_binding import ProjectCorpusBinding, load_binding, save_binding
from .memory_store import _now

# Legacy names re-exported from the single shared safe-index policy so existing
# importers keep working (the deny rules now live in paths.py).
DENY_PARTS = set(_paths.DENY_PARTS)
SENSITIVE_NAMES = set(_paths.SENSITIVE_NAMES)
MAX_FILES = 2_000
MAX_TOTAL_BYTES = 50 * 1024 * 1024
# A job left "running" longer than this is treated as orphaned (bootstrap runs
# inline, so a still-"running" job past this age means the request died).
STALE_JOB_SECONDS = 3600


@dataclass(frozen=True)
class BootstrapPlan:
    source_root: str
    included: tuple[str, ...] = ()
    skipped: dict[str, str] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    total_bytes: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class BootstrapJob:
    job_id: str
    project_id: str
    corpus_id: str
    source_root: str
    status: str
    started_at: str
    ended_at: str | None = None
    plan: BootstrapPlan | None = None
    enqueued: tuple[str, ...] = ()
    duplicates: tuple[str, ...] = ()
    skipped: dict[str, str] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    # Remote-path audit (corpus on watchdog AIAR): for the local path these stay
    # 0 and the per-file detail lives in `enqueued`/`duplicates`.
    adapter_source: str = "local"
    documents_ingested: int = 0
    chunks_added: int = 0

    def to_dict(self) -> dict[str, object]:
        out = asdict(self)
        if self.plan:
            out["plan"] = self.plan.to_dict()
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "BootstrapJob":
        plan_raw = raw.get("plan")
        plan = BootstrapPlan(**plan_raw) if isinstance(plan_raw, dict) else None
        return cls(
            job_id=str(raw.get("job_id") or ""),
            project_id=str(raw.get("project_id") or ""),
            corpus_id=str(raw.get("corpus_id") or ""),
            source_root=str(raw.get("source_root") or ""),
            status=str(raw.get("status") or "failed"),
            started_at=str(raw.get("started_at") or ""),
            ended_at=raw.get("ended_at") if isinstance(raw.get("ended_at"), str) else None,
            plan=plan,
            enqueued=tuple(raw.get("enqueued") or ()),
            duplicates=tuple(raw.get("duplicates") or ()),
            skipped=dict(raw.get("skipped") or {}),
            errors=tuple(raw.get("errors") or ()),
            adapter_source=str(raw.get("adapter_source") or "local"),
            documents_ingested=int(raw.get("documents_ingested") or 0),
            chunks_added=int(raw.get("chunks_added") or 0),
        )


def _job_dir(store: LedgerStore) -> Path:
    return store.dir / "grounding" / "bootstrap-jobs"


def _job_path(store: LedgerStore, job_id: str) -> Path:
    return _job_dir(store) / f"{job_id}.json"


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, sort_keys=True, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def save_job(store: LedgerStore, job: BootstrapJob) -> BootstrapJob:
    _atomic_write_json(_job_path(store, job.job_id), job.to_dict())
    return job


def load_job(store: LedgerStore, job_id: str) -> BootstrapJob | None:
    path = _job_path(store, job_id)
    if not path.exists():
        return None
    try:
        return BootstrapJob.from_dict(json.loads(path.read_text("utf-8")))
    except Exception:
        return None


def list_jobs(store: LedgerStore) -> list[BootstrapJob]:
    d = _job_dir(store)
    if not d.exists():
        return []
    out: list[BootstrapJob] = []
    for path in sorted(d.glob("boot_*.json")):
        try:
            out.append(BootstrapJob.from_dict(json.loads(path.read_text("utf-8"))))
        except Exception:
            continue
    return out


def _age_seconds(iso_ts: str) -> float:
    from datetime import datetime, timezone
    try:
        started = datetime.fromisoformat(iso_ts)
    except (ValueError, TypeError):
        return float("inf")
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - started).total_seconds()


def recover_stale_jobs(store: LedgerStore) -> int:
    """Bootstrap runs inline in the request, so a job still marked ``running``
    past ``STALE_JOB_SECONDS`` means its request died mid-flight (orphaned).
    Flip those to ``interrupted`` so a fresh bootstrap is not blocked forever by
    a ghost active job. Returns the number recovered."""
    recovered = 0
    for job in list_jobs(store):
        if job.status == "running" and _age_seconds(job.started_at) > STALE_JOB_SECONDS:
            save_job(store, replace(job, status="interrupted", ended_at=_now(),
                                    errors=job.errors + ("recovered: orphaned running job",)))
            recovered += 1
    return recovered


def active_job(store: LedgerStore) -> BootstrapJob | None:
    """A non-stale ``running`` job for this project, if any (idempotency key)."""
    for job in list_jobs(store):
        if job.status == "running" and _age_seconds(job.started_at) <= STALE_JOB_SECONDS:
            return job
    return None


def _gitignore_patterns(root: Path) -> list[str]:
    path = root / ".gitignore"
    if not path.exists():
        return []
    out: list[str] = []
    for raw in path.read_text("utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        out.append(line.rstrip("/"))
    return out


def _ignored_by_gitignore(rel: str, patterns: Iterable[str]) -> bool:
    name = Path(rel).name
    for pattern in patterns:
        pat = pattern.lstrip("/")
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat):
            return True
        if "/" not in pat and any(part == pat for part in Path(rel).parts):
            return True
    return False


def _skip_reason(
    root: Path,
    path: Path,
    patterns: Iterable[str],
    supported: set[str],
) -> str | None:
    rel = path.relative_to(root).as_posix()
    # Single shared safe-index policy (deny dirs / hidden / secret names).
    if _paths.is_sensitive_path(rel):
        return "denied_path"
    if _ignored_by_gitignore(rel, patterns):
        return "gitignored"
    if path.suffix.lower() not in supported:
        return "unsupported_extension"
    try:
        chunk = path.read_bytes()[:4096]
    except OSError as exc:
        return f"read_error:{exc}"
    if b"\x00" in chunk:
        return "binary_file"
    # A small text file that itself carries a high-confidence secret is never
    # indexed even if its name looks innocuous.
    try:
        if _paths.content_has_secret(chunk.decode("utf-8", errors="ignore")):
            return "secret_content"
    except Exception:
        pass
    return None


# F088-03/04: a project corpus indexes the team's code, but the public document
# extension list remains scoped to user-ingestable corpus files. Bootstrap opts
# into source extensions explicitly so project code can be planned and extracted.
CODE_EXTENSIONS = frozenset(text_source_extensions())


def plan_project_bootstrap(source_root: Path, *,
                           extra_extensions: frozenset[str] = frozenset()) -> BootstrapPlan:
    root = Path(source_root).expanduser().resolve()
    errors: list[str] = []
    if not root.is_dir():
        return BootstrapPlan(source_root=str(root), errors=(f"source not a directory: {root}",))
    patterns = _gitignore_patterns(root)
    supported = set(supported_extensions()) | set(extra_extensions)
    included: list[str] = []
    skipped: dict[str, str] = {}
    total = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        reason = _skip_reason(root, path, patterns, supported)
        if reason:
            skipped[rel] = reason
            continue
        size = path.stat().st_size
        if len(included) >= MAX_FILES:
            skipped[rel] = "file_cap"
            continue
        if total + size > MAX_TOTAL_BYTES:
            skipped[rel] = "byte_cap"
            continue
        included.append(rel)
        total += size
    return BootstrapPlan(
        source_root=str(root),
        included=tuple(included),
        skipped=skipped,
        errors=tuple(errors),
        total_bytes=total,
    )


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_into_corpus(source_root: Path, rel: str, corpus_id: str) -> tuple[str | None, str | None]:
    src = source_root / rel
    target = copied_path_for(corpus_id, src.name)
    files_root = (corpus_dir(corpus_id) / "files").resolve()
    try:
        target.resolve().relative_to(files_root)
    except (OSError, ValueError):
        return None, "invalid target path"
    digest = _sha256(src)
    with src.open("rb") as inp, target.open("wb") as out:
        for chunk in iter(lambda: inp.read(1 << 20), b""):
            out.write(chunk)
    file_id = new_file_id()
    entry = FileEntry(
        file_id=file_id,
        original_path=str(src),
        copied_path=str(target),
        sha256=digest,
        size_bytes=src.stat().st_size,
        mime_ext=src.suffix.lower(),
        status="queued",
    )
    inserted, _prior = reserve_or_get_duplicate(corpus_id, digest, entry, overwrite=False)
    if inserted is None:
        target.unlink(missing_ok=True)
        return None, None
    enqueue(corpus_id, file_id)
    return file_id, None


def _active_remote_adapter():
    """The configured remote grounding adapter, or None for the local path.
    Delegates to the shared selector so bootstrap + routes decide identically."""
    try:
        from .remote_adapter import active_remote_adapter
    except Exception:
        return None
    return active_remote_adapter()


def _ingest_into_remote(adapter, *, corpus_id: str, root: Path, plan: BootstrapPlan) -> dict:
    """Ingest the planned files into the remote AIAR instance: ensure the
    instance, ingest each file (AIAR chunks + embeds server-side), then publish.
    The safe-index policy already ran in planning (secrets/denied paths are in
    ``plan.skipped`` and never reach here); the adapter screens again before
    egress. FAIL CLOSED: the adapter raises on a failed/empty ingest job, so a
    partial batch surfaces as errors and the caller marks the bootstrap failed —
    and we do NOT publish a partially-failed corpus as ready."""
    errors: list[str] = list(plan.errors)
    ingested: list[str] = []
    dup_rels: list[str] = []
    chunks_added = 0
    if not plan.included:
        return {"errors": ["no files eligible for remote ingest"], "ingested": [],
                "duplicates": [], "chunks_added": 0}
    try:
        adapter.ensure_instance(corpus_id)
    except Exception as exc:
        return {"errors": [f"ensure_instance: {exc}"], "ingested": [],
                "duplicates": [], "chunks_added": 0}
    for rel in plan.included:
        try:
            ref = adapter.ingest_file(corpus_id=corpus_id, path=root / rel,
                                      metadata={"source": rel})
        except Exception as exc:
            errors.append(f"{rel}: {exc}")
            continue
        meta = ref.metadata or {}
        try:
            added = int(meta.get("chunks_added") or 0)
            dups = int(meta.get("duplicates") or 0)
        except (TypeError, ValueError):
            errors.append(f"{rel}: invalid remote ingest metadata")
            continue
        if added > 0:
            chunks_added += added
            ingested.append(rel)
        elif dups > 0:
            dup_rels.append(rel)
        else:
            errors.append(f"{rel}: remote ingest stored no chunks")
    if not errors:
        try:
            adapter.publish(corpus_id)
        except Exception as exc:
            errors.append(f"publish: {exc}")
    return {"errors": errors, "ingested": ingested, "duplicates": dup_rels,
            "chunks_added": chunks_added}


def start_project_bootstrap(
    store: LedgerStore,
    *,
    corpus_id: str,
    source_root: Path,
    extra_extensions: frozenset[str] = frozenset(),
) -> BootstrapJob:
    validate_corpus_name(corpus_id)
    root = Path(source_root).expanduser().resolve()
    # Per-project lock + stale recovery + idempotency: a concurrent in-flight
    # bootstrap for the SAME corpus+root returns the existing job instead of
    # racing a second copy into the corpus.
    with lock_for_dir(store.dir):
        recover_stale_jobs(store)
        existing = active_job(store)
        if existing is not None and existing.corpus_id == corpus_id \
                and existing.source_root == str(root):
            return existing
        plan = plan_project_bootstrap(root, extra_extensions=extra_extensions)
        job = BootstrapJob(
            job_id=f"boot_{uuid.uuid4().hex}",
            project_id=store.project_id,
            corpus_id=corpus_id,
            source_root=str(root),
            status="running",
            started_at=_now(),
            plan=plan,
            skipped=plan.skipped,
            errors=plan.errors,
        )
        save_job(store, job)
    remote_adapter = _active_remote_adapter()
    adapter_source = "remote" if remote_adapter is not None else "local"
    binding = load_binding(store)
    save_binding(
        store,
        ProjectCorpusBinding(
            project_id=store.project_id,
            mode="build_from_repo",
            corpus_id=corpus_id,
            source_root=str(root),
            adapter_source=adapter_source,
            health_state="indexing",
            health_reason="bootstrap running",
            bootstrap_job_id=job.job_id,
            created_at=binding.created_at,
        ),
    )
    if plan.errors:
        failed = replace(job, status="failed", ended_at=_now(), adapter_source=adapter_source)
        save_job(store, failed)
        return failed

    chunks_added = 0
    documents_ingested = 0
    if remote_adapter is not None:
        # Corpus lives on a remote AIAR (watchdog): ingest through the adapter;
        # AIAR chunks + embeds server-side. Nothing is copied into a local store.
        res = _ingest_into_remote(remote_adapter, corpus_id=corpus_id, root=root, plan=plan)
        enqueued = list(res["ingested"])
        duplicates = list(res["duplicates"])
        errors = list(res["errors"])
        chunks_added = int(res["chunks_added"])
        documents_ingested = len(enqueued) + len(duplicates)
    else:
        enqueued = []
        duplicates = []
        errors = list(plan.errors)
        for rel in plan.included:
            try:
                file_id, error = _copy_into_corpus(root, rel, corpus_id)
            except Exception as exc:
                errors.append(f"{rel}: {exc}")
                continue
            if error:
                errors.append(f"{rel}: {error}")
            elif file_id is None:
                duplicates.append(rel)
            else:
                enqueued.append(file_id)

    status = "failed" if errors else "done"
    done = replace(
        job,
        status=status,
        ended_at=_now(),
        enqueued=tuple(enqueued),
        duplicates=tuple(duplicates),
        errors=tuple(errors),
        adapter_source=adapter_source,
        documents_ingested=documents_ingested,
        chunks_added=chunks_added,
    )
    save_job(store, done)
    latest_binding = load_binding(store)
    if remote_adapter is not None:
        reason = (f"{documents_ingested} docs, {chunks_added} chunks ingested"
                  if not errors else "; ".join(errors[:3]))
    else:
        reason = (f"{len(enqueued)} files enqueued" if not errors
                  else "; ".join(errors[:3]))
    save_binding(
        store,
        replace(
            latest_binding,
            adapter_source=adapter_source,
            health_state="failed" if errors else "ready",
            health_reason=reason,
            last_refresh_at=_now() if not errors else latest_binding.last_refresh_at,
            index_version=latest_binding.index_version + (0 if errors else 1),
        ),
    )
    return done
