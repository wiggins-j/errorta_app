"""F102 P3/P4 — GitHub publishing orchestrators (council-side).

These orchestrate the existing-repo PR (P3) and new-repo (P4) flows. They reach
ALL git / ``gh`` / scan egress ONLY through the ``errorta_tools.runner.publish``
+ ``errorta_tools.runner.secret_scan`` seams — never ``subprocess`` / ``httpx``
directly (Council invariant 3; the RC8 no-egress guard enforces this). The
publish ledger + body builder are pure council modules.

P3 mechanism (RC4): after the human accepts at the merge gate, the accepted
changes are ALREADY written into the user's repo working tree (uncommitted, on
the checked-out/default branch) by ``merge_back``. P3 then: prechecks origin +
worktree safety (RC3/RC6), verifies the dirty set ⊆ the accepted file set
(clobber guard), checks out a NEW branch carrying those changes (the default
branch is NEVER committed to), secret-scans the to-be-pushed tree (RC7), commits
+ pushes the branch, and opens a PR into the default branch.

P4: export the delivered ``master`` tree to a temp dir, scan it, ``git init`` +
commit, then ``gh repo create --private --source --push`` (or stop at a local
git repo when ``local_only``).

Every failure records a redacted ``failed`` PublishEvent; tokens never touch an
event or a returned dict.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from errorta_council.coding.publish_body import build_publish_body
from errorta_council.coding.publish_gate import is_project_delivered
from errorta_council.coding.publish_ledger import PublishLedger
from errorta_tools.runner import publish as egress
from errorta_tools.runner import secret_scan


class PublishGateError(Exception):
    """A pre-push gate refused the publish. ``status`` is the HTTP code the route
    should surface; ``reason`` is a stable machine code; ``detail`` is extra
    redacted context (e.g. scan findings)."""

    def __init__(self, status: int, reason: str, detail: Any = None) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.detail = detail


def _redact_body(text: str | None) -> str:
    """F135: redact a PM-drafted PR body through the same token/path/username
    scrubbers the egress uses, so a supplied body can't leak a secret or a path."""
    if not text:
        return ""
    from errorta_diagnostics.redact import (
        redact_home_path,
        redact_tokens,
        redact_username,
    )
    out, _ = redact_tokens(str(text))
    out, _ = redact_home_path(out)
    out, _ = redact_username(out)
    return out


def _redact_title(text: str | None) -> str:
    """F135: a PM-drafted PR title lands in a PUBLIC PR title + the commit subject,
    so it gets the same token/path/username scrubbing as the body — the body was
    redacted but the title was not (leak parity). Collapsed to a single line and
    length-capped (a title is not a body). Returns '' when nothing usable remains,
    so the caller falls back to the deterministic default."""
    redacted = _redact_body(text)
    single = " ".join(redacted.split())
    return single[:200]


def _evidence_gate_allowed(store: Any, workspace: Any) -> tuple[bool, list[str]]:
    """(allowed, blocker_codes) from the F087-13 merge gate. No open tasks etc."""
    from errorta_council.coding.evidence import merge_review
    review = merge_review(store, workspace)
    gate = review["_gate"]
    return bool(gate.allowed), [b.code for b in gate.blockers]


def _accepted_changed_paths(workspace: Any) -> set[str]:
    """The file set the run delivered (RC3/RC4b clobber guard). Sourced from the
    cumulative merge-back preview — the same changed set that ``merge_back`` wrote
    into the user's repo."""
    try:
        preview = workspace.preview()
    except Exception:
        return set()
    return {str(cf.get("path")) for cf in (preview.get("changed_files") or [])
            if cf.get("path")}


def _tracked_tree_from_dir(root: Path) -> list[tuple[str, bytes]]:
    """Read every regular file under ``root`` (excluding ``.git``) as
    ``(rel_path, bytes)`` for the secret scan. Used for the P4 exported tree."""
    files: list[tuple[str, bytes]] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(root).as_posix()
        if rel == ".git" or rel.startswith(".git/"):
            continue
        try:
            files.append((rel, p.read_bytes()))
        except OSError:
            continue
    return files


