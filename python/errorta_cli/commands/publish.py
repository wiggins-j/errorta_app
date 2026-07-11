"""``publish`` — export the delivered work + open a PR / create a repo (§8.6).

Grounded against the real ``coding.py`` (file:line inline, verified this session):

* ``publish targets``       → ``GET  .../publish/targets``        (coding.py:4484)
* ``publish events``        → ``GET  .../publish/events``         (coding.py:4492)
* ``publish auth-status``   → ``GET  .../publish/auth-status``    (coding.py:4575)
* ``publish manual-export`` → ``POST .../publish/manual-export`` (``_ManualExport{kind}`` 4461/4500)
* ``publish pr``            → ``POST .../publish/existing-repo-pr`` (``_ExistingRepoPr`` 4595/4611)
* ``publish new-repo``      → ``POST .../publish/new-github-repo``  (``_NewGithubRepo`` 4604/4638)

**Safety (§14).** ``publish pr`` and ``publish new-repo`` create / modify
OUTWARD-FACING content (a PR on the user's repo, a new GitHub repo). They go
through :func:`_mutate.confirm_outward` — an explicit y/N showing exactly what
will happen (repo, branch, visibility, title) and a HARD ``--yes`` requirement
non-interactively / in ``--json`` (never publish silently). ``new-repo`` defaults
to ``private=True`` unless ``--public`` is passed. ``manual-export`` is a local
artifact writer (never touches GitHub), so it takes the sole-owner guard but not
the outward-action gate. ``auth-status`` NEVER prints a token (invariant #4).
"""
from __future__ import annotations

from typing import Any

from ..client import SidecarClient
from ..registry import Command, Param, register, render_json
from ..render import is_no_project, muted, no_project, render
from ..render import publish as _rp
from ..session import Context
from . import _base, _mutate

_MANUAL_EXPORT_KINDS = ("zip", "patch", "git_apply", "open_folder")


def _base_path(ctx: Context) -> str:
    return f"/coding/projects/{ctx.project_id}/publish"


# --------------------------------------------------------------------------- #
# Sub-action handlers.
# --------------------------------------------------------------------------- #

def _targets(client: SidecarClient, ctx: Context) -> dict[str, Any]:
    return {"_kind": "targets", "targets":
            (client.get_json(f"{_base_path(ctx)}/targets") or {}).get("targets") or []}


def _events(client: SidecarClient, ctx: Context) -> dict[str, Any]:
    return {"_kind": "events", "events":
            (client.get_json(f"{_base_path(ctx)}/events") or {}).get("events") or []}


def _auth(client: SidecarClient, ctx: Context) -> dict[str, Any]:
    # Never a token — the route returns {gh_present, login, token_in_keychain}.
    return {"_kind": "auth", "auth": client.get_json(f"{_base_path(ctx)}/auth-status") or {}}


