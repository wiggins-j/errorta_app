"""F087-05 — per-project isolated worktree + milestone merge-back.

Wraps the F039 ``ApplyWorkspace`` (an isolated, git-backed copy with
traversal-guarded writes, baseline checkpoint, cumulative diff, and conflict-
aware human-accept merge-back) for a Coding Mode project. The team works only
in this worktree; the user's real tree is never touched until an explicit
milestone accept.

Targets:
* ``new``      — the worktree starts from an empty seed (greenfield). "Accept"
  exports the worktree as the project (no real tree to merge into).
* ``existing`` — the worktree is seeded from the user's repo; "accept" merges
  the cumulative diff back into that repo, human-gated + conflict-aware.

Code execution (the tester role running the project's tests) happens via the
F039 sandboxed ``code_exec`` tool inside ``root()`` — this module only owns the
directory lifecycle + artifact bookkeeping.
"""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Any, Optional


class CodingWorkspaceError(RuntimeError):
    pass


# F087-17: build/test artifacts the worktree should never track (so they can't be
# captured by `git add -A` or block a checkout/merge in the branch-per-task flow).
_CODING_GITIGNORE = (
    "__pycache__/\n*.pyc\n*.pyo\n.pytest_cache/\n.mypy_cache/\n.ruff_cache/\n"
    ".coverage\nhtmlcov/\nnode_modules/\ndist/\nbuild/\n*.egg-info/\n.venv/\n"
    "venv/\n.DS_Store\n"
)


