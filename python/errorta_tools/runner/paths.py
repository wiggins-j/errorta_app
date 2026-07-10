"""Workspace path guards for ToolRunner."""
from __future__ import annotations

from pathlib import Path


class WorkspacePathError(ValueError):
    """Base class for invalid runner workspace paths."""


class WorkspaceEscapeError(WorkspacePathError):
    """Raised when a runner path would escape the granted workspace."""


def resolve_workspace_root(workspace_root: str | Path, *, must_exist: bool = True) -> Path:
    root = Path(workspace_root).expanduser().resolve()
    if must_exist and not root.exists():
        raise WorkspacePathError("workspace_root_missing")
    if must_exist and not root.is_dir():
        raise WorkspacePathError("workspace_root_not_directory")
    return root


def resolve_workspace_path(
    workspace_root: str | Path,
    relative_path: str | Path = ".",
    *,
    must_exist: bool = False,
) -> Path:
    """Resolve ``relative_path`` under ``workspace_root`` without escapes."""

    root = resolve_workspace_root(workspace_root)
    rel = Path(relative_path)
    if rel.is_absolute():
        raise WorkspaceEscapeError("absolute_workspace_path_not_allowed")
    candidate = (root / rel).expanduser().resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise WorkspaceEscapeError("workspace_path_escape") from exc
    if must_exist and not candidate.exists():
        raise WorkspacePathError("workspace_path_missing")
    return candidate


def safe_workspace_relative_path(workspace_root: str | Path, path: str | Path) -> str:
    root = resolve_workspace_root(workspace_root)
    candidate = Path(path).expanduser().resolve()
    try:
        return str(candidate.relative_to(root))
    except ValueError as exc:
        raise WorkspaceEscapeError("workspace_path_escape") from exc


__all__ = [
    "WorkspaceEscapeError",
    "WorkspacePathError",
    "resolve_workspace_path",
    "resolve_workspace_root",
    "safe_workspace_relative_path",
]
