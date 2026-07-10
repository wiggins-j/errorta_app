"""F008e — brief-driven corpus collection router.

Mounted at ``/briefs`` by ``errorta_app.server``. Implements the 10 endpoints
declared in §8 of the F008 spec. Briefs are stored on disk under
``~/.errorta/corpora/{corpus-slug}/``.

All routes are localhost-only by sidecar policy; no auth surface here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from errorta_briefs import (
    BriefConfig,
    BriefParseError,
    CONNECTOR_REGISTRY,
    parse_brief_markdown,
)
from errorta_briefs.lifecycle import BriefState
from errorta_briefs.runner import (
    BriefRunner,
    current_run,
    load_collect_state,
    load_run_extras,
)
from errorta_briefs.bundle import BundleError, build_bundle
from errorta_briefs.bundle_import import (
    BriefAlreadyExists,
    ImportError as BundleImportError,
    MarkdownInvalid,
    import_bundle,
)
from errorta_export.safe_path import UnsafePathError
from errorta_corpus import corpus_root
from errorta_corpus.pipeline import event_stream

from ._residency_proxy import refuse_local_dataplane_if_remote

router = APIRouter(prefix="/briefs", tags=["briefs"])


# ---------------------------------------------------------------------------
# Template library (F014-LIB)
# ---------------------------------------------------------------------------


# Resolve the example briefs directory relative to the repo root. The routes
# module lives at python/errorta_app/routes/briefs.py — the docs tree is
# four parents up at <repo>/docs/examples/briefs. The lookup tolerates a
# missing directory (returns []) so dev installs without the docs tree
# still respond cleanly.
_TEMPLATES_DIR = (
    Path(__file__).resolve().parents[3] / "docs" / "examples" / "briefs"
)

# How many characters of the markdown to surface as a short preview. The full
# body is also returned (see ``BriefTemplateOut.markdown``); the preview is a
# convenience for card-only UIs that don't need the full body. Bumped use of
# the full body fixed F014-LIB silent truncation when a picked template's
# frontmatter or body exceeded this cap.
_TEMPLATE_PREVIEW_CHARS = 600


def _templates_dir() -> Path:
    """Return the on-disk directory the templates endpoint scans.

    Overridable via the ``ERRORTA_BRIEF_TEMPLATES_DIR`` environment variable
    so tests can point at a temporary directory without monkey-patching.
    """
    override = os.environ.get("ERRORTA_BRIEF_TEMPLATES_DIR")
    if override:
        return Path(override)
    return _TEMPLATES_DIR


def _derive_template_title(markdown: str, fallback: str) -> str:
    """Pull a human-readable title out of the brief body.

    Preference order: first ``# Heading`` line in the markdown body, then the
    front-matter ``project`` field (if parseable), then the fallback filename.
    """
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip() or fallback
    try:
        config, _ = parse_brief_markdown(markdown)
        return config.project
    except BriefParseError:
        return fallback


def _derive_template_description(markdown: str) -> str:
    """Best-effort description string for a template card.

    Uses the front-matter ``description`` field when present. Falls back to
    an empty string when the brief either fails to parse or omits the field.
    """
    try:
        config, _ = parse_brief_markdown(markdown)
        return config.description or ""
    except BriefParseError:
        return ""


class BriefTemplateOut(BaseModel):
    id: str
    title: str
    description: str
    # Full markdown body of the example brief. Returned so the picker UI
    # can seed its textarea with the complete file rather than a truncated
    # preview (F014-LIB fix — previously only ``markdown_preview`` was
    # returned, which silently dropped any content past 600 chars).
    markdown: str
    markdown_preview: str
    mtime: float


@router.get("/templates", response_model=list[BriefTemplateOut])
def list_brief_templates() -> list[BriefTemplateOut]:
    """Scan ``docs/examples/briefs/`` and return template summaries.

    Each ``*.md`` file is surfaced as one entry with a stable ``id``
    (the filename stem), a derived title and description, a bounded
    preview of the markdown body, and the file's mtime so the UI can
    cache-bust safely.
    """
    out: list[BriefTemplateOut] = []
    root = _templates_dir()
    if not root.exists() or not root.is_dir():
        return out
    for path in sorted(root.glob("*.md")):
        try:
            markdown = path.read_text(encoding="utf-8")
        except OSError:
            continue
        stem = path.stem
        title = _derive_template_title(markdown, fallback=stem)
        description = _derive_template_description(markdown)
        preview = markdown[:_TEMPLATE_PREVIEW_CHARS]
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        out.append(
            BriefTemplateOut(
                id=stem,
                title=title,
                description=description,
                markdown=markdown,
                markdown_preview=preview,
                mtime=mtime,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class BriefOut(BaseModel):
    brief_id: str
    corpus_name: str
    state: str
    created_at: str
    last_run_at: Optional[str] = None


class SourceStatusOut(BaseModel):
    name: str
    state: str
    docs_collected: int
    docs_refused: int
    page_or_offset: int
    last_canonical_id: Optional[str] = None
    last_error: Optional[str] = None
    corpus_file_ids: list[str] = Field(default_factory=list)


class LiveStatusOut(BaseModel):
    brief_id: str
    run_id: Optional[str] = None
    state: str
    per_source: list[SourceStatusOut] = Field(default_factory=list)
    compliance_refusals: list[dict[str, Any]] = Field(default_factory=list)
    failures: list[dict[str, Any]] = Field(default_factory=list)
    ingested_count: int = 0


class RunOut(BaseModel):
    run_id: str
    brief_id: str
    state: str


class ValidateOut(BaseModel):
    ok: bool
    errors: list[dict[str, Any]] = Field(default_factory=list)
    connectors: dict[str, dict[str, Any]] = Field(default_factory=dict)
    dry_run_projection: Optional[dict[str, dict[str, Any]]] = None


# ---------------------------------------------------------------------------
# On-disk helpers
# ---------------------------------------------------------------------------


def _briefs_root() -> Path:
    return corpus_root()


def _brief_dir(brief_id: str) -> Path:
    # brief_id is the corpus slug (BriefConfig.corpus). We resolve via root/<id>.
    return _briefs_root() / brief_id


def _brief_md_path(brief_id: str) -> Path:
    return _brief_dir(brief_id) / "brief.md"


def _brief_manifest_path(brief_id: str) -> Path:
    return _brief_dir(brief_id) / "brief-manifest.json"


def _load_manifest(brief_id: str) -> dict[str, Any]:
    path = _brief_manifest_path(brief_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"brief '{brief_id}' not found")
    return json.loads(path.read_text(encoding="utf-8"))


def _save_manifest(brief_id: str, manifest: dict[str, Any]) -> None:
    path = _brief_manifest_path(brief_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


_HISTORY_TIMESTAMP_RE = re.compile(r"^[0-9T.Z\-]+$")


def _brief_history_dir(brief_id: str) -> Path:
    """Return the per-brief history directory under the corpus root."""
    return _brief_dir(brief_id) / "brief-history"


def _snapshot_brief(brief_id: str, old_markdown: str) -> str:
    """Persist ``old_markdown`` as a timestamped snapshot. Returns the timestamp.

    Filesystem-safe timestamp format (no colons) — ``%Y-%m-%dT%H%M%S.%fZ``.
    Uses an atomic write (temp file + os.replace) so a crashed sidecar can't
    leave a partial snapshot.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S.%fZ")
    hist_dir = _brief_history_dir(brief_id)
    hist_dir.mkdir(parents=True, exist_ok=True)
    dest = hist_dir / f"{timestamp}.md"
    tmp = dest.with_suffix(".md.tmp")
    tmp.write_text(old_markdown, encoding="utf-8")
    os.replace(tmp, dest)
    return timestamp