class CodingWorkspace:
    def __init__(self, project_id: str, ledger: Any) -> None:
        from errorta_tools.runner.apply_workspace import ApplyWorkspace
        self.project_id = project_id
        self.ledger = ledger
        self._ws = ApplyWorkspace(run_id=f"coding-{project_id}")
        self._target = "new"
        # F087-3 invariant: under concurrent dispatch this "active task" hint is
        # last-writer-wins across worker threads, so every turn-path call MUST
        # pass an explicit ``task_id`` (read_back/write_file/task_root all do) and
        # never rely on the active-task fallback. The only no-arg reader,
        # ``head()``, is used outside worker turns (merge-evidence gathering).
        self._active_task_id: str | None = None

    def root(self) -> Path:
        return self._ws.root

    def exists(self) -> bool:
        return self._ws.exists()

    def head(self) -> str:
        """Current worktree HEAD sha (F087-13 WS-2: the reviewer pins its verdict
        to this; a stale head does not approve). Empty string if unavailable."""
        try:
            if self._active_task_id and self._ws.has_worktree(self._active_task_id):
                return self._ws.head_ref(task_id=self._active_task_id)
            return self._ws.head_ref()
        except Exception:
            return ""

    def branch_head(self, branch: str) -> str:
        return self._ws.branch_head(branch)

    def workspace_fingerprint(self) -> dict[str, Any]:
        return self._ws.workspace_fingerprint()

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """F097: is ``ancestor`` reachable from ``descendant`` (fast-forward)?
        Used by resume integrity to accept a base branch that advanced via merges.
        Fails closed (False) on any error."""
        try:
            return self._ws.is_ancestor(ancestor, descendant)
        except Exception:
            return False

    # -- F087-17 branch-per-task workflow ---------------------------------- #
    def task_branch(self, task_id: str) -> str:
        return f"task-{task_id}"

    def start_task_branch(self, task_id: str, *, base: str = "master") -> str:
        """Create + check out the dev's branch off ``base`` so the task starts
        from everything merged so far. Returns the branch name."""
        branch = self.task_branch(task_id)
        self._ws.worktree_for(task_id, branch=branch, base=base, reset=True)
        self._active_task_id = task_id
        return branch

    def task_root(self, task_id: str, *, branch: str | None = None) -> Path:
        return self._ws.worktree_for(
            task_id, branch=branch or self.task_branch(task_id), reset=False)

    def remove_worktree(self, task_id: str) -> bool:
        if self._active_task_id == task_id:
            self._active_task_id = None
        return self._ws.remove_worktree(task_id)

    def remove_worktree_for_branch(self, branch: str) -> bool:
        removed = self._ws.remove_worktree_for_branch(branch)
        if self._active_task_id and self.task_branch(self._active_task_id) == branch:
            self._active_task_id = None
        return removed

    def prune_worktrees(self) -> list[str]:
        removed = self._ws.prune_worktrees()
        if self._active_task_id in removed:
            self._active_task_id = None
        return removed

    def destroy(self) -> None:
        """Remove Errorta-owned worktree state for this coding project."""
        self._active_task_id = None
        self._ws.destroy()

    def read_back(
        self, *, task_id: str | None = None, max_files: int = 40,
        max_bytes: int = 24_000,
    ) -> str:
        """A bounded snapshot of the CURRENT tree's files + contents, for the dev
        prompt — so it EXTENDS existing code instead of regenerating from scratch.
        Returns '' for an empty tree."""
        task_id = task_id or self._active_task_id
        files = [
            f for f in self._ws.list_files(task_id=task_id)
            if f != ".errorta-meta.json"
        ]
        if not files:
            return ""
        chunks: list[str] = []
        budget = max_bytes
        for path in files[:max_files]:
            body = self._ws.read_file(path, task_id=task_id)
            if body is None:
                continue
            snippet = body[:budget]
            chunks.append(f"--- {path} ---\n{snippet}")
            budget -= len(snippet)
            if budget <= 0:
                chunks.append("... (read-back truncated) ...")
                break
        return "\n\n".join(chunks)

    def pr_diff(self, branch: str, *, base: str = "master") -> str:
        return self._ws.branch_diff(branch, base=base)

    def merge_pr(self, branch: str, *, base: str = "master") -> dict[str, Any]:
        """PM-approved integration: merge the PR branch into ``base``."""
        res = self._ws.merge_branch(branch, into=base)
        if res.get("merged"):
            if self._active_task_id and self.task_branch(self._active_task_id) == branch:
                self._active_task_id = None
        return res

    def update_branch_from_base(
        self, task_id: str, branch: str, *, base: str = "master",
    ) -> dict[str, Any]:
        """F087-3: bring ``base`` into a PR ``branch`` before re-test, so a PR that
        was validated against an older base is revalidated against the integrated
        tree. Conflict-aware (see ApplyWorkspace.update_branch_from_base)."""
        return self._ws.update_branch_from_base(task_id, branch, base=base)

    def checkout(self, branch: str) -> None:
        if branch == "master":
            self._active_task_id = None
            self._ws.checkout(branch)
            return
        task_id = self._ws.task_id_for_branch(branch)
        if task_id is not None:
            self._active_task_id = task_id
            return
        self._ws.checkout(branch)

    def delete_branch(self, branch: str) -> bool:
        self.remove_worktree_for_branch(branch)
        return self._ws.delete_branch(branch)

    def list_branches(self) -> list[str]:
        return self._ws.list_branches()

    def list_files(self, *, task_id: str | None = None,
                   scope: str | None = None) -> list[str]:
        """List tracked files. ``scope="master"`` returns the MERGED tree (via
        ``git ls-tree master``, no checkout switch) — the F139 WS-B source of truth
        for "what is actually in the project". Otherwise lists the working checkout
        (``task_id``'s worktree, or the primary checkout)."""
        if scope == "master":
            return self._ws.list_files_on_ref("master")
        return self._ws.list_files(task_id=task_id)

    def read_master_file(self, rel_path: str) -> bytes | None:
        """Read a committed blob from master without touching the checkout."""
        from errorta_tools.runner.apply_workspace import (
            _git_bytes,
            _git_try,
            _safe_rel_pathspec,
        )

        safe = _safe_rel_pathspec(rel_path)
        ref = f"master:./{safe}"
        rc, kind, _err = _git_try(self._ws.root, "cat-file", "-t", ref)
        if rc != 0 or kind.strip() != "blob":
            return None
        rc, out = _git_bytes(self._ws.root, "show", ref)
        if rc != 0:
            return None
        return out

    def write_master_file(self, rel_path: str, content: str) -> str:
        """F105: write a human edit directly to the committed ``master`` ref
        without touching the shared working-tree checkout (a live run may hold it
        on a task branch). Returns the new ``master`` commit sha. Path is
        traversal-guarded inside ApplyWorkspace via ``_safe_rel_pathspec``."""
        return self._ws.write_master_file(rel_path, content)

    def is_on_master(self, rel_path: str) -> bool:
        from errorta_tools.runner.apply_workspace import _git_try, _safe_rel_pathspec

        try:
            safe = _safe_rel_pathspec(rel_path)
        except Exception:
            return False
        rc, out, _err = _git_try(self._ws.root, "cat-file", "-t", f"master:./{safe}")
        return rc == 0 and out.strip() == "blob"

    def export(self, dest: str) -> str:
        """F087-20: deliver the integrated master tree to a user-facing folder."""
        self._ws.checkout("master")
        return self._ws.export_master(dest)

    def set_target(self, target: str) -> None:
        """Restore the target on a reconstructed workspace (routes use this)."""
        self._target = target

    def setup(self, *, target: str, repo_path: Optional[str]) -> Path:
        """Create the isolated worktree. ``new`` seeds from an empty dir;
        ``existing`` seeds from ``repo_path`` (the user's repo)."""
        self._target = target
        if target == "existing":
            if not repo_path or not Path(repo_path).is_dir():
                raise CodingWorkspaceError("existing target needs a valid repo_path")
            source: str | Path = repo_path
        else:
            # Empty seed for a greenfield project.
            source = Path(tempfile.mkdtemp(prefix=f"coding-seed-{self.project_id}-"))
        root = self._ws.ensure(source)
        # F087-17: ignore build/test artifacts so a test run's __pycache__/.pyc
        # (etc.) is never staged by `git add -A` or made to block a branch
        # checkout/merge; F138: record the clean seed HEAD. (Idempotent — `.gitignore`
        # only seeded when absent, so a real repo's is not clobbered.)
        self._finalize_seed()
        return root

    def write_file(self, rel_path: str, content: str | bytes, *, task_id: str,
                   summary: str = "") -> str:
        """Write a file into the worktree (traversal-guarded by ApplyWorkspace)
        and record it in the ledger artifact index. Returns the new HEAD sha.

        ``content`` may be ``bytes`` for a binary asset (a real PNG/font/etc.);
        bytes are written verbatim and the provenance hash is over the raw bytes.
        The F140 destructive-write guard still applies whenever the EXISTING file
        is text — even if the incoming payload is base64-wrapped bytes — so a
        destructive stub cannot dodge the guard by being emitted as base64. It is
        skipped only when the existing file is itself a binary asset (a legit
        sprite/font re-export) or the file is new."""
        is_binary = isinstance(content, (bytes, bytearray))
        existed = False
        old_content: str | None = None  # existing file as text; None if binary/new/unreadable
        try:
            from errorta_tools.runner.apply_workspace import resolve_workspace_path
            root = (
                self.task_root(task_id)
                if self._ws.has_worktree(task_id)
                else self._ws.root
            )
            target = resolve_workspace_path(root, rel_path, must_exist=False)
            existed = target.exists()
            if existed and target.is_file():
                try:
                    raw = target.read_bytes()
                except OSError:
                    raw = None
                # A NUL byte marks a genuine binary asset (PNG/font/…); the
                # text-shape guard does not apply and would false-trip on a legit
                # binary re-export. A non-UTF-8 TEXT file (e.g. latin-1 source)
                # has no NUL and is still decoded leniently so it stays guarded.
                if raw is not None and b"\x00" not in raw:
                    old_content = raw.decode("utf-8", errors="replace")
        except Exception:
            existed = False
        # F140: refuse a write that would DESTROY an existing text file — a
        # placeholder "keep the file" sentinel written literally, or a large file
        # collapsed to a stub / blanked out. Runs regardless of whether the new
        # payload is text or base64-wrapped bytes (a base64 stub over a real .gd is
        # the same delete-the-codebase fumble). Raised so the dev turn records a
        # failed tool event and re-queues into the F136/F127 escalate-up ladder,
        # never opening a PR that deletes the codebase.
        if old_content is not None:
            new_as_text = (
                content.decode("utf-8", errors="replace") if is_binary else content
            )
            from .write_guard import BLOCKED_REASON, classify_destructive_write
            if classify_destructive_write(old_content, new_as_text) is not None:
                raise CodingWorkspaceError(BLOCKED_REASON)
        scoped_task_id = task_id if self._ws.has_worktree(task_id) else None
        # F139 WS-C: the dev write path never creates an empty commit. Re-emitting
        # an existing file byte-for-byte is a no-op, not progress.
        head_before = self._ws.head_ref(task_id=scoped_task_id)
        head = self._ws.write_and_commit(
            rel_path, content, task_id=scoped_task_id, allow_empty=False)
        # Only record provenance when a commit actually landed. A no-op re-emit
        # produces no commit (head unchanged); recording it would churn the
        # artifact ledger on writes git treats as nothing — the very ledger↔git
        # drift F139 exists to kill.
        if head != head_before:
            raw = bytes(content) if is_binary else content.encode("utf-8")
            sha = hashlib.sha256(raw).hexdigest()
            self.ledger.upsert_artifact(
                path=rel_path, status="modified" if existed else "created",
                last_task_id=task_id, content_sha256=sha, summary=summary,
            )
        return head

    def changed_paths(self, branch: str, *, base: str = "master") -> list[str]:
        """F139 WS-C: file paths ``branch`` changes vs ``base`` (adds + modifies +
        deletes) — its net contribution. Empty means no net change. Callers pass
        the authoritative branch name (`pr["branch"]` for a PR, or
        ``task_branch(task_id)`` for an in-flight dev turn)."""
        return self._ws.changed_paths(branch, base=base)

    def preview(self) -> dict[str, Any]:
        """The cumulative diff the team has built, for human review at a
        milestone."""
        return self._ws.merge_back_preview()

    def has_unaccepted_changes(self) -> bool:
        """F138: True iff the snapshot holds committed run work beyond its clean
        seed — on ``master`` OR any un-merged per-task branch (so an interrupted
        run's task-branch commits aren't silently discarded by a re-seed). Uses the
        recorded seed HEAD (immune to the setup-injected ``.gitignore`` commit and
        independent of the source repo). Snapshot-only: never touches ``repo_path``,
        so a missing source can't turn this into a 500 (the route validates
        ``repo_path`` separately). Conservative fallback (differs-from-source) only
        when no seed HEAD was ever recorded."""
        if not self._ws.exists():
            return False
        if self._ws.seed_head() is None:
            try:
                return bool(self.preview().get("changed_files"))
            except Exception:
                return True  # can't tell -> assume work exists (safe: prompts confirm)
        return self._ws.has_work_beyond_seed() or self._ws.has_uncommitted_work()

    def _finalize_seed(self) -> None:
        """Post-(re)seed: ensure the build/test-artifact ``.gitignore`` (F087-17)
        and record the clean seed HEAD (F138). Shared by ``setup`` and ``reseed`` so
        a re-seeded snapshot is byte-identical in shape to a freshly-set-up one."""
        try:
            if self._ws.read_file(".gitignore") is None:
                self._ws.write_and_commit(".gitignore", _CODING_GITIGNORE)
        except Exception:
            pass
        self._ws.set_seed_head()

    def reseed(self, repo_path: str | Path) -> Path:
        """F138: prune owned task worktrees, then atomically re-seed the snapshot
        from ``repo_path`` (discarding the prior snapshot). Callers must gate this
        behind the un-accepted-work check + explicit confirm."""
        self.prune_worktrees()
        self._active_task_id = None
        root = self._ws.reseed(repo_path)
        self._finalize_seed()
        return root

    def accept(self, *, confirm: bool, allow_conflicts: bool = False) -> dict[str, Any]:
        """Apply the worktree's work. Human-gated: ``confirm`` MUST be true.
        For an ``existing`` target this merges back into the user's repo; for a
        ``new`` target it returns the worktree root as the deliverable (no real
        tree to merge into)."""
        if not confirm:
            raise CodingWorkspaceError("merge-back requires explicit confirm=true")
        if self._target == "new":
            return {"mode": "new_project", "root": str(self._ws.root),
                    "diff": self._ws.cumulative_diff()}
        return self._ws.merge_back(allow_conflicts=allow_conflicts)