def _manual_export(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    kind = str(args.get("kind") or "").strip()
    if kind not in _MANUAL_EXPORT_KINDS:
        return _base.usage(
            "publish manual-export --kind {zip|patch|git_apply|open_folder}")
    # A LOCAL artifact writer (never touches GitHub) — sole-owner guard, no
    # outward-action gate. Writes into the project's deliverable dir.
    _mutate.guard_sole_owner(ctx)
    result = client.post_json(f"{_base_path(ctx)}/manual-export", json={"kind": kind})
    return {"_kind": "export", "export": result or {}}


def _pr(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    _mutate.guard_sole_owner(ctx)
    branch = args.get("branch")
    title = args.get("title")
    details = [
        "open a pull request on this project's existing GitHub repo",
        f"branch: {branch}" if branch else "branch: errorta/<project-id> (default)",
        f"title: {title}" if title else "title: (default generated)",
    ]
    if args.get("override"):
        details.append("override: bypass the secret-scan block")
    if not _mutate.confirm_outward(ctx, args, "open a pull request", details):
        return {"_kind": "aborted"}
    body: dict[str, Any] = {"override": bool(args.get("override"))}
    if branch:
        body["branch"] = str(branch)
    if title:
        body["title"] = str(title)
    if args.get("body") is not None:
        body["body_override"] = str(args["body"])
    return {"_kind": "pr", "result":
            client.post_json(f"{_base_path(ctx)}/existing-repo-pr", json=body) or {}}


def _new_repo(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    repo_name = str(args.get("name") or args.get("a") or "").strip()
    if not repo_name:
        return _base.usage(
            "publish new-repo <name> [--public] [--local-only] [--override]")
    _mutate.guard_sole_owner(ctx)
    # Private-by-default: only an explicit --public opens it to the world (§14).
    private = not bool(args.get("public"))
    local_only = bool(args.get("local-only"))
    details = [
        (f"create a LOCAL git repo named '{repo_name}' (not pushed to GitHub)"
         if local_only else
         f"create a new {'PRIVATE' if private else 'PUBLIC'} GitHub repo "
         f"named '{repo_name}' and push the delivered tree"),
    ]
    if args.get("override"):
        details.append("override: bypass the secret-scan block")
    if not _mutate.confirm_outward(ctx, args, "create a repository", details):
        return {"_kind": "aborted"}
    body = {"repo_name": repo_name, "private": private,
            "local_only": local_only, "override": bool(args.get("override"))}
    return {"_kind": "new-repo", "result":
            client.post_json(f"{_base_path(ctx)}/new-github-repo", json=body) or {}}


# --------------------------------------------------------------------------- #
# Dispatch + render.
# --------------------------------------------------------------------------- #

def _call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    action = str(args.get("action") or "").strip().lower() or "targets"
    if action in ("targets", "target"):
        return _targets(client, ctx)
    if action == "events":
        return _events(client, ctx)
    if action in ("auth-status", "auth"):
        return _auth(client, ctx)
    if action in ("manual-export", "export"):
        return _manual_export(client, ctx, args)
    if action == "pr":
        return _pr(client, ctx, args)
    if action in ("new-repo", "new"):
        return _new_repo(client, ctx, args)
    return _base.usage(
        "publish [targets|events|auth-status|manual-export --kind K|"
        "pr [--branch B --title T --body ... --override]|"
        "new-repo <name> [--public --local-only --override]]")


def _render(payload: Any, verbosity: Any, json_mode: bool) -> str:
    if json_mode:
        return render_json(payload)
    if is_no_project(payload):
        return no_project()
    usage = payload.get("_usage") if isinstance(payload, dict) else None
    if usage:
        return render(muted(f"usage: {usage}"))
    kind = (payload or {}).get("_kind")
    if kind == "aborted":
        return render(muted("aborted — nothing published."))
    if kind == "targets":
        return _rp.render_targets(payload)
    if kind == "events":
        return _rp.render_events(payload)
    if kind == "auth":
        return _rp.render_auth(payload)
    if kind == "export":
        return _rp.render_export(payload)
    if kind == "pr":
        return _rp.render_pr_result(payload)
    if kind == "new-repo":
        return _rp.render_new_repo_result(payload)
    return render(muted("nothing to show"))


register(Command(
    name="publish",
    help="Export the delivered work or open a PR / create a repo (outward-facing).",
    call=_call,
    render=_render,
    params=(
        Param("action", "targets|events|auth-status|manual-export|pr|new-repo",
              default=""),
        Param("a", "positional: repo name (new-repo).", default=None),
        Param("kind", "manual-export kind: zip|patch|git_apply|open_folder.",
              is_flag=False),
        Param("name", "new-repo: repository name.", is_flag=False),
        Param("public", "new-repo: create a PUBLIC repo (default is private).",
              is_flag=True),
        Param("local-only", "new-repo: create a local git repo only (no GitHub push).",
              is_flag=True),
        Param("branch", "pr: branch name (default errorta/<id>).", is_flag=False),
        Param("title", "pr: PR title (default generated).", is_flag=False),
        Param("body", "pr: PR body override.", is_flag=False),
        Param("override", "bypass the secret-scan block (pr / new-repo).",
              is_flag=True),
        Param("yes", "Authorize the outward-facing action (required non-interactively).",
              is_flag=True),
    ),
    # Not `mutating` for the registry watch-guard: the reads (targets/events/auth)
    # are watchable; the outward mutations gate themselves via confirm_outward.
))