def _read_brief_md(brief_id: str) -> str:
    path = _brief_md_path(brief_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"brief '{brief_id}' not found")
    return path.read_text(encoding="utf-8")


def _parse_or_400(markdown: str) -> tuple[BriefConfig, str]:
    try:
        return parse_brief_markdown(markdown)
    except BriefParseError as exc:
        raise HTTPException(
            status_code=400,
            detail={"message": exc.message, "errors": exc.errors},
        ) from exc


# ---------------------------------------------------------------------------
# Endpoints (10 per spec §8)
# ---------------------------------------------------------------------------


class CreateBriefIn(BaseModel):
    markdown: str


@router.get("", response_model=list[BriefOut])
def list_briefs() -> list[BriefOut]:
    out: list[BriefOut] = []
    root = _briefs_root()
    if not root.exists():
        return out
    for d in sorted(root.iterdir()):
        manifest_path = d / "brief-manifest.json"
        if not manifest_path.exists():
            continue
        try:
            m = json.loads(manifest_path.read_text(encoding="utf-8"))
            out.append(
                BriefOut(
                    brief_id=m["brief_id"],
                    corpus_name=m["corpus_name"],
                    state=m.get("state", BriefState.DRAFT.value),
                    created_at=m["created_at"],
                    last_run_at=m.get("last_run_at"),
                )
            )
        except Exception:
            continue
    return out


