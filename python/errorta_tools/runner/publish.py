"""F102 — egress primitives for Coding GitHub publishing.

This is the sanctioned egress boundary (``errorta_tools``) for F102's publishing
flow. It owns the only ``subprocess`` (git / ``gh``) and ``zipfile`` egress;
``errorta_council`` reaches NONE of it directly (Council invariant 3 — the
council-side ``publish_ledger`` is pure stdlib). The route layer in
``errorta_app`` calls into here.

This slice (P1 + P2) covers manual export (zip / patch) and read-only auth
detection (``gh`` presence + login). It NEVER pushes, NEVER creates a repo, and
NEVER returns or logs a token — ``gh_auth_status`` returns only presence + the
login name. Push / PR / repo-create + device-flow auth land in the next slice and
plug into the same seam.

git / ``gh`` run as plain argv subprocesses (no shell, fixed argv, isolated
behaviour). ``gh`` resolution mirrors the F040 CLI-binary pattern: a GUI ``.app``
inherits a minimal PATH that excludes ``~/.local/bin`` / ``/opt/homebrew/bin``,
so PATH is augmented before resolving.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

from errorta_model_gateway.providers._cli_common import _clean_subprocess_env

_GIT_IDENTITY = (
    "-c", "user.email=runner@errorta.local",
    "-c", "user.name=Errorta Runner",
    "-c", "commit.gpgsign=false",
)

# A ``gh api user`` / ``gh auth status`` probe should never block the request.
_GH_TIMEOUT_SECONDS = 8.0
_TERMINATE_GRACE_SECONDS = 3.0

# A push / repo-create / PR-create network round-trip gets a generous-but-bounded
# budget; these are sync subprocess.run calls (RC1) with start_new_session=True.
_GH_WRITE_TIMEOUT_SECONDS = 120.0
_GIT_WRITE_TIMEOUT_SECONDS = 120.0

# A GitHub repo name + a git branch name must match a strict pattern (no flag
# injection, no path traversal, no shell metacharacters). GitHub repo names allow
# alnum + ``.-_``; branch names additionally allow ``/``.
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")


class PublishEgressError(RuntimeError):
    """A publish egress (git archive / zip) operation failed (stable reason)."""


def _git_archive_tar(workspace_root: Path, ref: str) -> bytes:
    """Return the ``git archive <ref>`` tar stream (tracked files only, no
    ``.git``). Raises :class:`PublishEgressError` on failure (never surfaces raw
    git stderr — could echo paths/content)."""
    proc = subprocess.run(  # noqa: S603 — argv-only, no shell
        ["git", "-C", str(workspace_root), *_GIT_IDENTITY, "archive", ref],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise PublishEgressError("git_failed:archive")
    return proc.stdout


def build_zip_export(
    workspace_root: str | Path, dest_zip: str | Path, *, ref: str = "master"
) -> Path:
    """Stream ``git archive <ref>`` (a tar) and repackage it as a real ``.zip``.

    Tracked files only (so no ``.git`` and no build artifacts). The write is
    atomic: a temp file in the destination directory is fully written then
    ``os.replace``-d into place, so a crash mid-write can never leave a truncated
    ``.zip``. Returns the final path.
    """
    workspace_root = Path(workspace_root)
    dest_zip = Path(dest_zip)
    dest_zip.parent.mkdir(parents=True, exist_ok=True)

    tar_bytes = _git_archive_tar(workspace_root, ref)

    fd, tmp = tempfile.mkstemp(
        prefix=".publish-zip-", suffix=".tmp", dir=str(dest_zip.parent)
    )
    os.close(fd)
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as tar, \
                zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                extracted = tar.extractfile(member)
                if extracted is None:
                    continue
                zf.writestr(member.name, extracted.read())
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, dest_zip)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return dest_zip


def build_patch(
    workspace_root: str | Path, *, baseline_ref: str | None = None, ref: str = "HEAD"
) -> str:
    """Return the unified diff for the delivered work as text.

    Defaults to the full patch vs the baseline checkpoint (the run's first,
    parentless commit) — the same ``baseline..HEAD`` range the coding workspace's
    ``cumulative_diff`` uses. Wrapping the git call here keeps ``errorta_council``
    from shelling out. Raises :class:`PublishEgressError` on failure.
    """
    workspace_root = Path(workspace_root)
    if baseline_ref is None:
        rc, baseline_out = _git_text(
            workspace_root, "rev-list", "--max-parents=0", ref)
        if rc != 0 or not baseline_out.strip():
            raise PublishEgressError("git_failed:baseline")
        baseline_ref = baseline_out.strip().splitlines()[0]
    rc, out = _git_text(workspace_root, "diff", f"{baseline_ref}..{ref}")
    if rc != 0:
        raise PublishEgressError("git_failed:diff")
    return out


def _git_text(workspace_root: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(  # noqa: S603 — argv-only, no shell
        ["git", "-C", str(workspace_root), *_GIT_IDENTITY, *args],
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout


# -- gh auth detection (read-only) ----------------------------------------- #


def get_gh_binary() -> str | None:
    """Resolve the ``gh`` CLI on PATH, augmenting PATH for a frozen GUI ``.app``
    (mirrors the F040 CLI-binary resolution). Returns the absolute path or None.

    Also checks a couple of common install locations directly in case ``which``
    misses them under a minimal inherited environment.
    """
    env = _clean_subprocess_env()
    found = shutil.which("gh", path=env.get("PATH"))
    if found:
        return found
    for candidate in (
        "/opt/homebrew/bin/gh",
        "/usr/local/bin/gh",
        "/usr/bin/gh",
        str(Path.home() / ".local/bin/gh"),
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def gh_auth_status() -> dict:
    """Report ``gh`` presence + the logged-in GitHub login WITHOUT returning a
    token.

    Returns ``{"gh_present": bool, "login": str | None}``. ``login`` is the
    GitHub username from ``gh api user --jq .login`` when authenticated, else
    None. This NEVER raises (a missing/hung/erroring ``gh`` degrades to
    ``gh_present`` reflecting the binary and ``login`` None) and NEVER returns a
    token — only the public login name.
    """
    binary = get_gh_binary()
    if binary is None:
        return {"gh_present": False, "login": None}
    try:
        proc = subprocess.run(  # noqa: S603 — argv-only, no shell
            [binary, "api", "user", "--jq", ".login"],
            capture_output=True, text=True,
            env=_clean_subprocess_env(),
            timeout=_GH_TIMEOUT_SECONDS,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        return {"gh_present": True, "login": None}
    except Exception:
        return {"gh_present": True, "login": None}
    if proc.returncode != 0:
        # gh is present but not logged in (or the API call failed).
        return {"gh_present": True, "login": None}
    login = (proc.stdout or "").strip()
    return {"gh_present": True, "login": login or None}


# -- P3/P4 write egress: branch / commit / push / PR / repo-create --------- #


def _validate_branch_name(branch: str) -> str:
    if not _BRANCH_NAME_RE.match(branch or "") or ".." in branch:
        raise PublishEgressError("invalid_branch_name")
    return branch


def _validate_repo_name(name: str) -> str:
    if not _REPO_NAME_RE.match(name or "") or ".." in name:
        raise PublishEgressError("invalid_repo_name")
    return name


def _redact(text: str | None) -> str:
    """Redact tokens / home path / username from a git/gh stderr blob before it is
    surfaced. Never let a raw error echo a path or a token."""
    if not text:
        return ""
    from errorta_diagnostics.redact import (
        redact_home_path,
        redact_tokens,
        redact_username,
    )
    out, _ = redact_tokens(text)
    out, _ = redact_home_path(out)
    out, _ = redact_username(out)
    return out


def _git_run(repo_dir: Path, *args: str,
             timeout: float = _GIT_WRITE_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    """Run a git subprocess (argv-only, no shell, sync, isolated session). Returns
    the CompletedProcess; callers branch on returncode + redact stderr."""
    return subprocess.run(  # noqa: S603 — argv-only, no shell
        ["git", "-C", str(repo_dir), *_GIT_IDENTITY, *args],
        capture_output=True, text=True,
        env=_clean_subprocess_env(),
        timeout=timeout,
        start_new_session=True,
    )


def _gh_run(binary: str, *args: str, cwd: Path | None = None,
            timeout: float = _GH_WRITE_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    """Run a ``gh`` subprocess (argv-only, no shell, sync, isolated session).
    ``gh`` owns the credential — Errorta never reads/sets a token here."""
    return subprocess.run(  # noqa: S603 — argv-only, no shell
        [binary, *args],
        capture_output=True, text=True,
        env=_clean_subprocess_env(),
        cwd=str(cwd) if cwd is not None else None,
        timeout=timeout,
        start_new_session=True,
    )


def has_origin(repo_dir: str | Path) -> bool:
    """True iff the repo has an ``origin`` remote (RC6). Fail-closed (False) on
    any error so a missing/unreadable remote can't be mistaken for present."""
    try:
        proc = _git_run(Path(repo_dir), "remote", "get-url", "origin",
                        timeout=_GIT_WRITE_TIMEOUT_SECONDS)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0 and bool((proc.stdout or "").strip())