def _tree_from_repo_paths(repo_dir: Path, rel_paths: set[str]) -> list[tuple[str, bytes]]:
    """Read the named files from the repo working tree as ``(rel_path, bytes)``
    for the secret scan (RC7 — the to-be-pushed changed set)."""
    files: list[tuple[str, bytes]] = []
    for rel in sorted(rel_paths):
        fp = repo_dir / rel
        try:
            if fp.is_file() and not fp.is_symlink():
                files.append((rel, fp.read_bytes()))
        except OSError:
            continue
    return files


def _scan_or_raise(files: list[tuple[str, bytes]], *, override: bool) -> dict[str, Any]:
    report = secret_scan.scan_tree(files)
    if not report.clean and not override:
        raise PublishGateError(409, "secret_scan_hit", detail=report.to_dict())
    return report.to_dict()


def publish_existing_repo_pr(store: Any, workspace: Any, *,
                             override: bool = False,
                             branch: str | None = None,
                             title: str | None = None,
                             body_override: str | None = None) -> dict[str, Any]:
    """P3 — open a PR on the user's existing repo from the accepted changes.

    F135: ``branch`` / ``title`` / ``body_override`` are optional PM-drafted
    values. When omitted, the deterministic defaults (``errorta/<id>`` / a default
    title / the generated redacted body) are used unchanged. A supplied branch is
    validated by the egress; a supplied body is run through the same redaction as
    the generated one.
    """
    proj = store.get_project()
    project_id = store.project_id
    ledger = PublishLedger(project_id)

    # (1) gate: delivered + merge gate allowed.
    if not is_project_delivered(store):
        raise PublishGateError(409, "not_delivered")
    allowed, blockers = _evidence_gate_allowed(store, workspace)
    if not allowed:
        # Surface the ACTUAL first blocker code (e.g. tests_missing,
        # unreviewed_changes, preview_unavailable, definition_of_done, ...) rather
        # than always labelling the refusal "open_tasks". The merge gate has many
        # blockers; hardcoding "open_tasks" told a fully-delivered project whose
        # every task is done — and which therefore has NO open tasks — that it had
        # open tasks. The full list still rides along in ``detail["blockers"]``.
        reason = blockers[0] if blockers else "publish_gate_blocked"
        raise PublishGateError(409, reason, detail={"blockers": blockers})

    repo_path = proj.repo_path
    if proj.target != "existing" or not repo_path:
        raise PublishGateError(409, "not_existing_target")
    repo_dir = Path(repo_path)
    if not (repo_dir / ".git").exists():
        raise PublishGateError(409, "not_a_git_repo")

    # (2) origin + worktree safety (RC6 / RC3).
    if not egress.has_origin(repo_dir):
        raise PublishGateError(409, "no_origin")
    status = egress.target_repo_status(repo_dir)
    if status.get("detached") or status.get("in_progress"):
        raise PublishGateError(
            409, "repo_state_unsafe",
            detail={"detached": status.get("detached"),
                    "in_progress": status.get("in_progress")})

    # (3) clobber guard: the dirty set must be ⊆ the accepted changed set so
    # UNRELATED user changes cause a refusal (RC3/RC4b).
    accepted = _accepted_changed_paths(workspace)
    dirty = {p for p in (status.get("dirty_paths") or []) if p}
    unrelated = sorted(dirty - accepted)
    if unrelated:
        raise PublishGateError(409, "clobber_unrelated_changes",
                               detail={"unrelated_paths": unrelated[:50]})

    # F135: PM-drafted branch overrides the default, validated up front so a bad
    # name is a clean 400 (not a 502 from deep in the egress).
    if branch:
        try:
            egress._validate_branch_name(branch)
        except egress.PublishEgressError as exc:
            raise PublishGateError(400, "invalid_branch") from exc
    branch = branch or f"errorta/{project_id}"
    # F135: populate the connection target's GitHub identity from the repo origin
    # so the UI can show "Connected to owner/repo" (P3 wrote only repo_path before).
    _owner_repo = None
    try:
        origin = egress._git_run(repo_dir, "remote", "get-url", "origin")
        if origin.returncode == 0:
            _owner_repo = egress.parse_github_origin((origin.stdout or "").strip())
    except Exception:
        _owner_repo = None
    target = ledger.upsert_target(
        kind="existing_repo_pr", repo_path=str(repo_dir),
        github_owner=_owner_repo[0] if _owner_repo else None,
        github_repo=_owner_repo[1] if _owner_repo else None)
    ledger.append_event(target_id=target.target_id, kind="existing_repo_pr",
                        state="planned", branch=branch)

    try:
        # (4) default branch (PR base) + new branch carrying the accepted changes.
        default_branch = egress.detect_default_branch(repo_dir)

        # (5) secret-scan the to-be-pushed tree (RC7), not only the changed-file
        # diff. Include tracked files plus accepted untracked additions.
        tracked = set(egress.git_tracked_paths(repo_dir))
        scan = _scan_or_raise(_tree_from_repo_paths(repo_dir, tracked | accepted),
                              override=override)
        ledger.append_event(target_id=target.target_id, kind="existing_repo_pr",
                            state="scanned", branch=branch)

        # never direct-push the default branch: branch FIRST, carry the changes.
        egress.git_checkout_new_branch(repo_dir, branch, carry=True)

        # (6) commit + push + PR. F135: a PM-drafted body is redacted through the
        # same path as the generated one; a PM-drafted title replaces the default.
        body = _redact_body(body_override) if body_override else build_publish_body(store)
        # F135: a PM-drafted title is redacted (leak parity with the body) before it
        # reaches a public PR title / commit subject; empty-after-redaction falls
        # back to the deterministic default.
        pr_title = f"Errorta: {project_id}"
        if title:
            pr_title = _redact_title(title) or pr_title
        commit_sha = egress.git_commit_all(
            repo_dir, pr_title, body=body)
        ledger.append_event(target_id=target.target_id, kind="existing_repo_pr",
                            state="committed", branch=branch,
                            commit_sha=commit_sha)
        egress.git_push(repo_dir, "origin", branch, set_upstream=True)
        ledger.append_event(target_id=target.target_id, kind="existing_repo_pr",
                            state="pushed", branch=branch, commit_sha=commit_sha)
        pr = egress.gh_pr_create(
            repo_dir, base=default_branch, head=branch,
            title=pr_title, body=body)
        pr_url = pr.get("pr_url", "")
        ledger.append_event(target_id=target.target_id, kind="existing_repo_pr",
                            state="pr_opened", branch=branch,
                            commit_sha=commit_sha, pr_url=pr_url)
    except PublishGateError as exc:
        ledger.append_event(target_id=target.target_id, kind="existing_repo_pr",
                            state="failed", branch=branch, error=exc.reason)
        raise
    except egress.PublishEgressError as exc:
        ledger.append_event(target_id=target.target_id, kind="existing_repo_pr",
                            state="failed", branch=branch, error=str(exc))
        raise PublishGateError(502, "egress_failed", detail=str(exc)) from exc

    return {
        "branch": branch,
        "base": default_branch,
        "commit_sha": commit_sha,
        "pr_url": pr_url,
        "scan": scan,
        "events": [e.to_dict() for e in ledger.list_events()],
    }