@router.post("", response_model=BriefOut, status_code=201)
def create_brief(body: CreateBriefIn) -> BriefOut:
    config, _ = _parse_or_400(body.markdown)
    brief_id = config.corpus
    brief_dir = _brief_dir(brief_id)
    if (_brief_manifest_path(brief_id)).exists():
        raise HTTPException(status_code=409, detail=f"brief '{brief_id}' already exists")
    brief_dir.mkdir(parents=True, exist_ok=True)
    (brief_dir / "run-logs").mkdir(parents=True, exist_ok=True)
    _brief_md_path(brief_id).write_text(body.markdown, encoding="utf-8")
    now = datetime.now(timezone.utc).isoformat()
    manifest = {
        "brief_id": brief_id,
        "corpus_name": config.corpus,
        "project": config.project,
        "state": BriefState.DRAFT.value,
        "created_at": now,
        "last_run_at": None,
        "runs": [],
    }
    _save_manifest(brief_id, manifest)
    return BriefOut(
        brief_id=brief_id,
        corpus_name=config.corpus,
        state=BriefState.DRAFT.value,
        created_at=now,
    )


@router.get("/{brief_id}")
def get_brief(brief_id: str) -> dict[str, Any]:
    manifest = _load_manifest(brief_id)
    markdown = _read_brief_md(brief_id)
    try:
        config, body = parse_brief_markdown(markdown)
        parsed = config.model_dump()
    except BriefParseError as exc:
        parsed = None
        body = ""
        manifest = {**manifest, "parse_errors": exc.errors}
    return {
        "manifest": manifest,
        "markdown": markdown,
        "config": parsed,
        "body": body,
    }


class UpdateBriefIn(BaseModel):
    markdown: str


@router.put("/{brief_id}", response_model=BriefOut)
def update_brief(brief_id: str, body: UpdateBriefIn) -> BriefOut:
    manifest = _load_manifest(brief_id)
    config, _ = _parse_or_400(body.markdown)
    if config.corpus != brief_id:
        raise HTTPException(
            status_code=400,
            detail=f"corpus slug change not supported (path={brief_id}, body={config.corpus})",
        )
    md_path = _brief_md_path(brief_id)
    # F008-HISTORY — snapshot the prior on-disk markdown before overwrite. First-
    # time create (no file present) skips the snapshot so the very first PUT does
    # not produce a "previous version" of nothing.
    if md_path.exists():
        try:
            old_markdown = md_path.read_text(encoding="utf-8")
        except OSError:
            old_markdown = None
        if old_markdown is not None:
            try:
                _snapshot_brief(brief_id, old_markdown)
            except OSError:
                # History is a best-effort log; a failed snapshot should not
                # block the user's edit from being saved.
                pass
    md_path.write_text(body.markdown, encoding="utf-8")
    manifest["state"] = BriefState.DRAFT.value
    _save_manifest(brief_id, manifest)
    return BriefOut(
        brief_id=brief_id,
        corpus_name=config.corpus,
        state=manifest["state"],
        created_at=manifest["created_at"],
        last_run_at=manifest.get("last_run_at"),
    )


# ---------------------------------------------------------------------------
# F008-HISTORY — per-brief edit history (timestamped markdown snapshots)
# ---------------------------------------------------------------------------


class BriefHistoryEntry(BaseModel):
    timestamp: str
    byte_size: int
    sha256: str