def detect_default_branch(repo_dir: str | Path) -> str:
    """Detect the repo's default branch (RC5).

    1. ``git symbolic-ref refs/remotes/origin/HEAD`` -> strip ``origin/``.
    2. fallback ``gh repo view --json defaultBranchRef``.
    3. final fallback: probe ``main`` then ``master`` (a local ref that exists);
       else literal ``main``.
    """
    repo = Path(repo_dir)
    try:
        proc = _git_run(repo, "symbolic-ref", "refs/remotes/origin/HEAD",
                        timeout=_GIT_WRITE_TIMEOUT_SECONDS)
        if proc.returncode == 0:
            ref = (proc.stdout or "").strip()
            # refs/remotes/origin/<branch>
            if "/" in ref:
                name = ref.rsplit("/", 1)[-1]
                if name:
                    return name
    except (subprocess.TimeoutExpired, OSError):
        pass

    binary = get_gh_binary()
    if binary is not None:
        try:
            proc = _gh_run(
                binary, "repo", "view", "--json", "defaultBranchRef",
                "--jq", ".defaultBranchRef.name", cwd=repo,
                timeout=_GH_TIMEOUT_SECONDS)
            if proc.returncode == 0:
                name = (proc.stdout or "").strip()
                if name:
                    return name
        except (subprocess.TimeoutExpired, OSError):
            pass

    for candidate in ("main", "master"):
        try:
            proc = _git_run(repo, "show-ref", "--verify", "--quiet",
                            f"refs/heads/{candidate}",
                            timeout=_GIT_WRITE_TIMEOUT_SECONDS)
            if proc.returncode == 0:
                return candidate
        except (subprocess.TimeoutExpired, OSError):
            continue
    return "main"


