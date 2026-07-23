"""Config + path resolution for the CLI (F147 spec §4.3, §5.1).

``ERRORTA_HOME`` resolves **exactly** the way ``errorta_app.paths`` does
(``$ERRORTA_HOME`` > legacy ``ERRORTA_STATE_DIR``/``ERRORTA_DATA_DIR`` >
``~/.errorta``), plus a ``--home`` override. The resolution is *re-implemented*
here rather than imported so golden invariant #1 holds: ``errorta_cli`` imports
nothing from ``errorta_app`` outside ``serve.py``. The CLI reads/writes the same
on-disk store the app uses, so a project is interchangeable between the GUI and
the terminal.

Project↔directory mapping (spec §5.1, decision #5): a ``.errorta-project``
pointer file in the cwd (or an ancestor) holds the ``project_id``; failing that,
match the cwd against each project's ``repo_path``/``delivery_root``.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

_CANONICAL_ENV = "ERRORTA_HOME"
_LEGACY_ENVS = ("ERRORTA_STATE_DIR", "ERRORTA_DATA_DIR")

# The per-directory pointer file that binds a working directory to a project.
POINTER_FILENAME = ".errorta-project"

_warned: set[str] = set()


def resolve_home(override: str | None = None) -> Path:
    """Resolve the Errorta data root.

    Resolution order:
      1. an explicit ``override`` (``--home``), if non-empty;
      2. ``$ERRORTA_HOME``;
      3. the first non-empty legacy env var, with a one-time deprecation warning;
      4. ``~/.errorta``.

    The directory is created if missing (mirrors ``paths.errorta_home``).
    """
    if override is not None and str(override).strip():
        base = Path(str(override)).expanduser()
    else:
        raw = os.environ.get(_CANONICAL_ENV, "").strip()
        if raw:
            base = Path(raw).expanduser()
        else:
            legacy = _read_legacy_with_warning()
            base = legacy if legacy is not None else Path.home() / ".errorta"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _read_legacy_with_warning() -> Path | None:
    for name in _LEGACY_ENVS:
        raw = os.environ.get(name, "").strip()
        if not raw:
            continue
        if name not in _warned:
            log.warning(
                "%s is set but ERRORTA_HOME is not. %s is a legacy env var; "
                "ERRORTA_HOME is the canonical replacement and wins if both are "
                "set. Please migrate your launch scripts.",
                name,
                name,
            )
            _warned.add(name)
        return Path(raw).expanduser()
    return None


# --- on-disk locations (all derived from the resolved home) -----------------

def coding_projects_dir(home: Path) -> Path:
    """Directory holding one subdir per coding project."""
    return home / "council" / "coding-projects"


def sidecar_record_path(home: Path) -> Path:
    """``sidecar.json`` — the CLI-owned sidecar discovery file (spec §4.2)."""
    return home / "sidecar.json"


def sidecar_lock_path(home: Path) -> Path:
    """The lock file guarding the discover-or-spawn critical section."""
    return home / "sidecar.lock"


def sidecar_token_path(home: Path) -> Path:
    """R3 — the per-sidecar bearer token (0600).

    Kept in a SEPARATE file from ``sidecar.json`` (which is world-readable 0644)
    so the mutation-auth secret is never exposed to a stray local reader. Minted
    at ``sidecar.spawn()`` and read by the client to attach ``Authorization:
    Bearer`` on mutating requests."""
    return home / "sidecar-token"


def build_commit() -> str | None:
    """Best-effort build commit of *this* CLI, for the sidecar compat check.

    Reads ``ERRORTA_BUILD_COMMIT`` only — a frozen binary carries no git and we
    must not import ``errorta_app.build_info`` (invariant #1). ``None`` means
    "unknown", which never blocks adoption.
    """
    commit = (os.environ.get("ERRORTA_BUILD_COMMIT") or "").strip()
    return commit or None


# --- project ↔ directory mapping --------------------------------------------

def read_pointer(start: Path | None = None) -> str | None:
    """Return the ``project_id`` from a ``.errorta-project`` pointer.

    Walks ``start`` (default cwd) and its ancestors for the first pointer file.
    The file is JSON ``{"project_id": "..."}``; a bare single line holding the id
    is also accepted for robustness.
    """
    cur = (start or Path.cwd()).resolve()
    for directory in (cur, *cur.parents):
        pointer = directory / POINTER_FILENAME
        if not pointer.is_file():
            continue
        try:
            text = pointer.read_text("utf-8").strip()
        except OSError:
            return None
        if not text:
            return None
        try:
            data = json.loads(text)
        except ValueError:
            return text.splitlines()[0].strip() or None
        if isinstance(data, dict):
            pid = data.get("project_id")
            return str(pid) if pid else None
        return str(data).strip() or None
    return None


def write_pointer(directory: Path, project_id: str) -> Path:
    """Write a ``.errorta-project`` pointer binding ``directory`` to a project."""
    pointer = Path(directory) / POINTER_FILENAME
    pointer.write_text(json.dumps({"project_id": project_id}) + "\n", "utf-8")
    return pointer


def _iter_project_records(home: Path):
    """Yield ``(project_id, project.json dict)`` for every project on disk."""
    root = coding_projects_dir(home)
    if not root.is_dir():
        return
    for child in sorted(root.iterdir()):
        manifest = child / "project.json"
        if not manifest.is_file():
            continue
        try:
            raw = json.loads(manifest.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(raw, dict):
            yield str(raw.get("id", child.name)), raw


def _path_contains(root: str | None, target: Path) -> bool:
    if not root:
        return False
    try:
        root_real = Path(str(root)).expanduser().resolve()
    except OSError:
        return False
    try:
        target.resolve().relative_to(root_real)
        return True
    except (ValueError, OSError):
        return False


def resolve_project_id(home: Path, cwd: Path | None = None) -> str | None:
    """Resolve the project bound to ``cwd`` (spec §5.1).

    1. A ``.errorta-project`` pointer in the cwd or an ancestor.
    2. Fallback: the cwd is inside some project's ``repo_path`` or
       ``delivery_root``. The deepest (most specific) match wins.
    """
    here = (cwd or Path.cwd()).resolve()
    pid = read_pointer(here)
    if pid:
        return pid

    best: tuple[int, str] | None = None
    for project_id, raw in _iter_project_records(home):
        for key in ("repo_path", "delivery_root"):
            root = raw.get(key)
            if _path_contains(root, here):
                depth = len(Path(str(root)).expanduser().resolve().parts)
                if best is None or depth > best[0]:
                    best = (depth, project_id)
    return best[1] if best else None
