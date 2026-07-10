"""F039 slice 7 — isolated, git-backed apply workspace for code_write auto_apply.

Auto-apply NEVER touches the user's working tree. Each run gets its own copy of
the granted workspace under ``${council_root}/apply-workspaces/<run_id>/``,
initialized as a throwaway git repo. The baseline commit is the checkpoint;
each applied write is committed on top, so:

- ``checkpoint()`` records a rollback point (a commit sha),
- ``rollback(ref)`` hard-resets to it (undo a failed apply/exec),
- ``cumulative_diff()`` is the full proposed patch vs the baseline,
- the patch is surfaced for explicit human accept; it is never auto-merged
  back to the user's tree.

git runs as a plain subprocess (this is the egress boundary — errorta_tools —
not errorta_council). No shell, fixed argv, isolated cwd, identity passed via
``-c`` flags so it never depends on or mutates the user's global git config.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

from errorta_council.paths import council_root

from .paths import (
    WorkspacePathError,
    resolve_workspace_path,
    resolve_workspace_root,
)

_SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

_GIT_IDENTITY = (
    "-c", "user.email=runner@errorta.local",
    "-c", "user.name=Errorta Runner",
    "-c", "commit.gpgsign=false",
)

_SKIPPED_DIR_NAMES = frozenset({
    ".git",
    ".errorta",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".cache",
    ".next",
    ".nuxt",
    ".svelte-kit",
    ".turbo",
    "dist",
    "build",
    "target",
    "coverage",
})

_SKIPPED_FILE_NAMES = frozenset({
    ".coverage",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".yarnrc",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
})

_ENV_EXAMPLE_NAMES = frozenset({
    ".env.example",
    ".env.sample",
    ".env.template",
})

_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx")


class ApplyWorkspaceError(RuntimeError):
    """A git/apply-workspace operation failed (stable reason code)."""


def _safe_run_id(run_id: str) -> str:
    # Allowlist (not denylist): run_id is the only caller-influenced component
    # of the isolated root path, so reject anything but [A-Za-z0-9_.-] — this
    # also covers "..", "/", and "\\" (Windows separator).
    if not run_id or ".." in run_id or not _SAFE_RUN_ID_RE.match(run_id):
        raise ApplyWorkspaceError("apply_unsafe_run_id")
    return run_id


def _git(repo: Path, *args: str, _stdin: str | None = None,
         _env: dict[str, str] | None = None) -> str:
    run_env = None
    if _env is not None:
        run_env = {**os.environ, **_env}
    proc = subprocess.run(
        ["git", "-C", str(repo), *_GIT_IDENTITY, *args],
        capture_output=True, text=True, input=_stdin, env=run_env,
    )
    if proc.returncode != 0:
        # Never surface raw git stderr (could echo paths/content) — stable code.
        raise ApplyWorkspaceError(f"git_failed:{args[0] if args else 'git'}")
    return proc.stdout


def _git_try(repo: Path, *args: str) -> tuple[int, str, str]:
    """Non-raising git for operations whose failure is expected/handled (e.g. a
    merge conflict). Returns (returncode, stdout, stderr)."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *_GIT_IDENTITY, *args],
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _git_bytes(repo: Path, *args: str) -> tuple[int, bytes]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *_GIT_IDENTITY, *args],
        capture_output=True,
    )
    return proc.returncode, proc.stdout


_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,128}$")


def _safe_branch(name: str) -> str:
    if not _BRANCH_RE.match(name or "") or ".." in name:
        raise ApplyWorkspaceError("apply_bad_branch_name")
    return name


def _safe_rel_pathspec(path: str) -> str:
    cleaned = str(path or "")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if (
        not cleaned
        or "\x00" in cleaned
        or cleaned.startswith(":")
        or cleaned.startswith("/")
        or os.path.isabs(cleaned)
        or any(part == ".." for part in cleaned.split("/"))
    ):
        raise ApplyWorkspaceError("apply_bad_pathspec")
    return cleaned


def _worktree_slug(task_id: str, branch: str) -> str:
    label = re.sub(r"[^A-Za-z0-9_.-]+", "-", task_id).strip(".-") or "task"
    digest = hashlib.sha256(f"{task_id}\0{branch}".encode("utf-8")).hexdigest()[:12]
    return f"{label[:48]}-{digest}"


def _copy_ignore(src: str, names: list[str]) -> set[str]:
    """Skip paths that would weaken the isolated apply workspace boundary.

    The source workspace is user-controlled. ``shutil.copytree`` follows
    symlinks by default, which can smuggle files from outside the granted
    workspace into Errorta's persistent apply area. Secret-looking dotfiles and
    heavyweight dependency/cache directories are also skipped; the auto-apply
    workspace is a patch/test scratchpad, not a backup of the user's machine.
    """

    root = Path(src)
    ignored: set[str] = set()
    for name in names:
        path = root / name
        lowered = name.lower()
        if path.is_symlink():
            ignored.add(name)
        elif path.is_dir() and name in _SKIPPED_DIR_NAMES:
            ignored.add(name)
        elif name in _SKIPPED_FILE_NAMES:
            ignored.add(name)
        elif lowered.startswith(".env") and name not in _ENV_EXAMPLE_NAMES:
            ignored.add(name)
        elif lowered.endswith(_SECRET_SUFFIXES):
            ignored.add(name)
    return ignored