def target_repo_status(repo_dir: str | Path) -> dict:
    """Inspect the target repo worktree (RC3).

    Returns ``{clean, dirty_paths, detached, in_progress}``:
    * ``dirty_paths`` — porcelain entries (relative paths) with uncommitted
      changes / untracked files.
    * ``clean`` — no dirty paths.
    * ``detached`` — HEAD is not on a branch.
    * ``in_progress`` — a merge / rebase / cherry-pick / bisect is underway.
    Fail-closed: an unreadable repo reports detached + in_progress True + not
    clean so a caller refuses rather than pushes blind.
    """
    repo = Path(repo_dir)
    fail_closed = {"clean": False, "dirty_paths": [], "detached": True,
                   "in_progress": True}
    try:
        proc = _git_run(repo, "status", "--porcelain",
                        timeout=_GIT_WRITE_TIMEOUT_SECONDS)
    except (subprocess.TimeoutExpired, OSError):
        return fail_closed
    if proc.returncode != 0:
        return fail_closed
    dirty: list[str] = []
    for line in (proc.stdout or "").splitlines():
        if not line.strip():
            continue
        # porcelain v1: "XY <path>" (path begins at col 4).
        dirty.append(line[3:] if len(line) > 3 else line.strip())

    detached = False
    try:
        head = _git_run(repo, "symbolic-ref", "-q", "HEAD",
                        timeout=_GIT_WRITE_TIMEOUT_SECONDS)
        detached = head.returncode != 0
    except (subprocess.TimeoutExpired, OSError):
        detached = True

    in_progress = False
    try:
        git_dir_proc = _git_run(repo, "rev-parse", "--git-dir",
                                timeout=_GIT_WRITE_TIMEOUT_SECONDS)
        if git_dir_proc.returncode == 0:
            git_dir = (git_dir_proc.stdout or "").strip()
            gp = Path(git_dir)
            if not gp.is_absolute():
                gp = repo / gp
            markers = ("MERGE_HEAD", "rebase-merge", "rebase-apply",
                       "CHERRY_PICK_HEAD", "BISECT_LOG", "REVERT_HEAD")
            in_progress = any((gp / m).exists() for m in markers)
    except (subprocess.TimeoutExpired, OSError):
        in_progress = True

    return {
        "clean": not dirty,
        "dirty_paths": dirty,
        "detached": detached,
        "in_progress": in_progress,
    }


