"""BUNDLE-IMPORT — restore a brief bundle (.tar.gz) into ~/.errorta/corpora/.

Sibling to :mod:`errorta_briefs.bundle`. Where ``build_bundle`` packs a brief +
its corpus snapshot into a portable tar.gz with a verification manifest, this
module unpacks one back into the canonical on-disk layout that
``errorta_briefs.runner._brief_dir_for`` and the routes layer expect.

Safety properties — the public API guarantees these or it raises and writes
nothing:

* Every file in the archive is SHA-256 verified against ``bundle-manifest.json``
  *before* any extraction commits to the target directory. Any mismatch raises
  with the offending archive path.
* Path-traversal and symlink members are rejected. Extraction prefers
  ``tarfile.extractall(filter='data')`` on Python 3.12+ and falls back to a
  manual validation pass on older runtimes — mirrors the pattern in
  :mod:`errorta_welcome.ingest_bridge`.
* On brief_id collision the function raises :class:`BriefAlreadyExists`.
  Callers may retry with ``rename_to=...`` to import under a different id.
* On any mid-extract failure both the temp staging dir and the (possibly
  partially-populated) target brief dir are removed before the exception
  escapes — no orphan ``brief.md`` left on disk.

Returns :class:`BriefImportResult` on success.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from errorta_export.safe_path import (
    UnsafePathError,  # noqa: F401 — re-exported for the route's except clause
    resolve_under_root,
    safe_segment,
)


_CHUNK = 4 * 1024 * 1024


class ImportError(Exception):
    """Raised for any bundle-import failure (corrupt tarball, sha mismatch, …)."""


class BriefAlreadyExists(Exception):
    """Raised when the resolved brief_id directory already exists on disk."""

    def __init__(self, brief_id: str, corpus_name: str) -> None:
        super().__init__(
            f'Brief with id "{brief_id}" already exists in corpus "{corpus_name}". '
            "Pass rename_to to import under a different id."
        )
        self.brief_id = brief_id
        self.corpus_name = corpus_name


class MarkdownInvalid(Exception):
    """Raised when the bundled brief.md fails parse_brief_markdown re-validation."""

    def __init__(self, message: str, errors: Optional[list[dict[str, Any]]] = None) -> None:
        super().__init__(message)
        self.errors = errors or []


@dataclass
class BriefImportResult:
    brief_id: str
    corpus_name: str
    files_imported: int
    warnings: list[str] = field(default_factory=list)
    timestamp_imported: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256_file(path: Path, *, chunk: int = _CHUNK) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def _find_bundle_root(tf: tarfile.TarFile) -> str:
    """Return the single top-level directory name inside the tarball.

    Bundles are produced by :func:`errorta_briefs.bundle.build_bundle` and have
    exactly one top-level dir named ``brief-{brief_id}-{ts}``. If the archive
    is malformed (no clear single root), the empty string is returned so the
    caller can fall through to "members live at archive root".
    """
    roots: set[str] = set()
    for m in tf.getmembers():
        head = m.name.split("/", 1)[0]
        if head:
            roots.add(head)
    if len(roots) == 1:
        return next(iter(roots))
    return ""


def _safe_extract(tf: tarfile.TarFile, dest: Path) -> None:
    """Extract ``tf`` into ``dest`` with traversal/symlink guards.

    Prefers ``tarfile.extractall(filter='data')`` on Python 3.12+ and falls
    back to manual member validation on older runtimes (rejects non-regular /
    non-directory members and any resolved path that escapes ``dest``).
    """
    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()
    for member in tf.getmembers():
        if member.issym() or member.islnk():
            raise ImportError(f"unsafe tar member (symlink): {member.name}")
        if not (member.isreg() or member.isdir()):
            raise ImportError(f"unsafe tar member type ({member.type!r}): {member.name}")
        member_path = (dest / member.name).resolve()
        try:
            member_path.relative_to(dest_resolved)
        except ValueError as exc:
            raise ImportError(f"unsafe tar member (path traversal): {member.name}") from exc

    try:
        tf.extractall(dest, filter="data")  # noqa: S202 — validated above
    except TypeError:
        tf.extractall(dest)  # noqa: S202 — validated above


def _read_bundle_manifest(staging_root: Path) -> dict[str, Any]:
    manifest_path = staging_root / "bundle-manifest.json"
    if not manifest_path.exists():
        raise ImportError("bundle-manifest.json missing from archive")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ImportError(f"bundle-manifest.json unreadable: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ImportError("bundle-manifest.json is not an object")
    if manifest.get("version") != 1:
        raise ImportError(
            f"unsupported bundle-manifest version: {manifest.get('version')!r}"
        )
    for key in ("brief_id", "files"):
        if key not in manifest:
            raise ImportError(f"bundle-manifest.json missing required key: {key}")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ImportError("bundle-manifest.json 'files' is not a list")
    for entry in files:
        if not isinstance(entry, dict):
            raise ImportError("bundle-manifest.json 'files' entry is not an object")
        if "path" not in entry or "sha256" not in entry:
            raise ImportError(
                "bundle-manifest.json file entry missing 'path' or 'sha256'"
            )
    return manifest


def _verify_shas(staging_root: Path, manifest: dict[str, Any]) -> None:
    """Stream-SHA-256 every manifest entry; raise on first mismatch."""
    for entry in manifest["files"]:
        rel = entry["path"]
        expected = entry["sha256"]
        # F086: reject absolute/traversal manifest keys before any filesystem
        # touch (a crafted key would otherwise hash + leak an out-of-tree file).
        on_disk = resolve_under_root(staging_root, rel)
        if not on_disk.exists() or not on_disk.is_file():
            raise ImportError(f"bundle entry missing on disk: {rel}")
        actual = _sha256_file(on_disk)
        if actual != expected:
            raise ImportError(
                f"sha256 mismatch for bundled file: {rel} "
                f"(expected {expected[:12]}…, got {actual[:12]}…)"
            )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def import_bundle(
    tar_path: Path,
    corpus_name: str,
    rename_to: Optional[str] = None,
    *,
    briefs_root: Optional[Path] = None,
) -> BriefImportResult:
    """Verify and import a brief bundle into ``~/.errorta/corpora/{corpus}/{brief_id}/``.

    Parameters
    ----------
    tar_path:
        Path to the ``.tar.gz`` bundle on disk.
    corpus_name:
        Name of the corpus we're importing into. Stored on the rewritten
        ``brief-manifest.json`` and used as the parent directory under
        ``corpus_root()`` (the runner / routes layer treats the brief dir
        itself as ``corpus_root()/{brief_id}/`` — see
        :func:`errorta_briefs.runner._brief_dir_for`).
    rename_to:
        Optional override for the effective brief_id. Used by the routes
        layer to retry an import after a 409 collision.
    briefs_root:
        Override for the brief root directory. Defaults to
        ``errorta_corpus.corpus_root()`` so tests can isolate via HOME.

    Returns
    -------
    :class:`BriefImportResult` summarising the import.
    """
    from errorta_briefs import parse_brief_markdown  # local import — avoid cycle
    from errorta_briefs.parser import BriefParseError
    from errorta_corpus import corpus_root
    from errorta_corpus.manifest import FileEntry, save_manifest

    tar_path = Path(tar_path)
    if not tar_path.exists() or not tar_path.is_file():
        raise ImportError(f"bundle tarball not found: {tar_path}")

    if briefs_root is None:
        briefs_root = corpus_root()

    warnings: list[str] = []

    # ------------------------------------------------------------------
    # Phase 1: extract to a temp dir, verify shas, then commit.
    # ------------------------------------------------------------------
    tmp_dir = Path(tempfile.mkdtemp(prefix="errorta-bundle-import-"))
    target_dir: Optional[Path] = None
    try:
        try:
            with tarfile.open(tar_path, mode="r:gz") as tf:
                _safe_extract(tf, tmp_dir)
                # Capture the single top-level dir name so the staging root is
                # unambiguous regardless of archive layout quirks.
                tf2_root = _find_bundle_root(tf)
        except ImportError:
            raise
        except tarfile.TarError as exc:
            raise ImportError(f"tarball unreadable: {exc}") from exc

        # The bundle convention is a single top-level "brief-{id}-{ts}" dir.
        staging_root = (tmp_dir / tf2_root) if tf2_root else tmp_dir
        if not staging_root.is_dir():
            # Tar may have created the dir at extraction time; fall back to tmp_dir.
            staging_root = tmp_dir

        manifest = _read_bundle_manifest(staging_root)
        _verify_shas(staging_root, manifest)

        original_brief_id = str(manifest["brief_id"])
        effective_brief_id = rename_to.strip() if rename_to and rename_to.strip() else original_brief_id

        # F086: effective_brief_id (from the manifest's brief_id OR the route's
        # rename_to query param) and corpus_name (route query param) are each
        # joined to a root as a single directory component below — validate them
        # so a ".." can't escape the briefs/corpus root.
        effective_brief_id = safe_segment(effective_brief_id)
        safe_segment(corpus_name)

        # ------------------------------------------------------------------
        # Phase 2: target collision check (before we touch the final dir).
        # ------------------------------------------------------------------
        target_dir = briefs_root / effective_brief_id
        if target_dir.exists():
            raise BriefAlreadyExists(effective_brief_id, corpus_name)

        target_dir.mkdir(parents=True, exist_ok=False)

        # ------------------------------------------------------------------
        # Phase 3: validate brief.md (after we've staked the target dir, but
        # before we materialise the rest of the layout). A failure here must
        # still clean up the empty target dir — handled in the except branch.
        # ------------------------------------------------------------------
        brief_md_src = staging_root / "brief.md"
        if not brief_md_src.exists():
            raise ImportError("bundle missing brief.md")
        markdown_text = brief_md_src.read_text(encoding="utf-8")
        try:
            parse_brief_markdown(markdown_text)
        except BriefParseError as exc:
            raise MarkdownInvalid(
                f"bundled brief.md failed re-validation: {exc.message}",
                errors=list(exc.errors),
            ) from exc

        files_imported = 0

        # brief.md
        shutil.copy2(brief_md_src, target_dir / "brief.md")
        files_imported += 1

        # Optional sidecar JSON files.
        for name in ("collect-state.json", "dedup-index.json", "run-extras.json"):
            p = staging_root / name
            if p.exists() and p.is_file():
                shutil.copy2(p, target_dir / name)
                files_imported += 1

        # run-logs/ tree (optional).
        run_logs_src = staging_root / "run-logs"
        if run_logs_src.exists() and run_logs_src.is_dir():
            run_logs_dst = target_dir / "run-logs"
            run_logs_dst.mkdir(parents=True, exist_ok=True)
            for entry in run_logs_src.rglob("*"):
                if entry.is_file():
                    rel = entry.relative_to(run_logs_src)
                    dst = run_logs_dst / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(entry, dst)
                    files_imported += 1
        else:
            (target_dir / "run-logs").mkdir(parents=True, exist_ok=True)

        # Corpus files (under corpus/files/* in the archive) are mirrored into
        # the brief dir at files/* — same layout the live runner writes. We
        # track basename → new absolute path so we can rewrite copied_path in
        # the corpus manifest below.
        copied_payload: dict[str, Path] = {}
        corpus_files_src = staging_root / "corpus" / "files"
        if corpus_files_src.exists() and corpus_files_src.is_dir():
            files_dst = target_dir / "files"
            files_dst.mkdir(parents=True, exist_ok=True)
            for entry in corpus_files_src.iterdir():
                if entry.is_file():
                    dst = files_dst / entry.name
                    shutil.copy2(entry, dst)
                    copied_payload[entry.name] = dst.resolve()
                    files_imported += 1

        # ------------------------------------------------------------------
        # Phase 4: rewrite brief-manifest.json to a DRAFT state with the new
        # effective brief_id + corpus_name.
        # ------------------------------------------------------------------
        now_iso = datetime.now(timezone.utc).isoformat()
        new_brief_manifest = {
            "brief_id": effective_brief_id,
            "corpus_name": corpus_name,
            "state": "DRAFT",
            "created_at": now_iso,
            "last_run_at": None,
            "runs": [],
        }
        manifest_path = target_dir / "brief-manifest.json"
        tmp_manifest = manifest_path.with_suffix(".json.tmp")
        tmp_manifest.write_text(
            json.dumps(new_brief_manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp_manifest.replace(manifest_path)
        # brief-manifest.json counts as an imported file too.
        files_imported += 1

        # ------------------------------------------------------------------
        # Phase 5: if a corpus manifest snapshot rode along, rewrite each
        # FileEntry.copied_path to its new absolute location and call
        # save_manifest atomically.
        # ------------------------------------------------------------------
        corpus_manifest_src = staging_root / "corpus-manifest.json"
        if corpus_manifest_src.exists() and corpus_manifest_src.is_file():
            try:
                raw = json.loads(corpus_manifest_src.read_text(encoding="utf-8"))
            except Exception as exc:
                warnings.append(f"corpus-manifest.json unreadable, skipped: {exc}")
            else:
                new_entries: dict[str, FileEntry] = {}
                for fid, entry in (raw.get("files") or {}).items():
                    if not isinstance(entry, dict):
                        continue
                    old_cp = entry.get("copied_path") or ""
                    base = Path(old_cp).name if old_cp else ""
                    new_cp = copied_payload.get(base)
                    payload = dict(entry)
                    if new_cp is not None:
                        payload["copied_path"] = str(new_cp)
                    # Filter to FileEntry-known fields (drop unknown extras to
                    # match the dataclass constructor signature).
                    allowed = {f for f in FileEntry.__dataclass_fields__.keys()}
                    payload = {k: v for k, v in payload.items() if k in allowed}
                    try:
                        new_entries[fid] = FileEntry(**payload)
                    except TypeError as exc:
                        warnings.append(
                            f"corpus manifest entry {fid!r} skipped: {exc}"
                        )
                if new_entries:
                    save_manifest(corpus_name, new_entries)

        return BriefImportResult(
            brief_id=effective_brief_id,
            corpus_name=corpus_name,
            files_imported=files_imported,
            warnings=warnings,
            timestamp_imported=now_iso,
        )
    except BaseException:
        # Atomic cleanup: never leave a half-populated brief dir on disk.
        if target_dir is not None:
            try:
                if target_dir.exists():
                    shutil.rmtree(target_dir, ignore_errors=True)
            except OSError:
                pass
        raise
    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except OSError:
            pass
