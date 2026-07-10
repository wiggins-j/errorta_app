"""F010-IMPORT — restore an Errorta export bundle (.tar/.tar.gz) into ~/.errorta/corpora/.

Sibling to the directory-tree exporter under this package. Where the planner +
copy modules produce a verifiable per-corpus directory tree on a USB target,
this module unpacks a *tarball* containing that tree back into the canonical
on-disk layout that the corpus manifest module expects.

Safety properties — the public API guarantees these or it raises and writes
nothing into ``~/.errorta/corpora``:

* Every file in the archive is SHA-256 verified against ``export-manifest.json``
  *before* any file is moved into ``~/.errorta/corpora/``. Any mismatch raises
  with the offending archive path.
* Path-traversal and symlink members are rejected. Extraction prefers
  ``tarfile.extractall(filter='data')`` on Python 3.12+ and falls back to a
  manual validation pass on older runtimes — mirrors :mod:`errorta_briefs.bundle_import`.
* On corpus-name collision the function raises :class:`CorpusCollisionError`
  carrying the full conflict list so the route layer can return HTTP 409.
* On any mid-import failure every corpus directory created by this call is
  removed before the exception escapes — no half-populated corpora on disk.

Returns :class:`ImportResult` on success.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from errorta_export.safe_path import (
    UnsafePathError,
    resolve_under_root,
    safe_segment,
)


_CHUNK = 4 * 1024 * 1024

# Module-level rebindings (lazy-imported on first call) so tests can
# ``monkeypatch.setattr(errorta_export.import_, "save_manifest", ...)`` to
# simulate mid-import failures without touching the real corpus manifest layer.
try:  # pragma: no cover — defensive import
    from errorta_corpus.manifest import FileEntry, save_manifest  # noqa: F401
except Exception:  # pragma: no cover — module may be unavailable in some envs
    FileEntry = None  # type: ignore[assignment]
    save_manifest = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ExportImportError(Exception):
    """Base class for all import_export_bundle failures."""


class ManifestMissingError(ExportImportError):
    """Raised when export-manifest.json is missing or unreadable."""


class ChecksumMismatchError(ExportImportError):
    """Raised when an extracted file's SHA-256 does not match the manifest."""

    def __init__(self, path: str, expected: str, actual: str) -> None:
        super().__init__(
            f"sha256 mismatch for {path}: expected {expected[:12]}…, got {actual[:12]}…"
        )
        self.path = path
        self.expected = expected
        self.actual = actual


class CorpusCollisionError(ExportImportError):
    """Raised when one or more imported corpora already exist on disk."""

    def __init__(self, conflicting_corpora: list[str]) -> None:
        super().__init__(
            "corpus name(s) already exist in target: " + ", ".join(conflicting_corpora)
        )
        self.conflicting_corpora = list(conflicting_corpora)


class UnsafeMemberError(ExportImportError):
    """Raised when a tarball member is a symlink, hardlink, or path-traversal."""


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class ImportResult:
    corpora_imported: list[str] = field(default_factory=list)
    files_copied: int = 0
    total_bytes: int = 0
    errors: list[str] = field(default_factory=list)


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


def _safe_extract(tf: tarfile.TarFile, dest: Path) -> None:
    """Extract ``tf`` into ``dest`` with traversal/symlink guards.

    Mirrors :func:`errorta_briefs.bundle_import._safe_extract`: rejects any
    symlink/hardlink member, validates every resolved member path lies inside
    ``dest``, then calls ``extractall(filter='data')`` on Python 3.12+ and
    falls back to the unfiltered call on older runtimes (members already
    validated above).
    """
    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()
    for member in tf.getmembers():
        if member.issym() or member.islnk():
            raise UnsafeMemberError(f"unsafe tar member (symlink): {member.name}")
        if not (member.isreg() or member.isdir()):
            raise UnsafeMemberError(
                f"unsafe tar member type ({member.type!r}): {member.name}"
            )
        member_path = (dest / member.name).resolve()
        try:
            member_path.relative_to(dest_resolved)
        except ValueError as exc:
            raise UnsafeMemberError(
                f"unsafe tar member (path traversal): {member.name}"
            ) from exc

    try:
        tf.extractall(dest, filter="data")  # noqa: S202 — validated above
    except TypeError:
        tf.extractall(dest)  # noqa: S202 — validated above