def git_tracked_paths(repo_dir: str | Path) -> list[str]:
    """Return tracked file paths relative to ``repo_dir``.

    This is used by the publish secret scan so P3 scans the to-be-pushed tree
    instead of only the changed-file diff. Paths are NUL-delimited to avoid
    newline ambiguity. Raises on git failure so callers fail closed.
    """
    proc = _git_run(Path(repo_dir), "ls-files", "-z")
    if proc.returncode != 0:
        raise PublishEgressError("git_failed:ls_files")
    return [p for p in (proc.stdout or "").split("\0") if p]


def git_checkout_new_branch(repo_dir: str | Path, branch: str, *,
                            carry: bool = True) -> None:
    """Create + check out a NEW branch off the current HEAD (RC4 step c).

    ``carry=True`` keeps the working-tree changes (the accepted, uncommitted
    merge-back) on the new branch — ``git checkout -b`` carries them by default,
    so we never stash/reset. Refuses to clobber an existing branch (``-b`` fails
    if it exists). The default branch is NEVER committed to."""
    branch = _validate_branch_name(branch)
    proc = _git_run(Path(repo_dir), "checkout", "-b", branch)
    if proc.returncode != 0:
        raise PublishEgressError("git_failed:checkout_branch")


def git_commit_all(repo_dir: str | Path, message: str, *, body: str = "") -> str:
    """``git add -A`` then commit with ``message`` (subject) + ``body`` (passed
    via a temp file with ``-F``, NEVER argv — a body could carry newlines /
    metacharacters). Returns the new commit sha. Raises on failure."""
    repo = Path(repo_dir)
    add = _git_run(repo, "add", "-A")
    if add.returncode != 0:
        raise PublishEgressError("git_failed:add")

    full = message if not body else f"{message}\n\n{body}"
    fd, tmp = tempfile.mkstemp(prefix=".publish-commitmsg-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(full)
        commit = _git_run(repo, "commit", "-F", tmp)
        if commit.returncode != 0:
            raise PublishEgressError("git_failed:commit")
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    rev = _git_run(repo, "rev-parse", "HEAD")
    if rev.returncode != 0:
        raise PublishEgressError("git_failed:rev_parse")
    return (rev.stdout or "").strip()


def git_push(repo_dir: str | Path, remote: str, branch: str, *,
             set_upstream: bool = True) -> dict:
    """Push ``branch`` to ``remote`` (RC4 step f). Returns
    ``{pushed: True, branch}`` on success; raises :class:`PublishEgressError`
    with a redacted reason on failure. Never pushes a default branch directly —
    the caller branches first; this just transports the explicit branch."""
    branch = _validate_branch_name(branch)
    if remote not in ("origin",):
        # Only origin is a valid push target for the publish flow.
        raise PublishEgressError("invalid_remote")
    args = ["push"]
    if set_upstream:
        args.append("--set-upstream")
    args += [remote, branch]
    proc = _git_run(Path(repo_dir), *args)
    if proc.returncode != 0:
        raise PublishEgressError("git_failed:push:" + _redact(proc.stderr)[:200])
    return {"pushed": True, "branch": branch}


def gh_pr_create(repo_dir: str | Path, *, base: str, head: str, title: str,
                 body: str) -> dict:
    """``gh pr create --base --head --title --body-file <tmp>`` (RC4 step g).

    Body via a temp file (never argv). Returns ``{pr_url}``. Raises on failure
    with a redacted reason. ``gh`` owns the credential."""
    binary = get_gh_binary()
    if binary is None:
        raise PublishEgressError("gh_absent")
    base = _validate_branch_name(base)
    head = _validate_branch_name(head)
    fd, tmp = tempfile.mkstemp(prefix=".publish-prbody-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body or "")
        proc = _gh_run(
            binary, "pr", "create",
            "--base", base, "--head", head,
            "--title", title, "--body-file", tmp,
            cwd=Path(repo_dir))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    if proc.returncode != 0:
        raise PublishEgressError("gh_failed:pr_create:" + _redact(proc.stderr)[:200])
    # gh prints the PR URL on stdout.
    pr_url = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else ""
    return {"pr_url": pr_url}


def gh_repo_create(name: str, *, private: bool = True, source_dir: str | Path,
                   push: bool = True) -> dict:
    """``gh repo create <name> --private --source <dir> --push`` (P4).

    Validates ``name`` against a strict pattern (reject flag injection). Returns
    ``{repo_url}``. Raises on failure with a redacted reason. ``gh`` owns the
    credential."""
    binary = get_gh_binary()
    if binary is None:
        raise PublishEgressError("gh_absent")
    name = _validate_repo_name(name)
    args = ["repo", "create", name,
            "--private" if private else "--public",
            "--source", str(source_dir)]
    if push:
        args.append("--push")
    proc = _gh_run(binary, *args, cwd=Path(source_dir))
    if proc.returncode != 0:
        raise PublishEgressError(
            "gh_failed:repo_create:" + _redact(proc.stderr)[:200])
    repo_url = ""
    out = (proc.stdout or "").strip().splitlines()
    for line in out:
        line = line.strip()
        if line.startswith("http"):
            repo_url = line
            break
    if not repo_url and out:
        repo_url = out[-1].strip()
    return {"repo_url": repo_url}


def git_init_commit(source_dir: str | Path, message: str, *, body: str = "",
                    default_branch: str = "main") -> str:
    """Initialize a git repo in ``source_dir`` and create the initial commit (P4
    local-only path). Returns the commit sha. Idempotent-ish: re-init is safe but
    a second commit only happens if there is something to commit."""
    repo = Path(source_dir)
    init = _git_run(repo, "init", "-b", default_branch)
    if init.returncode != 0:
        # older git without -b: init then rename
        init2 = _git_run(repo, "init")
        if init2.returncode != 0:
            raise PublishEgressError("git_failed:init")
        _git_run(repo, "checkout", "-b", default_branch)
    return git_commit_all(repo, message, body=body)


# -- F135: import egress — clone / init / remote-add + GitHub-origin parsing -- #

# A clone can pull a large repo over the network; give it a generous-but-bounded
# budget well above the 120s write timeout so a real clone doesn't time out, while
# a hung clone still fails cleanly instead of hanging the request.
_GIT_CLONE_TIMEOUT_SECONDS = 600.0
# Listing remote branches (F141 WS-C) is a metadata-only round-trip — a much
# tighter budget than a full clone so the import UI stays responsive.
_GIT_LS_REMOTE_TIMEOUT_SECONDS = 20.0
_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Accept only GitHub HTTPS or SSH remotes; owner/repo are a strict charset (no
# path traversal, no flag injection). ``.git`` suffix + trailing slash optional.
_GH_HTTPS_RE = re.compile(
    r"^https://github\.com/([A-Za-z0-9][A-Za-z0-9._-]*)/([A-Za-z0-9][A-Za-z0-9._-]*?)(?:\.git)?/?$")
_GH_SSH_RE = re.compile(
    r"^git@github\.com:([A-Za-z0-9][A-Za-z0-9._-]*)/([A-Za-z0-9][A-Za-z0-9._-]*?)(?:\.git)?/?$")
_GH_SSH_URL_RE = re.compile(
    r"^ssh://git@github\.com/([A-Za-z0-9][A-Za-z0-9._-]*)/([A-Za-z0-9][A-Za-z0-9._-]*?)(?:\.git)?/?$")


def parse_github_origin(url: str | None) -> tuple[str, str] | None:
    """Parse a GitHub remote URL into ``(owner, repo)`` or ``None`` if it is not a
    recognizable GitHub HTTPS/SSH remote. Pure (no egress). Rejects non-GitHub and
    malformed origins so a half-connected project is never created."""
    if not url:
        return None
    text = url.strip()
    for pat in (_GH_HTTPS_RE, _GH_SSH_RE, _GH_SSH_URL_RE):
        m = pat.match(text)
        if m:
            return m.group(1), m.group(2)
    return None


def validate_clone_url(url: str | None) -> str:
    """Return ``url`` iff it is a valid GitHub HTTPS/SSH remote, else raise. This
    is the argv-injection guard for ``git clone`` / ``git remote add`` — the url is
    also passed after ``--`` so a flag-shaped value can never be read as an
    option."""
    if parse_github_origin(url) is None:
        raise PublishEgressError("invalid_repo_url")
    return str(url).strip()


def git_clone(repo_url: str, dest: str | Path, *, ref: str | None = None,
              shallow: bool = False,
              timeout: float = _GIT_CLONE_TIMEOUT_SECONDS) -> Path:
    """Clone ``repo_url`` into ``dest`` (F135). ``gh`` owns the credential (it
    configures git's credential helper) — no token is ever injected into the URL.
    The URL is validated + passed after ``--``; an optional ``ref`` is validated as
    a branch name. Raises :class:`PublishEgressError` (redacted) on failure or
    timeout; the caller surfaces a clean job error, never a hang."""
    url = validate_clone_url(repo_url)
    dest = Path(dest)
    args = ["git", *_GIT_IDENTITY, "clone"]
    if shallow:
        args += ["--depth", "1"]
    if ref:
        args += ["--branch", _validate_branch_name(ref)]
    args += ["--", url, str(dest)]
    try:
        proc = subprocess.run(  # noqa: S603 — argv-only, no shell, url after --
            args, capture_output=True, text=True,
            env=_clean_subprocess_env(), timeout=timeout, start_new_session=True)
    except subprocess.TimeoutExpired as exc:
        raise PublishEgressError("git_failed:clone_timeout") from exc
    if proc.returncode != 0:
        raise PublishEgressError("git_failed:clone:" + _redact(proc.stderr)[:200])
    return dest


def list_remote_branches(repo_url: str) -> dict:
    """List a GitHub repo's branches WITHOUT cloning (F141 WS-C). Uses
    ``git ls-remote`` so the same ``gh`` credential helper that ``git_clone`` uses
    authenticates private repos — no token is ever injected into the URL. The URL
    is validated + passed after ``--`` (flag-injection safe); the child mirrors
    ``git_clone``'s isolation (clean env, new session, bounded timeout). Raises
    :class:`PublishEgressError` (redacted) on failure/timeout; the route surfaces a
    clean structured error so the import UI can fall back to the free-text branch
    field. Returns ``{"branches": [...], "default_branch": "..."}``."""
    url = validate_clone_url(repo_url)
    try:
        heads = subprocess.run(  # noqa: S603 — argv-only, no shell, url after --
            ["git", *_GIT_IDENTITY, "ls-remote", "--heads", "--", url],
            capture_output=True, text=True,
            env=_clean_subprocess_env(),
            timeout=_GIT_LS_REMOTE_TIMEOUT_SECONDS, start_new_session=True)
    except subprocess.TimeoutExpired as exc:
        raise PublishEgressError("git_failed:ls_remote_timeout") from exc
    if heads.returncode != 0:
        raise PublishEgressError(
            "git_failed:ls_remote:" + _redact(heads.stderr)[:200])
    branches: list[str] = []
    for line in (heads.stdout or "").splitlines():
        # each line: "<sha>\trefs/heads/<name>"
        _, _, ref = line.partition("\t")
        name = ref.strip()
        if name.startswith("refs/heads/"):
            branches.append(name[len("refs/heads/"):])

    default_branch = ""
    try:
        symref = subprocess.run(  # noqa: S603 — argv-only, no shell, url after --
            ["git", *_GIT_IDENTITY, "ls-remote", "--symref", "--", url, "HEAD"],
            capture_output=True, text=True,
            env=_clean_subprocess_env(),
            timeout=_GIT_LS_REMOTE_TIMEOUT_SECONDS, start_new_session=True)
    except subprocess.TimeoutExpired:
        symref = None
    if symref is not None and symref.returncode == 0:
        for line in (symref.stdout or "").splitlines():
            # "ref: refs/heads/<name>\tHEAD"
            if line.startswith("ref:") and "refs/heads/" in line:
                head_ref = line.split("refs/heads/", 1)[1]
                default_branch = head_ref.split("\t", 1)[0].strip()
                break
    if not default_branch and branches:
        default_branch = "main" if "main" in branches else branches[0]
    return {"branches": branches, "default_branch": default_branch}


def git_init(repo_dir: str | Path, *, default_branch: str = "main") -> Path:
    """Initialize a git repo in an existing (user-owned) directory (F135 D12).
    Guarded/confirmed at the route layer; this is the argv-only egress."""
    repo = Path(repo_dir)
    proc = _git_run(repo, "init", "-b", default_branch)
    if proc.returncode != 0:
        proc2 = _git_run(repo, "init")
        if proc2.returncode != 0:
            raise PublishEgressError("git_failed:init")
        _git_run(repo, "checkout", "-b", default_branch)
    return repo


def git_remote_add(repo_dir: str | Path, name: str, url: str) -> dict:
    """Attach a validated GitHub remote to a repo (F135 D8, for git-init'd local
    projects that need an origin before a P3 PR). Rejects a bad remote name or a
    non-GitHub URL."""
    if not _REMOTE_NAME_RE.match(name or ""):
        raise PublishEgressError("invalid_remote_name")
    url = validate_clone_url(url)
    proc = _git_run(Path(repo_dir), "remote", "add", name, url)
    if proc.returncode != 0:
        raise PublishEgressError("git_failed:remote_add:" + _redact(proc.stderr)[:120])
    return {"remote": name, "url": url}


def git_rev_parse_head(repo_dir: str | Path) -> str:
    """Short HEAD sha (for import provenance), or '' on failure."""
    proc = _git_run(Path(repo_dir), "rev-parse", "--short", "HEAD")
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


# -- F138: refresh an imported project from remote (fetch / FF / staleness) -- #


def git_current_branch(repo_dir: str | Path) -> str:
    """The checked-out branch name, or ``"HEAD"`` when detached, or ``""`` on
    failure. F138 uses this to refuse fast-forwarding the WRONG branch (a repo on
    ``feature-x`` must not be FF'd to ``origin/main``)."""
    proc = _git_run(Path(repo_dir), "rev-parse", "--abbrev-ref", "HEAD")
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def git_is_shallow(repo_dir: str | Path) -> bool:
    """True iff the repo is a shallow clone (``--depth 1``). Fail-closed to False
    (treat as full) on any error — a full-history fetch/FF is always safe."""
    try:
        proc = _git_run(Path(repo_dir), "rev-parse", "--is-shallow-repository")
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0 and (proc.stdout or "").strip() == "true"


def git_fetch(repo_dir: str | Path, *, remote: str = "origin",
              unshallow: bool = False,
              timeout: float = _GIT_CLONE_TIMEOUT_SECONDS) -> None:
    """Fetch ``remote`` (F138). NO ``--prune`` — the preview path calls this and
    must not delete remote-tracking refs. ``unshallow`` deepens a shallow clone so
    ahead/behind + fast-forward are meaningful. Raises :class:`PublishEgressError`
    (redacted) on failure or timeout."""
    if not _REMOTE_NAME_RE.match(remote or ""):
        raise PublishEgressError("invalid_remote_name")
    args = ["fetch"]
    if unshallow:
        args.append("--unshallow")
    args.append(remote)
    try:
        proc = _git_run(Path(repo_dir), *args, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise PublishEgressError("git_failed:fetch_timeout") from exc
    if proc.returncode != 0:
        raise PublishEgressError("git_failed:fetch:" + _redact(proc.stderr)[:160])


def git_refresh_remote_head(repo_dir: str | Path, *, remote: str = "origin",
                            timeout: float = _GIT_WRITE_TIMEOUT_SECONDS) -> None:
    """Refresh ``refs/remotes/<remote>/HEAD`` from the remote's advertised HEAD.

    A normal ``git fetch`` does not update this symbolic ref when a repository's
    default branch changes. F138 must refresh it before deciding which branch may
    be fast-forwarded and before reconciling the publish target.
    """
    if not _REMOTE_NAME_RE.match(remote or ""):
        raise PublishEgressError("invalid_remote_name")
    try:
        proc = _git_run(
            Path(repo_dir), "remote", "set-head", remote, "--auto", timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise PublishEgressError("git_failed:remote_head_timeout") from exc
    if proc.returncode != 0:
        raise PublishEgressError("git_failed:remote_head")


def git_ahead_behind(repo_dir: str | Path, local: str,
                     upstream: str) -> tuple[int, int] | None:
    """``(ahead, behind)`` of ``local`` relative to ``upstream`` via
    ``git rev-list --left-right --count local...upstream``. Returns ``None`` when a
    ref is missing or history is truncated (a shallow clone), so the caller can
    report "unknown" instead of a bogus count."""
    proc = _git_run(Path(repo_dir), "rev-list", "--left-right", "--count",
                    f"{local}...{upstream}")
    if proc.returncode != 0:
        return None
    parts = (proc.stdout or "").split()
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def git_fast_forward(repo_dir: str | Path, ref: str) -> None:
    """Fast-forward the current branch to ``ref`` (F138). ``--ff-only`` refuses to
    create a merge commit, so a diverged branch fails cleanly instead of merging.
    Raises :class:`PublishEgressError("git_failed:not_fast_forward")` when it can't
    FF (caller surfaces ``branch_diverged``)."""
    proc = _git_run(Path(repo_dir), "merge", "--ff-only", ref)
    if proc.returncode != 0:
        raise PublishEgressError("git_failed:not_fast_forward")


__all__ = [
    "PublishEgressError",
    "build_zip_export",
    "build_patch",
    "get_gh_binary",
    "gh_auth_status",
    "has_origin",
    "detect_default_branch",
    "target_repo_status",
    "git_tracked_paths",
    "git_checkout_new_branch",
    "git_commit_all",
    "git_push",
    "git_init_commit",
    "gh_pr_create",
    "gh_repo_create",
    "parse_github_origin",
    "validate_clone_url",
    "git_clone",
    "git_init",
    "git_remote_add",
    "git_rev_parse_head",
    "git_current_branch",
    "git_is_shallow",
    "git_fetch",
    "git_refresh_remote_head",
    "git_ahead_behind",
    "git_fast_forward",
]
