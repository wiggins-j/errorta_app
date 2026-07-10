"""Errorta corpus storage + ingestion orchestration (F004)."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

# Corpus names are used as directory components under ~/.errorta/corpora/.
# Restrict to a conservative charset and reject empty / dot-prefixed values to
# prevent path traversal from local clients on 127.0.0.1.
_CORPUS_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class InvalidCorpusName(ValueError):
    pass


def validate_corpus_name(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise InvalidCorpusName("corpus name must be a non-empty string")
    if name.startswith(".") or name in {".", ".."}:
        raise InvalidCorpusName("corpus name may not start with '.'")
    if not _CORPUS_NAME_RE.match(name):
        raise InvalidCorpusName(
            "corpus name may only contain letters, digits, '_', '-', '.'"
        )
    return name


def corpus_root() -> Path:
    """Return ``$ERRORTA_HOME/corpora`` (default ``~/.errorta/corpora``).

    Routes through ``errorta_app.paths.corpora_dir()`` so this respects
    the consolidated F-INFRA-12 data-residency env var.
    """
    # Imported here (not at module top) to avoid a circular import:
    # errorta_app.paths imports nothing from corpus, but importing it at
    # module-load time before sidecar startup would force the
    # errorta_app package to evaluate before the test fixtures that
    # monkeypatch HOME can run.
    from errorta_app.paths import corpora_dir
    return corpora_dir()


def corpus_dir(name: str) -> Path:
    validate_corpus_name(name)
    d = corpus_root() / name
    (d / "files").mkdir(parents=True, exist_ok=True)
    return d


def delete_corpus(name: str) -> bool:
    """Remove a corpus directory (manifest + files + chunks) path-safely.

    Resolves the target under the corpus root and refuses anything that
    escapes it (defence in depth on top of the name charset check). Returns
    ``True`` if a corpus directory existed and was removed, ``False`` if the
    corpus did not exist (idempotent). Raises :class:`InvalidCorpusName` for an
    invalid or traversal-prone name.
    """
    validate_corpus_name(name)
    root = corpus_root().resolve()
    target = (root / name).resolve()
    # The validated name can't contain separators or '..', but resolve + a
    # containment check guards against symlinked roots and any future loosening
    # of the charset.
    if target == root or root not in target.parents:
        raise InvalidCorpusName(f"corpus path escapes the corpus root: {name!r}")
    if not target.is_dir():
        return False
    shutil.rmtree(target)
    return True