def publish_new_github_repo(store: Any, workspace: Any, *, repo_name: str,
                            private: bool = True, local_only: bool = False,
                            override: bool = False) -> dict[str, Any]:
    """P4 — create a new (private-by-default) GitHub repo from the delivered tree,
    or stop at a local git repo when ``local_only``."""
    project_id = store.project_id
    ledger = PublishLedger(project_id)

    # (1) gate: delivered + merge gate allowed.
    if not is_project_delivered(store):
        raise PublishGateError(409, "not_delivered")
    allowed, blockers = _evidence_gate_allowed(store, workspace)
    if not allowed:
        # Surface the ACTUAL first blocker code (e.g. tests_missing,
        # unreviewed_changes, preview_unavailable, definition_of_done, ...) rather
        # than always labelling the refusal "open_tasks". The merge gate has many
        # blockers; hardcoding "open_tasks" told a fully-delivered project whose
        # every task is done — and which therefore has NO open tasks — that it had
        # open tasks. The full list still rides along in ``detail["blockers"]``.
        reason = blockers[0] if blockers else "publish_gate_blocked"
        raise PublishGateError(409, reason, detail={"blockers": blockers})

    # validate the repo name early (reject flag injection) even for local_only,
    # so the destination dir name is also clean.
    try:
        clean_name = egress._validate_repo_name(repo_name)
    except egress.PublishEgressError as exc:
        raise PublishGateError(422, "invalid_repo_name") from exc

    target = ledger.upsert_target(
        kind="new_github_repo", github_repo=clean_name,
        privacy="private" if private else "public")
    ledger.append_event(target_id=target.target_id, kind="new_github_repo",
                        state="planned")

    tmp = Path(tempfile.mkdtemp(prefix=f"f102-newrepo-{project_id}-"))
    export_dir = tmp / clean_name
    try:
        # (2) export the delivered master tree to a temp dir (the to-be-pushed
        # tree). Reuse the workspace export (git-archive backed, tracked files).
        workspace.export(str(export_dir))

        # (3) scan the whole exported tree (RC7).
        files = _tracked_tree_from_dir(export_dir)
        initial_files = sorted(rel for rel, _ in files)
        scan = _scan_or_raise(files, override=override)
        ledger.append_event(target_id=target.target_id, kind="new_github_repo",
                            state="scanned")

        # (4) git init + initial commit.
        body = build_publish_body(store)
        commit_sha = egress.git_init_commit(
            export_dir, f"Initial commit: {clean_name}", body=body)
        ledger.append_event(target_id=target.target_id, kind="new_github_repo",
                            state="committed", commit_sha=commit_sha)

        if local_only:
            # leave the local git repo in place (move it out of the temp dir so it
            # survives cleanup) — deliver under the project's deliverable dir.
            from errorta_council.coding.deliverable import deliverable_dir
            proj = store.get_project()
            dest_root = deliverable_dir(
                project_id,
                proj.delivery_root if proj.target != "existing" else None)
            local_dest = dest_root.parent / f"{project_id}-git-repo"
            if local_dest.exists():
                raise PublishGateError(
                    409, "local_dest_exists", detail={"path": str(local_dest)})
            local_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(export_dir), str(local_dest))
            return {
                "local_only": True,
                "local_path": str(local_dest),
                "commit_sha": commit_sha,
                "initial_files": initial_files,
                "scan": scan,
                "events": [e.to_dict() for e in ledger.list_events()],
            }

        # (5) create the GitHub repo + push.
        result = egress.gh_repo_create(
            clean_name, private=private, source_dir=export_dir, push=True)
        repo_url = result.get("repo_url", "")
        ledger.upsert_target(target_id=target.target_id, kind="new_github_repo",
                             github_repo=clean_name,
                             privacy="private" if private else "public")
        ledger.append_event(target_id=target.target_id, kind="new_github_repo",
                            state="pushed", commit_sha=commit_sha, pr_url=repo_url)
        return {
            "local_only": False,
            "repo_url": repo_url,
            "private": private,
            "commit_sha": commit_sha,
            "initial_files": initial_files,
            "scan": scan,
            "events": [e.to_dict() for e in ledger.list_events()],
        }
    except PublishGateError as exc:
        ledger.append_event(target_id=target.target_id, kind="new_github_repo",
                            state="failed", error=exc.reason)
        raise
    except egress.PublishEgressError as exc:
        ledger.append_event(target_id=target.target_id, kind="new_github_repo",
                            state="failed", error=str(exc))
        raise PublishGateError(502, "egress_failed", detail=str(exc)) from exc
    finally:
        # export_dir is moved out of tmp on the local_only path; otherwise it is
        # consumed by gh repo create — either way removing the temp parent is safe.
        shutil.rmtree(tmp, ignore_errors=True)


__all__ = [
    "PublishGateError",
    "publish_existing_repo_pr",
    "publish_new_github_repo",
]