class ApplyWorkspace:
    """A per-run isolated, git-backed copy of the granted workspace."""

    def __init__(self, *, run_id: str) -> None:
        self._run_id = _safe_run_id(run_id)
        self._root = council_root() / "apply-workspaces" / self._run_id
        self._worktrees_root = self._root.parent / f"{self._run_id}.worktrees"
        # Source pointer lives OUTSIDE the git repo dir so it never lands in a
        # commit or the cumulative diff.
        self._meta_path = self._root.parent / f"{self._run_id}.source.json"
        self._worktree_registry_path = (
            self._root.parent / f"{self._run_id}.worktrees.json"
        )
        # F087 Slice 3: serialize the worktree-registry read-modify-write so
        # concurrent dev/tester turns (one ApplyWorkspace shared across the
        # ThreadPool) can't lose entries / leak worktree dirs. RLock so a method
        # calling another locked method on this instance can't self-deadlock.
        self._lock = threading.RLock()

    @property
    def root(self) -> Path:
        return self._root

    def _load_worktree_registry(self) -> dict[str, dict[str, str]]:
        try:
            data = json.loads(self._worktree_registry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        out: dict[str, dict[str, str]] = {}
        for key, raw in data.items():
            if not isinstance(raw, dict):
                continue
            branch = str(raw.get("branch", ""))
            path = str(raw.get("path", ""))
            if key and branch and path:
                out[str(key)] = {"branch": branch, "path": path}
        return out

    def _save_worktree_registry(self, registry: dict[str, dict[str, str]]) -> None:
        # F087 Slice 3: atomic write (temp + os.replace) so a crash mid-write
        # can never truncate the registry to invalid JSON (which _load would
        # silently read as {} -> all live worktrees leaked).
        path = self._worktree_registry_path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".worktrees-", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(registry, sort_keys=True))
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _write_source_meta(self, payload: bytes) -> None:
        """Atomically replace the out-of-tree source metadata."""
        path = self._meta_path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".source-", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _owned_worktree_path(self, path: str | Path) -> Path:
        root = self._worktrees_root.resolve()
        resolved = Path(path).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ApplyWorkspaceError("apply_unowned_worktree") from exc
        return resolved

    def _branch_exists(self, branch: str) -> bool:
        rc, _out, _err = _git_try(
            self._root, "show-ref", "--verify", "--quiet",
            f"refs/heads/{_safe_branch(branch)}")
        return rc == 0

    def _remove_worktree_entry(self, entry: dict[str, str]) -> bool:
        path = self._owned_worktree_path(entry["path"])
        removed = False
        if path.exists():
            _git_try(self._root, "worktree", "remove", "--force", str(path))
            if path.exists():
                shutil.rmtree(path)
            removed = True
        _git_try(self._root, "worktree", "prune")
        return removed

    def has_worktree(self, task_id: str) -> bool:
        entry = self._load_worktree_registry().get(str(task_id))
        if not entry:
            return False
        try:
            path = self._owned_worktree_path(entry["path"])
        except ApplyWorkspaceError:
            return False
        return path.exists()

    def worktree_for(
        self,
        task_id: str,
        *,
        branch: str | None = None,
        base: str = "master",
        reset: bool = False,
    ) -> Path:
        """Return an owned git worktree for ``task_id`` checked out to ``branch``.

        The primary repo stays on its integration branch. Task worktrees share
        the same object store, so dev writes/test runs can operate concurrently
        without clobbering the primary checkout.
        """
        if not self.exists():
            raise ApplyWorkspaceError("apply_workspace_missing")
        task_key = str(task_id)
        if not task_key:
            raise ApplyWorkspaceError("apply_bad_task_id")
        with self._lock:
            self._worktrees_root.mkdir(parents=True, exist_ok=True)
            registry = self._load_worktree_registry()

            # F087 Slice 3: when no branch is given (e.g. head_ref/write_and_commit
            # querying a task), KEEP the task on its already-registered branch
            # instead of defaulting to ``task-<id>`` — defaulting would evict a
            # worktree on a custom branch and recreate it off base, destroying the
            # checkout. Only fall back to the default for a task with no entry.
            existing = registry.get(task_key)
            if branch is None and existing and existing.get("branch"):
                branch = existing["branch"]
            branch = _safe_branch(branch or f"task-{task_key}")
            base = _safe_branch(base)

            # A branch can only have one owned task worktree. Remove
            # stale/conflicting registry entries before creating the requested one.
            for key, entry in list(registry.items()):
                if key != task_key and entry.get("branch") == branch:
                    self._remove_worktree_entry(entry)
                    registry.pop(key, None)

            entry = registry.get(task_key)
            if entry and entry.get("branch") != branch:
                self._remove_worktree_entry(entry)
                registry.pop(task_key, None)
                entry = None

            path = self._owned_worktree_path(
                entry["path"] if entry else self._worktrees_root / _worktree_slug(task_key, branch)
            )
            if path.exists() and (path / ".git").exists():
                if reset:
                    _git(path, "reset", "--hard", "-q", base)
                    _git(path, "clean", "-fdq")
                registry[task_key] = {"branch": branch, "path": str(path)}
                self._save_worktree_registry(registry)
                return path

            if path.exists():
                shutil.rmtree(path)

            if reset:
                if self.current_branch() == branch:
                    _git(self._root, "checkout", "-q", base)
                _git(self._root, "branch", "-f", branch, base)
            elif not self._branch_exists(branch):
                _git(self._root, "branch", branch, base)

            _git(self._root, "worktree", "add", "--force", str(path), branch)
            registry[task_key] = {"branch": branch, "path": str(path)}
            self._save_worktree_registry(registry)
            return path

    def remove_worktree(self, task_id: str) -> bool:
        with self._lock:
            registry = self._load_worktree_registry()
            entry = registry.pop(str(task_id), None)
            if not entry:
                self._save_worktree_registry(registry)
                return False
            removed = self._remove_worktree_entry(entry)
            self._save_worktree_registry(registry)
            return removed

    def remove_worktree_for_branch(self, branch: str) -> bool:
        branch = _safe_branch(branch)
        with self._lock:
            registry = self._load_worktree_registry()
            removed = False
            for key, entry in list(registry.items()):
                if entry.get("branch") == branch:
                    removed = self._remove_worktree_entry(entry) or removed
                    registry.pop(key, None)
            self._save_worktree_registry(registry)
            return removed

    def task_id_for_branch(self, branch: str) -> str | None:
        branch = _safe_branch(branch)
        for task_id, entry in self._load_worktree_registry().items():
            if entry.get("branch") == branch:
                return task_id
        return None

    def prune_worktrees(self) -> list[str]:
        """Prune stale git worktree metadata and remove invalid owned entries."""
        if not self.exists():
            return []
        with self._lock:
            _git_try(self._root, "worktree", "prune")
            registry = self._load_worktree_registry()
            removed: list[str] = []
            for task_id, entry in list(registry.items()):
                try:
                    path = self._owned_worktree_path(entry["path"])
                except ApplyWorkspaceError:
                    registry.pop(task_id, None)
                    removed.append(task_id)
                    continue
                rc, _out, _err = _git_try(path, "rev-parse", "--is-inside-work-tree")
                if not path.exists() or rc != 0:
                    if path.exists():
                        shutil.rmtree(path)
                    registry.pop(task_id, None)
                    removed.append(task_id)
            self._save_worktree_registry(registry)
            return removed

    def _clear_owned_worktrees(self) -> None:
        registry = self._load_worktree_registry()
        for entry in list(registry.values()):
            try:
                self._remove_worktree_entry(entry)
            except Exception:
                continue
        if self._worktrees_root.exists():
            shutil.rmtree(self._worktrees_root)
        try:
            self._worktree_registry_path.unlink()
        except FileNotFoundError:
            pass

    def destroy(self) -> None:
        """Remove this apply workspace and all owned sidecar worktree metadata."""
        with self._lock:
            self._clear_owned_worktrees()
            if self._root.exists():
                shutil.rmtree(self._root)
            try:
                self._meta_path.unlink()
            except FileNotFoundError:
                pass

    def exists(self) -> bool:
        return (self._root / ".git").is_dir()

    def ensure(self, source_workspace: str | Path) -> Path:
        """Idempotently create the isolated copy + baseline checkpoint."""
        if self.exists():
            return self._root
        source = resolve_workspace_root(source_workspace)
        # The isolated workspace must live OUTSIDE the granted workspace, or the
        # recursive copy would copy itself. (council_root is normally under
        # ${ERRORTA_HOME}, separate from a user project; this guards a
        # misconfiguration where they overlap.) Resolve BOTH ends — comparing an
        # unresolved root against a resolved source misses symlinked paths
        # (e.g. /tmp -> /private/tmp on macOS).
        resolved_root = self._root.resolve()
        if resolved_root == source or source in resolved_root.parents:
            raise ApplyWorkspaceError("apply_workspace_inside_source")
        self._root.parent.mkdir(parents=True, exist_ok=True)
        if self._root.exists():
            shutil.rmtree(self._root)
        # Copy a sanitized workspace. Symlinks are skipped instead of followed,
        # so a project-local link to /Users, /etc, or another checkout cannot
        # drag data outside the granted tree into the persistent apply area.
        shutil.copytree(source, self._root, ignore=_copy_ignore)
        _git(self._root, "init", "-q")
        _git(self._root, "add", "-A")
        # Allow an empty baseline (an empty granted workspace is valid).
        _git(self._root, "commit", "-q", "--allow-empty", "-m", "errorta-baseline")
        # F087-17: standardize the integration branch name so the branch-per-task
        # workflow has a deterministic base (git's default branch name varies).
        _git(self._root, "branch", "-M", "master")
        # Record where this copy came from, so a human-accepted merge-back knows
        # which tree to write the proposed patch into.
        self._write_source_meta(json.dumps({"source": str(source)}).encode("utf-8"))
        return self._root

    def reseed(self, source_workspace: str | Path) -> Path:
        """F138: atomically REPLACE the snapshot with a fresh copy of ``source``.

        Unlike :meth:`ensure` (copy-once), this discards the current snapshot and
        rebuilds it from ``source`` — used when an imported project is refreshed.
        Atomic-swap: the new copy is built in a temp sibling dir FIRST, so a failed
        copy (disk full / unreadable source file) leaves the prior good snapshot
        untouched; only after a clean build is the old workspace torn down (incl.
        owned worktrees) and the new one moved into place.
        """
        source = resolve_workspace_root(source_workspace)
        resolved_root = self._root.resolve()
        if resolved_root == source or source in resolved_root.parents:
            raise ApplyWorkspaceError("apply_workspace_inside_source")
        with self._lock:
            self._root.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._root.parent / f"{self._run_id}.reseed-tmp"
            backup = self._root.parent / f"{self._run_id}.reseed-backup"
            # Recover a prior interrupted swap before starting another one. A
            # leftover backup with a live root is only stale cleanup from a
            # successful swap; without a live root it is the last good snapshot.
            if backup.exists():
                if self._root.exists():
                    shutil.rmtree(backup)
                else:
                    os.replace(backup, self._root)
                    _git_try(self._root, "worktree", "prune")
            if tmp.exists():
                shutil.rmtree(tmp)
            try:
                shutil.copytree(source, tmp, ignore=_copy_ignore)
                _git(tmp, "init", "-q")
                _git(tmp, "add", "-A")
                _git(tmp, "commit", "-q", "--allow-empty", "-m", "errorta-baseline")
                _git(tmp, "branch", "-M", "master")
            except Exception:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
            # New snapshot is fully built. Preserve the prior root until the
            # replacement and source metadata both succeed, so even an os.replace
            # or metadata-write failure can roll back to a usable snapshot.
            old_meta: bytes | None
            try:
                old_meta = self._meta_path.read_bytes()
            except OSError:
                old_meta = None
            if self._root.exists():
                os.replace(self._root, backup)
            try:
                # With the primary root parked at ``backup``, remove owned
                # worktrees but preserve the old source metadata until the new
                # root is live. A crash at any point can therefore restore the
                # backup without losing its source pointer.
                self._clear_owned_worktrees()
                os.replace(tmp, self._root)
                self._write_source_meta(
                    json.dumps({"source": str(source)}).encode("utf-8")
                )
            except Exception:
                if self._root.exists():
                    shutil.rmtree(self._root, ignore_errors=True)
                if backup.exists():
                    os.replace(backup, self._root)
                    _git_try(self._root, "worktree", "prune")
                if old_meta is not None:
                    self._write_source_meta(old_meta)
                shutil.rmtree(tmp, ignore_errors=True)
                raise
            shutil.rmtree(backup, ignore_errors=True)
        return self._root

    def head(self) -> str:
        """Current HEAD sha of the snapshot (full). Raises on a broken repo."""
        return _git(self._root, "rev-parse", "HEAD").strip()

    def set_seed_head(self) -> None:
        """F138: record the current HEAD as the 'clean seed' — the snapshot state
        right after setup/reseed, before any run. Un-accepted deliverable work is
        then 'HEAD advanced past this seed', immune to the setup-injected
        ``.gitignore`` commit. Best-effort; stored alongside the source pointer."""
        with self._lock:
            try:
                head = self.head()
            except ApplyWorkspaceError:
                return
            try:
                data = json.loads(self._meta_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            except (OSError, ValueError):
                data = {}
            data["seed_head"] = head
            self._write_source_meta(json.dumps(data).encode("utf-8"))

    def seed_head(self) -> str | None:
        try:
            data = json.loads(self._meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        value = data.get("seed_head") if isinstance(data, dict) else None
        return str(value) if value else None

    def has_work_beyond_seed(self) -> bool:
        """F138: True iff ANY branch (``master`` OR a per-task branch) has a commit
        beyond the recorded clean seed. Task work lives on separate ``task-*``
        branches/worktrees and is only on ``master`` after a merge, so a check of
        ``master`` HEAD alone would MISS an interrupted/unmerged run's committed
        work — re-seeding would then silently destroy it. This counts commits
        reachable from any branch but not from the seed. Conservative (True) when
        it can't be determined."""
        seed = self.seed_head()
        if not seed or not self.exists():
            return False
        rc, out, _ = _git_try(self._root, "rev-list", "--count", "--branches",
                              "--not", seed)
        if rc != 0:
            return True
        try:
            return int(out.strip()) > 0
        except ValueError:
            return True

    def has_uncommitted_work(self) -> bool:
        """Return whether the primary snapshot or any owned task worktree is dirty.

        A worker can be interrupted after writing files but before committing its
        task branch. Commit-graph checks alone miss that state and a re-seed would
        silently delete it, so unreadable/invalid worktrees fail closed to True.
        """
        if not self.exists():
            return False
        paths = [self._root]
        for entry in self._load_worktree_registry().values():
            try:
                path = self._owned_worktree_path(entry["path"])
            except (ApplyWorkspaceError, KeyError):
                return True
            if not path.exists():
                return True
            paths.append(path)
        for path in paths:
            rc, out, _err = _git_try(path, "status", "--porcelain")
            if rc != 0 or bool(out.strip()):
                return True
        return False

    def source_path(self) -> Path | None:
        """The granted workspace this copy was made from (None if unrecorded)."""
        try:
            data = json.loads(self._meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        src = data.get("source")
        return Path(src) if src else None

    def baseline_ref(self) -> str:
        # The first commit on the branch is the baseline checkpoint.
        out = _git(self._root, "rev-list", "--max-parents=0", "HEAD")
        return out.strip().splitlines()[0]

    def head_ref(self, *, task_id: str | None = None) -> str:
        repo = self.worktree_for(task_id) if task_id else self._root
        return _git(repo, "rev-parse", "HEAD").strip()

    def branch_head(self, branch: str) -> str:
        return _git(self._root, "rev-parse", _safe_branch(branch)).strip()

    def branch_heads(self) -> dict[str, str]:
        out = _git(
            self._root, "for-each-ref", "--format=%(refname:short) %(objectname)",
            "refs/heads/")
        heads: dict[str, str] = {}
        for line in out.splitlines():
            if not line.strip():
                continue
            name, sha = line.split(" ", 1)
            heads[name] = sha.strip()
        return heads

    def workspace_fingerprint(self) -> dict[str, Any]:
        worktrees: dict[str, dict[str, Any]] = {}
        for task_id, entry in self._load_worktree_registry().items():
            try:
                path = self._owned_worktree_path(entry["path"])
                exists = path.exists()
                head = _git(path, "rev-parse", "HEAD").strip() if exists else ""
            except Exception:
                exists = False
                head = ""
            worktrees[task_id] = {
                "branch": entry.get("branch", ""),
                "exists": exists,
                "head": head,
            }
        return {
            "format": "coding-workspace-fingerprint.v1",
            "primary": {
                "branch": self.current_branch() if self.exists() else "",
                "head": self.head_ref() if self.exists() else "",
            },
            "branches": self.branch_heads() if self.exists() else {},
            "worktrees": worktrees,
        }

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """True iff ``ancestor`` is reachable from ``descendant`` (i.e. a
        fast-forward / contains relationship). False on any error or non-ancestry,
        so callers fail closed. Used by resume integrity to accept a base branch
        that legitimately advanced via merges since the run was interrupted."""
        if not ancestor or not descendant:
            return False
        if ancestor == descendant:
            return True
        rc, _out, _err = _git_try(
            self._root, "merge-base", "--is-ancestor", ancestor, descendant)
        return rc == 0

    def write_and_commit(
        self, rel_path: str, content: str | bytes, *, task_id: str | None = None,
        allow_empty: bool = True,
    ) -> str:
        """Write a file inside the workspace (traversal-guarded) and commit it.
        Returns the new HEAD sha (a rollback point).

        ``content`` is text (written UTF-8) or ``bytes`` (written verbatim) — the
        bytes path lets a dev turn persist a real binary asset (a PNG, a font, a
        compiled resource) that the text-only channel could only mangle into an
        undecodable placeholder. The commit / no-op-detection logic is
        content-type agnostic (git stages either the same).

        ``allow_empty`` (default True) preserves every historical caller (the
        `.gitignore` seed, the F039 ``code_write`` builtin, validation scripts).
        The F139 WS-C dev write path passes ``allow_empty=False`` so a write whose
        content is identical to what is already committed makes **no commit** — an
        empty commit would otherwise let re-emitting an existing file look like
        progress (the reddit-look-a-like Navigation-rewritten-100× failure)."""
        repo = self.worktree_for(task_id) if task_id else self._root
        target = resolve_workspace_path(repo, rel_path, must_exist=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, (bytes, bytearray)):
            target.write_bytes(bytes(content))
        else:
            target.write_text(content, encoding="utf-8")
        _git(repo, "add", "-A")
        if not allow_empty:
            # Nothing staged vs HEAD → the write was a no-op; do not commit.
            rc, _out, _err = _git_try(repo, "diff", "--cached", "--quiet")
            if rc == 0:
                return self.head_ref(task_id=task_id)
        commit_args = ["commit", "-q", "-m", f"apply:{rel_path}"]
        if allow_empty:
            commit_args.insert(2, "--allow-empty")
        _git(repo, *commit_args)
        return self.head_ref(task_id=task_id)

    def changed_paths(self, name: str, *, base: str = "master") -> list[str]:
        """Distinct file paths that branch ``name`` changes relative to ``base``
        (``git diff --name-only base...name``). Empty when ``name`` is even with
        ``base`` — the F139 WS-C "this task contributed no net change" signal."""
        out = _git(self._root, "diff", "--name-only",
                   f"{_safe_branch(base)}...{_safe_branch(name)}")
        return [ln for ln in out.splitlines() if ln.strip()]

    def write_master_file(self, rel_path: str, content: str, *,
                          message: str | None = None) -> str:
        """Atomically write ``content`` to ``rel_path`` on the ``master`` ref via
        git plumbing — WITHOUT checking out or switching the shared working tree.

        A live coding run may hold the working tree on a task branch, so this
        never runs ``checkout``/``add``/``commit`` against the working copy.
        Instead it: hashes the new blob (``hash-object -w``), builds a new tree
        from ``master``'s tree in a THROWAWAY index (``GIT_INDEX_FILE`` in a temp
        dir, ``read-tree`` + ``update-index --cacheinfo`` + ``write-tree``),
        commits it with ``commit-tree`` parented on the current ``master``, then
        moves ``refs/heads/master`` with a compare-and-swap (``update-ref`` old
        value) so a concurrent advance fails closed rather than clobbering. The
        working-tree checkout and the worktree registry are untouched. Returns the
        new ``master`` commit sha."""
        safe = _safe_rel_pathspec(rel_path)
        with self._lock:
            # Current master commit + tree (compare-and-swap baseline).
            old_master = _git(self._root, "rev-parse", "master").strip()
            blob = _git(self._root, "hash-object", "-w", "--stdin",
                        _stdin=content).strip()
            tmp_index_dir = tempfile.mkdtemp(prefix=".master-write-", dir=str(self._root.parent))
            try:
                index_file = str(Path(tmp_index_dir) / "index")
                env = {"GIT_INDEX_FILE": index_file}
                _git(self._root, "read-tree", "master", _env=env)
                _git(self._root, "update-index", "--add", "--cacheinfo",
                     f"100644,{blob},{safe}", _env=env)
                new_tree = _git(self._root, "write-tree", _env=env).strip()
            finally:
                shutil.rmtree(tmp_index_dir, ignore_errors=True)
            commit_msg = message or f"human edit: {safe}"
            new_commit = _git(self._root, "commit-tree", new_tree,
                              "-p", old_master, "-m", commit_msg,
                              _stdin="").strip()
            # Compare-and-swap: only advance master if it hasn't moved.
            _git(self._root, "update-ref", "refs/heads/master", new_commit, old_master)
            return new_commit

    def rollback(self, ref: str) -> None:
        if not all(c in "0123456789abcdefABCDEF" for c in ref) or not ref:
            raise ApplyWorkspaceError("apply_bad_rollback_ref")
        _git(self._root, "reset", "--hard", "-q", ref)

    def cumulative_diff(self) -> str:
        """The full patch the run has built vs the baseline checkpoint."""
        return _git(self._root, "diff", f"{self.baseline_ref()}..HEAD")

    # -- F087-17 branch-per-task workflow ---------------------------------- #
    def current_branch(self) -> str:
        return _git(self._root, "rev-parse", "--abbrev-ref", "HEAD").strip()

    def create_branch(self, name: str, *, base: str = "master") -> str:
        """Create (or reset) ``name`` off ``base`` and check it out. Returns the
        branch HEAD. The dev works here so each task starts from — and extends —
        everything merged into ``base`` so far."""
        _git(self._root, "checkout", "-q", "-B", _safe_branch(name),
             _safe_branch(base))
        return self.head_ref()

    def checkout(self, name: str) -> None:
        _git(self._root, "checkout", "-q", _safe_branch(name))

    def delete_branch(self, name: str) -> bool:
        """Force-delete a branch (e.g. after its PR merged, to reclaim space).
        Never deletes the current branch / master; best-effort (returns False on
        failure rather than raising)."""
        name = _safe_branch(name)
        if name == "master" or name == self.current_branch():
            return False
        rc, _o, _e = _git_try(self._root, "branch", "-D", name)
        return rc == 0

    def list_branches(self) -> list[str]:
        out = _git(self._root, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
        return [b for b in out.splitlines() if b.strip()]

    def read_file(self, rel_path: str, *, task_id: str | None = None) -> str | None:
        """Current working-tree contents of ``rel_path`` (traversal-guarded), or
        None if absent — the read-back that lets a dev EXTEND existing code."""
        repo = self.worktree_for(task_id) if task_id else self._root
        target = resolve_workspace_path(repo, rel_path, must_exist=False)
        if not target.exists() or not target.is_file():
            return None
        try:
            return target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def list_files(self, *, task_id: str | None = None) -> list[str]:
        """Tracked files on the current branch (for read-back of the whole tree)."""
        repo = self.worktree_for(task_id) if task_id else self._root
        out = _git(repo, "ls-files")
        return [ln for ln in out.splitlines() if ln.strip()]

    def list_files_on_ref(self, ref: str = "master") -> list[str]:
        """Tracked files on a committed ``ref`` (default ``master``), read via git
        plumbing (``git ls-tree``) WITHOUT switching the shared working checkout —
        a live run may hold it on a task branch. This is the merged-truth source
        (F139 WS-B): "what is actually integrated" is answered by git here, never
        by the artifact ledger. Returns ``[]`` when the ref does not exist (a fresh
        workspace before the first commit) or on any git failure (fail-closed:
        never invent files)."""
        safe = _safe_branch(ref)
        # Verify the ref resolves to a commit before listing. `rev-parse` accepts
        # branches, tags, AND raw shas — `_branch_exists` only checks local heads,
        # which would make a valid sha/tag ref look like an empty project.
        rc, _out, _err = _git_try(
            self._root, "rev-parse", "--verify", "--quiet", f"{safe}^{{commit}}")
        if rc != 0:
            return []
        rc, out, _err = _git_try(self._root, "ls-tree", "-r", "--name-only", safe)
        if rc != 0:
            return []
        return [ln for ln in out.splitlines() if ln.strip()]

    def branch_diff(self, name: str, *, base: str = "master") -> str:
        """The PR diff: what ``name`` changes relative to ``base``."""
        return _git(self._root, "diff",
                    f"{_safe_branch(base)}...{_safe_branch(name)}")

    def export_master(self, dest: str | Path) -> str:
        """F087-20: materialize the integrated ``master`` tree into ``dest`` as a
        clean, user-facing deliverable — tracked files only (via ``git archive``),
        no ``.git`` and no build artifacts. Returns the destination path."""
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["git", "-C", str(self._root), *_GIT_IDENTITY, "archive", "master"],
            capture_output=True,
        )
        if proc.returncode != 0:
            raise ApplyWorkspaceError("git_failed:archive")
        tar = subprocess.run(["tar", "-x", "-C", str(dest)], input=proc.stdout,
                             capture_output=True)
        if tar.returncode != 0:
            raise ApplyWorkspaceError("export_extract_failed")
        return str(dest)

    def merge_branch(self, name: str, *, into: str = "master") -> dict[str, Any]:
        """Merge ``name`` into ``into`` (the PM-approved integration step).
        Conflict-aware + fail-closed: on conflict the merge is ABORTED (the tree
        is never left half-merged) and the conflicted paths are reported."""
        name = _safe_branch(name)
        into = _safe_branch(into)
        _git(self._root, "checkout", "-q", into)
        rc, _out, _err = _git_try(self._root, "merge", "--no-ff", "--no-edit", name)
        if rc == 0:
            self.remove_worktree_for_branch(name)
            return {"merged": True, "conflicts": [], "head": self.head_ref()}
        # capture conflicted paths, then abort cleanly
        _rc2, conflicts_out, _e2 = _git_try(
            self._root, "diff", "--name-only", "--diff-filter=U")
        conflicts = [ln for ln in conflicts_out.splitlines() if ln.strip()]
        _git_try(self._root, "merge", "--abort")
        return {"merged": False, "conflicts": conflicts, "head": self.head_ref()}

    def update_branch_from_base(
        self, task_id: str, branch: str, *, base: str = "master",
    ) -> dict[str, Any]:
        """F087-3 stale-base revalidation: merge ``base`` into ``branch`` (in the
        branch's own worktree) so a PR validated against an older ``base`` picks
        up the integrated tree before it is re-tested.

        Returns ``{updated, conflicts, head, changed}``. ``changed`` is False when
        the branch already contained ``base`` (nothing to revalidate). Like
        ``merge_branch`` it is conflict-aware + fail-closed: a conflicting merge
        is ABORTED (the worktree is never left half-merged) and the conflicted
        paths are reported so the caller can bounce the PR to a resolve task."""
        branch = _safe_branch(branch)
        base = _safe_branch(base)
        wt = self.worktree_for(task_id, branch=branch, reset=False)
        before = _git(wt, "rev-parse", "HEAD").strip()
        rc, _out, _err = _git_try(wt, "merge", "--no-ff", "--no-edit", base)
        if rc == 0:
            after = _git(wt, "rev-parse", "HEAD").strip()
            return {"updated": True, "conflicts": [], "head": after,
                    "changed": after != before}
        _rc2, conflicts_out, _e2 = _git_try(
            wt, "diff", "--name-only", "--diff-filter=U")
        conflicts = [ln for ln in conflicts_out.splitlines() if ln.strip()]
        _git_try(wt, "merge", "--abort")
        return {"updated": False, "conflicts": conflicts, "head": before,
                "changed": False}

    # -- merge-back to the user's tree (human-accept-gated) ------------------ #

    def _changed_entries(self) -> list[tuple[str, str]]:
        """``(status, path)`` for every file changed baseline..HEAD.

        Uses ``-z`` so paths with spaces/non-ascii are unambiguous. A rename
        (``R``) / copy (``C``) is expanded to a delete of the old path + an add
        of the new path, which is exactly how merge-back should treat it.
        """
        out = _git(
            self._root, "diff", "--name-status", "-z",
            f"{self.baseline_ref()}..HEAD",
        )
        tokens = out.split("\0")
        entries: list[tuple[str, str]] = []
        i = 0
        while i < len(tokens):
            status = tokens[i]
            if not status:
                i += 1
                continue
            code = status[0]
            # R/C carry two paths (old, new). We don't pass -C, so C never
            # actually appears; both are treated as delete-old + add-new, which
            # is correct for a rename (the only form we emit).
            if code in ("R", "C"):
                old_path, new_path = tokens[i + 1], tokens[i + 2]
                entries.append(("D", old_path))
                entries.append(("A", new_path))
                i += 3
            else:
                entries.append((code, tokens[i + 1]))
                i += 2
        return entries

    def _baseline_bytes(self, rel_path: str) -> bytes | None:
        proc = subprocess.run(
            ["git", "-C", str(self._root), *_GIT_IDENTITY,
             "show", f"{self.baseline_ref()}:{rel_path}"],
            capture_output=True,
        )
        return proc.stdout if proc.returncode == 0 else None

    def _head_bytes(self, rel_path: str) -> bytes | None:
        target = resolve_workspace_path(self._root, rel_path, must_exist=False)
        return target.read_bytes() if target.is_file() else None

    def merge_back_preview(self) -> dict[str, Any]:
        """Describe the proposed patch + any conflicts WITHOUT writing anything.

        A *conflict* is a file whose current content in the user's tree diverges
        from BOTH our baseline (so the user edited it after the run started) AND
        our proposed result — applying would clobber concurrent user edits.
        """
        source = self.source_path()
        if source is None or not source.exists():
            raise ApplyWorkspaceError("apply_source_missing")
        changed: list[dict[str, str]] = []
        conflicts: list[str] = []
        for status, path in self._changed_entries():
            changed.append({"path": path, "status": status})
            baseline = self._baseline_bytes(path)
            head = self._head_bytes(path)
            try:
                src_file = resolve_workspace_path(source, path, must_exist=False)
            except WorkspacePathError:
                # A path that escapes the source tree is itself a conflict we
                # refuse to act on (never write outside the granted tree).
                conflicts.append(path)
                continue
            src = src_file.read_bytes() if src_file.is_file() else None
            if src is not None and src != baseline and src != head:
                conflicts.append(path)
        return {
            "source": str(source),
            "has_changes": bool(changed),
            "changed_files": changed,
            "conflicts": conflicts,
            "diff": self.cumulative_diff(),
        }

    def merge_back(self, *, allow_conflicts: bool = False) -> dict[str, Any]:
        """Apply the cumulative patch into the user's source tree.

        Fail-closed: if any file conflicts and ``allow_conflicts`` is False,
        nothing is written. Caller is responsible for the human-accept gate;
        this method assumes acceptance has been granted.

        Two passes: resolve + validate EVERY target (traversal-guarded, plus a
        write-time conflict re-check that closes the preview→write TOCTOU)
        before writing anything, so a bad path or a freshly-introduced conflict
        can't leave the tree half-merged.
        """
        preview = self.merge_back_preview()
        source = Path(preview["source"])
        if preview["conflicts"] and not allow_conflicts:
            return {
                "applied": False,
                "reason": "conflicts",
                "conflicts": preview["conflicts"],
                "changed_files": preview["changed_files"],
            }
        # Pass 1 — resolve + validate. ``writes`` holds (target, bytes);
        # ``deletes`` holds targets. Any escape or a now-live conflict aborts
        # the whole apply before a single byte is written.
        writes: list[tuple[str, Path, bytes]] = []
        deletes: list[tuple[str, Path]] = []
        for status, path in self._changed_entries():
            try:
                target = resolve_workspace_path(source, path, must_exist=False)
            except WorkspacePathError:
                return {"applied": False, "reason": "unsafe_path",
                        "conflicts": preview["conflicts"], "path": path}
            head = self._head_bytes(path)
            if not allow_conflicts:
                # Re-check at write time: the user may have edited since preview.
                baseline = self._baseline_bytes(path)
                src = target.read_bytes() if target.is_file() else None
                if src is not None and src != baseline and src != head:
                    return {"applied": False, "reason": "conflicts",
                            "conflicts": [path],
                            "changed_files": preview["changed_files"]}
            if status == "D" or head is None:
                deletes.append((path, target))
            else:
                writes.append((path, target, head))
        # Pass 2 — apply.
        written: list[str] = []
        deleted: list[str] = []
        for path, target, data in writes:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            written.append(path)
        for path, target in deletes:
            if target.is_file():
                target.unlink()
                deleted.append(path)
        return {
            "applied": True,
            "written": written,
            "deleted": deleted,
            "conflicts": preview["conflicts"],
        }