def _find_manifest(staging_root: Path) -> Path:
    """Locate export-manifest.json — at staging_root or under a single top-level dir."""
    direct = staging_root / "export-manifest.json"
    if direct.exists():
        return direct
    # Search shallowly: support archives with a single top-level dir.
    for child in staging_root.iterdir():
        if child.is_dir():
            cand = child / "export-manifest.json"
            if cand.exists():
                return cand
    raise ManifestMissingError("export-manifest.json missing from archive")


def _read_manifest(manifest_path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ManifestMissingError(
            f"export-manifest.json unreadable: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise ManifestMissingError("export-manifest.json is not an object")
    if "files" not in manifest or not isinstance(manifest["files"], dict):
        raise ManifestMissingError(
            "export-manifest.json missing required 'files' object"
        )
    return manifest


def _verify_shas(manifest_base: Path, manifest: dict[str, Any]) -> None:
    """SHA-256 every manifest entry; raise on first mismatch."""
    for rel, meta in manifest["files"].items():
        expected = (meta or {}).get("sha256")
        if not expected:
            # Manifest entries without a recorded sha cannot be verified —
            # treat as a manifest defect, not a silent skip.
            raise ChecksumMismatchError(rel, expected or "", "(missing)")
        # F086: reject absolute/traversal manifest keys BEFORE any filesystem
        # touch, so a crafted key can never make us hash a file outside the
        # staging root (which would leak its sha256 via ChecksumMismatchError).
        on_disk = resolve_under_root(manifest_base, rel)
        if not on_disk.exists() or not on_disk.is_file():
            raise ChecksumMismatchError(rel, expected, "(missing on disk)")
        actual = _sha256_file(on_disk)
        if actual != expected:
            raise ChecksumMismatchError(rel, expected, actual)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def import_export_bundle(
    tarball_path: "str | Path",
    target_home: Optional[Path] = None,
) -> ImportResult:
    """Verify and import an Errorta export tarball into ``~/.errorta/corpora/``.

    Parameters
    ----------
    tarball_path:
        Path to the export bundle (``.tar``, ``.tar.gz`` or ``.tgz``).
    target_home:
        Override for the Errorta home directory (defaults to ``~/.errorta``).
        Tests pass a tmp path to isolate from the real home.

    Returns
    -------
    :class:`ImportResult` summarising the import.
    """
    # Resolve save_manifest/FileEntry via the module dict so tests can
    # monkeypatch ``errorta_export.import_.save_manifest`` cleanly. Falls back
    # to a fresh import each call to dodge the cycle with errorta_app.routes.
    save_fn = globals().get("save_manifest")
    file_entry_cls = globals().get("FileEntry")
    if save_fn is None or file_entry_cls is None:
        from errorta_corpus.manifest import FileEntry as _FE, save_manifest as _SM

        save_fn = save_fn or _SM
        file_entry_cls = file_entry_cls or _FE

    tarball_path = Path(tarball_path)
    if not tarball_path.exists() or not tarball_path.is_file():
        raise ExportImportError(f"export tarball not found: {tarball_path}")

    if target_home is not None:
        home = Path(target_home)
    else:
        from errorta_app.paths import errorta_home
        home = errorta_home()
    corpora_root = home / "corpora"

    tmp_dir = Path(tempfile.mkdtemp(prefix="errorta-export-import-"))
    created_corpus_dirs: list[Path] = []
    try:
        # ------------------------------------------------------------------
        # Phase 1: extract tarball safely into a tmp staging dir.
        # ------------------------------------------------------------------
        try:
            with tarfile.open(tarball_path) as tf:
                _safe_extract(tf, tmp_dir)
        except UnsafeMemberError:
            raise
        except tarfile.TarError as exc:
            raise ExportImportError(f"tarball unreadable: {exc}") from exc

        # ------------------------------------------------------------------
        # Phase 2: locate + parse export-manifest.json.
        # ------------------------------------------------------------------
        manifest_path = _find_manifest(tmp_dir)
        manifest_base = manifest_path.parent
        manifest = _read_manifest(manifest_path)

        # ------------------------------------------------------------------
        # Phase 3: SHA-256 every file BEFORE any commit to ~/.errorta.
        # ------------------------------------------------------------------
        _verify_shas(manifest_base, manifest)

        # ------------------------------------------------------------------
        # Phase 4: collision detection.
        #
        # The manifest's ``corpora`` field is the source of truth for the
        # set of names this bundle owns. Fall back to inferring from the
        # files map (keyed by ``Errorta/corpora/{name}/files/...``) if absent.
        # ------------------------------------------------------------------
        corpora_in_bundle: list[str] = []
        listed = manifest.get("corpora")
        if isinstance(listed, list) and listed:
            corpora_in_bundle = [str(x) for x in listed]
        else:
            seen: set[str] = set()
            for rel in manifest["files"].keys():
                parts = rel.replace("\\", "/").split("/")
                # Expected: Errorta/corpora/{name}/files/{filename}
                if len(parts) >= 4 and parts[0] == "Errorta" and parts[1] == "corpora":
                    name = parts[2]
                    if name not in seen:
                        seen.add(name)
                        corpora_in_bundle.append(name)

        if not corpora_in_bundle:
            raise ManifestMissingError(
                "export-manifest.json contains no corpora to import"
            )

        # F086: corpus names drive directory creation (corpora_root / cname) in
        # Phase 5 and the collision check just below. They come from the
        # attacker-controlled manifest "corpora" array (or inferred parts[2]),
        # so validate each as a single safe path component before any join — a
        # name like ".." would otherwise escape the corpora root.
        corpora_in_bundle = [safe_segment(name) for name in corpora_in_bundle]

        conflicting = [
            name for name in corpora_in_bundle if (corpora_root / name).exists()
        ]
        if conflicting:
            raise CorpusCollisionError(conflicting)

        # ------------------------------------------------------------------
        # Phase 5: commit. For each corpus, materialise files/ and write a
        # fresh manifest.json via save_manifest(). Track every created dir so
        # the except branch can wipe partial state.
        # ------------------------------------------------------------------
        corpora_root.mkdir(parents=True, exist_ok=True)
        result = ImportResult()

        # Group manifest file entries by corpus name.
        per_corpus_files: dict[str, list[tuple[str, dict[str, Any], Path]]] = {}
        for rel, meta in manifest["files"].items():
            rel_norm = rel.replace("\\", "/")
            parts = rel_norm.split("/")
            # We only know how to place Errorta/corpora/{name}/files/{file}.
            if (
                len(parts) >= 5
                and parts[0] == "Errorta"
                and parts[1] == "corpora"
                and parts[3] == "files"
            ):
                cname = safe_segment(parts[2])
                filename = safe_segment(parts[-1])
                per_corpus_files.setdefault(cname, []).append(
                    (filename, meta or {}, resolve_under_root(manifest_base, rel))
                )

        for cname in corpora_in_bundle:
            target_corpus_dir = corpora_root / cname
            target_corpus_dir.mkdir(parents=True, exist_ok=False)
            created_corpus_dirs.append(target_corpus_dir)
            files_dst = target_corpus_dir / "files"
            files_dst.mkdir(parents=True, exist_ok=True)

            entries: dict[str, FileEntry] = {}
            for i, (filename, meta, src) in enumerate(per_corpus_files.get(cname, [])):
                dst = files_dst / filename
                shutil.copy2(src, dst)
                fid = f"f{i:03d}"
                sha = str(meta.get("sha256") or "")
                size_bytes = int(meta.get("size_bytes") or 0)
                mime_ext = filename.rsplit(".", 1)[-1] if "." in filename else ""
                entries[fid] = file_entry_cls(
                    file_id=fid,
                    original_path=str(meta.get("original_path") or dst),
                    copied_path=str(dst),
                    sha256=sha,
                    size_bytes=size_bytes,
                    mime_ext=mime_ext,
                    status="ready",
                )
                result.files_copied += 1
                result.total_bytes += size_bytes

            save_fn(cname, entries)
            result.corpora_imported.append(cname)

        return result
    except BaseException:
        # Atomic cleanup: never leave a half-populated corpus dir on disk.
        for d in created_corpus_dirs:
            try:
                if d.exists():
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass
        raise
    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except OSError:
            pass


def import_result_as_dict(result: ImportResult) -> dict[str, Any]:
    """Convenience: ImportResult → JSON-able dict for route responses."""
    return asdict(result)