@router.get("/{brief_id}/history", response_model=list[BriefHistoryEntry])
def list_brief_history(brief_id: str) -> list[BriefHistoryEntry]:
    """Return descending-timestamp list of snapshots; [] when none exist.

    Returns ``[]`` (not 404) when the history directory is missing or empty —
    the brief itself may exist without ever having been edited.
    """
    # Validate the brief exists to surface a clean 404 for unknown ids.
    if not _brief_manifest_path(brief_id).exists():
        raise HTTPException(status_code=404, detail=f"brief '{brief_id}' not found")
    hist_dir = _brief_history_dir(brief_id)
    if not hist_dir.exists() or not hist_dir.is_dir():
        return []
    entries: list[BriefHistoryEntry] = []
    for p in hist_dir.glob("*.md"):
        try:
            data = p.read_bytes()
        except OSError:
            continue
        entries.append(
            BriefHistoryEntry(
                timestamp=p.stem,
                byte_size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        )
    entries.sort(key=lambda e: e.timestamp, reverse=True)
    return entries


@router.get("/{brief_id}/history/{timestamp}")
def get_brief_history_snapshot(brief_id: str, timestamp: str) -> Any:
    """Return the markdown body of a single snapshot as text/markdown.

    The timestamp is restricted to ``^[0-9T.Z-]+$`` so a malicious caller
    cannot smuggle ``..`` or slashes through the path parameter (FastAPI
    decodes percent-escapes before our handler runs).
    """
    from fastapi.responses import PlainTextResponse

    if not _brief_manifest_path(brief_id).exists():
        raise HTTPException(status_code=404, detail=f"brief '{brief_id}' not found")
    if not _HISTORY_TIMESTAMP_RE.match(timestamp):
        raise HTTPException(status_code=400, detail="invalid timestamp")
    snapshot_path = _brief_history_dir(brief_id) / f"{timestamp}.md"
    # Defence in depth: resolve and confirm the resulting path is still inside
    # the brief's history directory (guards against unicode normalisation
    # surprises slipping past the regex above).
    try:
        resolved = snapshot_path.resolve()
        hist_root = _brief_history_dir(brief_id).resolve()
    except OSError:
        raise HTTPException(status_code=404, detail="snapshot not found")
    if not str(resolved).startswith(str(hist_root)):
        raise HTTPException(status_code=400, detail="invalid timestamp")
    if not snapshot_path.exists() or not snapshot_path.is_file():
        raise HTTPException(status_code=404, detail="snapshot not found")
    return PlainTextResponse(
        snapshot_path.read_text(encoding="utf-8"),
        media_type="text/markdown; charset=utf-8",
    )


@router.post("/{brief_id}/history/{timestamp}/restore", response_model=BriefOut)
def restore_brief_history_snapshot(brief_id: str, timestamp: str) -> BriefOut:
    """Restore ``brief.md`` to a prior snapshot's content.

    Flow:
      1. 404 if the brief manifest is missing.
      2. 400 if the ``timestamp`` doesn't match ``_HISTORY_TIMESTAMP_RE``.
      3. 404 if the snapshot file is missing (after path-traversal guard).
      4. 400 if the snapshot's markdown fails ``parse_brief_markdown``; no
         disk mutation occurs in that case.
      5. Snapshot the CURRENT ``brief.md`` into history (so the restore itself
         is undoable by walking history again).
      6. Atomic write the snapshot content into ``brief.md``.
      7. Set manifest ``state`` to DRAFT; preserve ``created_at``,
         ``last_run_at``, and ``runs[]`` untouched.
    """
    # 1) manifest must exist
    manifest = _load_manifest(brief_id)

    # 2) timestamp shape
    if not _HISTORY_TIMESTAMP_RE.match(timestamp):
        raise HTTPException(status_code=400, detail="invalid timestamp")

    # 3) path-traversal guard + missing snapshot 404
    snapshot_path = _brief_history_dir(brief_id) / f"{timestamp}.md"
    try:
        resolved = snapshot_path.resolve()
        hist_root = _brief_history_dir(brief_id).resolve()
    except OSError:
        raise HTTPException(status_code=404, detail="snapshot not found")
    if not str(resolved).startswith(str(hist_root)):
        raise HTTPException(status_code=400, detail="invalid timestamp")
    if not snapshot_path.exists() or not snapshot_path.is_file():
        raise HTTPException(status_code=404, detail="snapshot not found")

    # 4) parse-validate the snapshot BEFORE mutating disk
    snapshot_markdown = snapshot_path.read_text(encoding="utf-8")
    _parse_or_400(snapshot_markdown)

    # 5) snapshot the CURRENT brief.md so the restore itself enters history
    md_path = _brief_md_path(brief_id)
    if md_path.exists():
        try:
            current_markdown = md_path.read_text(encoding="utf-8")
        except OSError:
            current_markdown = None
        if current_markdown is not None:
            try:
                _snapshot_brief(brief_id, current_markdown)
            except OSError:
                # Best-effort — never block restore on a snapshot-write failure.
                pass

    # 6) atomic write snapshot content to brief.md
    md_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = md_path.with_suffix(".md.tmp")
    tmp.write_text(snapshot_markdown, encoding="utf-8")
    os.replace(tmp, md_path)

    # 7) flip state to DRAFT; preserve created_at / last_run_at / runs[]
    manifest["state"] = BriefState.DRAFT.value
    _save_manifest(brief_id, manifest)

    return BriefOut(
        brief_id=brief_id,
        corpus_name=manifest.get("corpus_name", brief_id),
        state=manifest["state"],
        created_at=manifest["created_at"],
        last_run_at=manifest.get("last_run_at"),
    )


@router.delete("/{brief_id}")
def delete_brief(brief_id: str) -> dict[str, Any]:
    brief_dir = _brief_dir(brief_id)
    if not (_brief_manifest_path(brief_id)).exists():
        raise HTTPException(status_code=404, detail=f"brief '{brief_id}' not found")
    # delete brief.md + manifest + state files; preserve docs/ for user inspection.
    for name in ("brief.md", "brief-manifest.json", "collect-state.json", "dedup-index.json"):
        p = brief_dir / name
        if p.exists():
            p.unlink()
    return {"brief_id": brief_id, "deleted": True}


def _validate_config(config: BriefConfig, *, dry_run: bool) -> dict[str, Any]:
    """Shared validation core for /validate and /validate-markdown.

    Walks the parsed ``BriefConfig``, instantiates each connector via
    ``CONNECTOR_REGISTRY``, and (when ``dry_run`` is True) samples each source
    through the ``ComplianceGate`` to project per-source acceptance counts.

    Returns a dict with keys:
      * ``ok`` (bool) — true when every connector instantiated and reported ok.
      * ``errors`` (list) — empty here; parse errors are reported by callers.
      * ``connectors`` (dict[name -> status]) — per-source connector status.
      * ``compliance_projection`` (dict | None) — dry-run sample projection.
      * ``parsed`` (dict) — the parsed config (``BriefConfig.model_dump()``).
    """
    connectors: dict[str, dict[str, Any]] = {}
    instances: dict[str, Any] = {}
    overall_ok = True
    for s in config.sources:
        cls = CONNECTOR_REGISTRY.get(s.name)
        if cls is None:
            connectors[s.name] = {"ok": False, "reason": "unknown connector"}
            overall_ok = False
            continue
        try:
            inst = cls(s.config)
            status = inst.status()
        except Exception as exc:
            connectors[s.name] = {"ok": False, "reason": str(exc)}
            overall_ok = False
            continue
        connectors[s.name] = status
        instances[s.name] = inst
        if not status.get("ok", False):
            overall_ok = False

    compliance_projection: Optional[dict[str, dict[str, Any]]] = None
    if dry_run:
        from errorta_briefs.compliance import ComplianceGate
        from errorta_briefs.dryrun import dry_run_sample_source

        gate = ComplianceGate()
        compliance_projection = {}
        for s in config.sources:
            inst = instances.get(s.name)
            connector_name = type(inst).__name__ if inst is not None else s.name
            candidates_seen = 0
            compliance_pass = 0
            compliance_refused = 0
            sample_refusal_reasons: list[str] = []
            if inst is not None:
                try:
                    for _doc, ok, reason in dry_run_sample_source(inst, s, gate, sample_limit=5):
                        candidates_seen += 1
                        if ok:
                            compliance_pass += 1
                        else:
                            compliance_refused += 1
                            if reason is not None:
                                sample_refusal_reasons.append(reason)
                except Exception as exc:
                    sample_refusal_reasons.append(f"connector error: {exc}")
            compliance_projection[s.name] = {
                "connector_name": connector_name,
                "candidates_seen": candidates_seen,
                "compliance_pass": compliance_pass,
                "compliance_refused": compliance_refused,
                "sample_refusal_reasons": sample_refusal_reasons,
            }
    return {
        "ok": overall_ok,
        "errors": [],
        "connectors": connectors,
        "compliance_projection": compliance_projection,
        "parsed": config.model_dump(),
    }


@router.post("/{brief_id}/validate", response_model=ValidateOut)
def validate_brief(brief_id: str, dry_run: bool = False) -> ValidateOut:
    markdown = _read_brief_md(brief_id)
    try:
        config, _ = parse_brief_markdown(markdown)
    except BriefParseError as exc:
        return ValidateOut(ok=False, errors=exc.errors)
    result = _validate_config(config, dry_run=dry_run)
    # Preserve the existing wire shape for /{id}/validate: it returns the
    # projection under ``dry_run_projection`` (not ``compliance_projection``).
    return ValidateOut(
        ok=result["ok"],
        errors=result["errors"],
        connectors=result["connectors"],
        dry_run_projection=result["compliance_projection"],
    )


class ValidateMarkdownIn(BaseModel):
    markdown: str
    dry_run: bool = False


class ValidateMarkdownOut(BaseModel):
    ok: bool
    errors: list[dict[str, Any]] = Field(default_factory=list)
    connectors: dict[str, dict[str, Any]] = Field(default_factory=dict)
    compliance_projection: Optional[dict[str, dict[str, Any]]] = None
    parsed: Optional[dict[str, Any]] = None


@router.post("/validate-markdown", response_model=ValidateMarkdownOut)
def validate_markdown(body: ValidateMarkdownIn) -> ValidateMarkdownOut:
    """Stateless validation entry point — no disk reads, no persistence.

    Used by the import path to pre-flight a candidate brief markdown blob
    before persisting it through ``POST /briefs``. Parse failures come back
    as ``ok=false`` with a populated ``errors`` array (200 OK, not 422 — the
    import path needs a clean ok/errors contract).
    """
    try:
        config, _ = parse_brief_markdown(body.markdown)
    except BriefParseError as exc:
        errors = list(exc.errors) if exc.errors else [{"msg": exc.message}]
        return ValidateMarkdownOut(ok=False, errors=errors)
    result = _validate_config(config, dry_run=body.dry_run)
    return ValidateMarkdownOut(
        ok=result["ok"],
        errors=result["errors"],
        connectors=result["connectors"],
        compliance_projection=result["compliance_projection"],
        parsed=result["parsed"],
    )


def _start_run(brief_id: str) -> RunOut:
    # F086 Slice E: a brief collect materializes a corpus on local disk; refuse
    # under remote residency rather than silently building it on the laptop.
    refuse_local_dataplane_if_remote(f"/briefs/{brief_id}/run")
    markdown = _read_brief_md(brief_id)
    config, _ = _parse_or_400(markdown)
    manifest = _load_manifest(brief_id)
    runner = BriefRunner()
    try:
        run_id = runner.submit(brief_id, config, _briefs_root())
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    manifest["state"] = BriefState.RUNNING.value
    manifest["last_run_at"] = datetime.now(timezone.utc).isoformat()
    manifest.setdefault("runs", []).append(
        {
            "run_id": run_id,
            "started_at": manifest["last_run_at"],
            "outcome": "running",
        }
    )
    _save_manifest(brief_id, manifest)
    return RunOut(run_id=run_id, brief_id=brief_id, state=BriefState.RUNNING.value)


@router.post("/{brief_id}/run", response_model=RunOut)
def run_brief(brief_id: str) -> RunOut:
    return _start_run(brief_id)


@router.post("/{brief_id}/start", response_model=RunOut)
def start_brief(brief_id: str) -> RunOut:
    """Alias of /run — accepted in acceptance tests."""
    return _start_run(brief_id)


@router.post("/{brief_id}/refresh", response_model=RunOut)
def refresh_brief(brief_id: str) -> RunOut:
    """Refresh re-runs against the existing corpus; for v0.3 it shares /run semantics."""
    return _start_run(brief_id)


@router.post("/{brief_id}/pause")
def pause_brief(brief_id: str) -> dict[str, Any]:
    active_bid, run_id, runner = current_run()
    if runner is None or active_bid != brief_id:
        raise HTTPException(status_code=409, detail="no active run for this brief")
    runner.pause()
    return {"brief_id": brief_id, "run_id": run_id, "paused": True}


def _snapshot_status(brief_id: str) -> LiveStatusOut:
    brief_dir = _brief_dir(brief_id)
    if not _brief_manifest_path(brief_id).exists():
        raise HTTPException(status_code=404, detail=f"brief '{brief_id}' not found")
    manifest = _load_manifest(brief_id)
    cs = load_collect_state(brief_dir)
    if cs is None:
        return LiveStatusOut(
            brief_id=brief_id,
            run_id=None,
            state=manifest.get("state", BriefState.DRAFT.value),
            per_source=[],
        )
    extras = load_run_extras(brief_dir)
    per_source = []
    for name, ps in cs.per_source.items():
        se = extras.per_source.get(name)
        per_source.append(
            SourceStatusOut(
                name=name,
                state=ps.state,
                docs_collected=ps.docs_ingested_to_corpus,
                docs_refused=se.docs_refused if se else 0,
                page_or_offset=ps.page_or_offset or 0,
                last_canonical_id=se.last_canonical_id if se else None,
                last_error=se.last_error if se else None,
                corpus_file_ids=list(se.corpus_file_ids) if se else [],
            )
        )
    state_value = cs.state.value if isinstance(cs.state, BriefState) else str(cs.state)
    return LiveStatusOut(
        brief_id=brief_id,
        run_id=cs.run_id,
        state=state_value,
        per_source=per_source,
        compliance_refusals=list(extras.compliance_refusals),
        failures=[
            {
                "error_class": f.error_class,
                "message": f.message,
                "occurred_at": f.occurred_at,
                "retry_count": f.retry_count,
            }
            for f in cs.failures
        ],
        ingested_count=len(extras.ingested_canonical_ids),
    )


@router.get("/{brief_id}/status")
def status_brief(brief_id: str, request: Request):
    """Return SSE stream when Accept: text/event-stream, else a JSON snapshot."""
    accept = request.headers.get("accept", "")
    if "text/event-stream" in accept.lower():
        return StreamingResponse(event_stream(), media_type="text/event-stream")
    return _snapshot_status(brief_id)


# ---------------------------------------------------------------------------
# F008-BUNDLE — portable .tar.gz export
# ---------------------------------------------------------------------------


class ExportBundleRequest(BaseModel):
    target_dir: str
    dry_run: bool = False


def _safe_target_dir(target_dir: str) -> Path:
    """Resolve and validate a caller-supplied bundle destination directory.

    Refuses traversal-style relative components and requires the directory to
    exist and be writable. Returns the resolved absolute Path.
    """
    if not target_dir or ".." in Path(target_dir).parts:
        raise HTTPException(status_code=400, detail="invalid target_dir")
    p = Path(target_dir).expanduser().resolve()
    if not p.exists() or not p.is_dir():
        raise HTTPException(status_code=400, detail=f"target_dir does not exist: {p}")
    if not os.access(str(p), os.W_OK):
        raise HTTPException(status_code=400, detail=f"target_dir not writable: {p}")
    return p


@router.post("/{brief_id}/export-bundle")
def export_bundle_brief(
    brief_id: str,
    body: ExportBundleRequest,
) -> StreamingResponse:
    """Stream a brief-bundle build as SSE.

    Event sequence (happy path):
        hello -> phase:planning -> file* -> phase:packaging -> phase:verifying -> done

    Errors emit an ``error`` event then close the stream; HTTP status is 200
    for parity with ``/export/run`` (the SSE error payload carries the failure
    message).
    """
    # Validate target_dir up-front so we can return a fast 4xx instead of an
    # SSE error when the caller misuses the API.
    safe_dir = _safe_target_dir(body.target_dir)

    # Verify brief exists before opening the stream — gives a clean 404 for
    # unknown ids and guarantees no partial tar.gz lands on disk.
    if not _brief_manifest_path(brief_id).exists():
        raise HTTPException(status_code=404, detail=f"brief '{brief_id}' not found")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest_path = safe_dir / f"{brief_id}-{timestamp}.tar.gz"

    def _gen():
        from queue import Empty, Queue
        from threading import Thread

        q: Queue = Queue()

        def _emit(event: str, payload: dict[str, Any]) -> None:
            q.put((event, payload))

        def _runner() -> None:
            try:
                def _progress(evt: str, payload: dict[str, Any]) -> None:
                    if evt in ("planning", "packaging", "verifying"):
                        _emit("phase", {"phase": evt})
                    elif evt == "file":
                        _emit("file", payload)
                    elif evt == "done":
                        _emit(
                            "done",
                            {
                                "dest_path": str(dest_path),
                                "sha256_hex": payload.get("sha256_hex", ""),
                                "file_count": payload.get("file_count", 0),
                                "total_size_bytes": payload.get("total_size_bytes", 0),
                                "dry_run": bool(payload.get("dry_run", False)),
                            },
                        )

                build_bundle(
                    brief_id,
                    dest_path,
                    dry_run=body.dry_run,
                    progress=_progress,
                )
            except BundleError as exc:
                _emit("error", {"message": str(exc)})
            except Exception as exc:  # pragma: no cover - defensive
                _emit("error", {"message": f"{type(exc).__name__}: {exc}"})
            finally:
                _emit("__end__", {})

        t = Thread(target=_runner, daemon=True)
        t.start()

        yield "event: hello\ndata: {}\n\n"

        while True:
            try:
                event, payload = q.get(timeout=30.0)
            except Empty:
                # Heartbeat comment frame keeps long builds alive.
                yield ": keepalive\n\n"
                continue
            if event == "__end__":
                break
            yield f"event: {event}\ndata: {json.dumps(payload)}\n\n"
            if event == "error":
                break

    return StreamingResponse(_gen(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# BUNDLE-IMPORT — restore a brief bundle from a .tar.gz upload
# ---------------------------------------------------------------------------


class BriefImportResultOut(BaseModel):
    brief_id: str
    corpus_name: str
    files_imported: int
    warnings: list[str] = Field(default_factory=list)
    timestamp_imported: str


@router.post("/import-bundle", response_model=BriefImportResultOut)
async def import_bundle_route(
    tarball: UploadFile = File(...),
    corpus_name: str = "default",
    rename_to: Optional[str] = None,
) -> BriefImportResultOut:
    """Accept a multipart upload of a brief bundle (.tar.gz) and restore it.

    409 on brief_id collision (caller may retry with ``rename_to``).
    400 on corrupt tar / sha mismatch / unsafe members.
    422 on bundled brief.md re-validation failure.
    """
    refuse_local_dataplane_if_remote("/briefs/import-bundle")
    # Stream the upload into a temp file we can hand to import_bundle().
    import tempfile

    suffix = ".tar.gz"
    fd, tmp_name = tempfile.mkstemp(prefix="errorta-import-", suffix=suffix)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out_fh:
            while True:
                chunk = await tarball.read(1024 * 1024)
                if not chunk:
                    break
                out_fh.write(chunk)
        try:
            result = import_bundle(
                tmp_path,
                corpus_name=corpus_name,
                rename_to=rename_to,
            )
        except BriefAlreadyExists as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "already_exists",
                    "message": str(exc),
                    "brief_id": exc.brief_id,
                    "corpus_name": exc.corpus_name,
                    "rename_to_hint": "Pass rename_to=<new-id> to retry under a different id.",
                },
            ) from exc
        except MarkdownInvalid as exc:
            raise HTTPException(
                status_code=422,
                detail={"message": str(exc), "errors": exc.errors},
            ) from exc
        except UnsafePathError as exc:
            # F086: crafted brief_id / corpus_name / manifest key tried to
            # escape the briefs/corpus root — return the key only, no oracle.
            raise HTTPException(
                status_code=400,
                detail={"code": exc.code, "key": exc.key},
            ) from exc
        except BundleImportError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass

    return BriefImportResultOut(
        brief_id=result.brief_id,
        corpus_name=result.corpus_name,
        files_imported=result.files_imported,
        warnings=result.warnings,
        timestamp_imported=result.timestamp_imported,
    )
