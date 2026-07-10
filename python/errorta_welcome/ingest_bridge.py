"""Bridge from a verified welcome-corpus tarball into the F004 ingest pipeline.

F004's ingestion pipeline is not yet implemented. This module extracts the
tarball into a temp directory and delegates to the F004 entry point if it is
available; otherwise it records the extracted file list so the API can return
a deterministic payload for v0.1.
"""
from __future__ import annotations

import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

CORPUS_NAME = "welcome"


@dataclass
class IngestResult:
    corpus_name: str
    extracted_root: Path
    files: List[str] = field(default_factory=list)
    f004_invoked: bool = False
    f004_error: Optional[str] = None


def extract_tarball(tarball: Path, dest_root: Path) -> Path:
    """Safely extract ``tarball`` under ``dest_root`` and return the extracted dir."""
    dest_root.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest_root.resolve()
    with tarfile.open(tarball, mode="r:gz") as tf:
        # Guard against path traversal, symlink/hardlink escapes, and non-regular
        # member types. tarfile is known to be exploitable via crafted link
        # targets; we reject anything that isn't a plain file or directory and
        # ensure resolved names stay under dest_root.
        for member in tf.getmembers():
            if not (member.isreg() or member.isdir()):
                raise RuntimeError(
                    f"unsafe tar member type ({member.type!r}): {member.name}"
                )
            member_path = (dest_root / member.name).resolve()
            try:
                member_path.relative_to(dest_resolved)
            except ValueError as exc:
                raise RuntimeError(f"unsafe tar member: {member.name}") from exc

        # Prefer the hardened data filter on Python 3.12+; fall back to the
        # manual validation above on older runtimes.
        try:
            tf.extractall(dest_root, filter="data")  # noqa: S202 — validated above
        except TypeError:
            tf.extractall(dest_root)  # noqa: S202 — validated above

    # Conventional layout is `welcome-corpus/`. Fall back to dest_root if absent.
    candidate = dest_root / "welcome-corpus"
    return candidate if candidate.is_dir() else dest_root


def _collect_files(root: Path) -> List[str]:
    files: List[str] = []
    for p in root.rglob("*"):
        if p.is_file():
            files.append(str(p.relative_to(root)))
    return sorted(files)


def ingest_extracted(extracted_root: Path) -> IngestResult:
    """Invoke the F004 ingestion pipeline on ``extracted_root``.

    F004 is not yet built. When it lands, this function should call its public
    "create corpus from directory" entry point with ``name=CORPUS_NAME``. For
    v0.1 we record the file list and mark ``f004_invoked=False``.
    """
    files = _collect_files(extracted_root)
    result = IngestResult(
        corpus_name=CORPUS_NAME,
        extracted_root=extracted_root,
        files=files,
        f004_invoked=False,
    )

    try:
        from errorta_corpus.directory_ingest import ingest_directory

        ingested = ingest_directory(extracted_root, name=CORPUS_NAME)
        result.f004_invoked = True
        if ingested.errors:
            result.f004_error = "; ".join(ingested.errors[:5])
    except Exception as exc:  # pragma: no cover — best-effort bridge
        result.f004_error = repr(exc)

    return result
