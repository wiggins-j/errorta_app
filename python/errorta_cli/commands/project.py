"""``projects`` / ``new`` / ``open`` / ``switch`` / ``delete`` / ``import`` —
project lifecycle + the directory-binding model (F147 §5.1, §8.1).

Grounded against the real routes in ``routes/coding.py`` (line refs inline,
verified this session):

* ``projects``      → ``GET  /coding/projects``                 (coding.py:476)
* ``new <id>``      → ``POST /coding/projects`` (``_NewProject``, coding.py:187/516)
* ``open|switch <id>`` → ``GET /coding/projects/{id}``          (coding.py:548)
* ``delete <id>``   → ``DELETE /coding/projects/{id}``          (coding.py:556)
* ``import local``  → ``POST /coding/projects/import/local`` (``_LocalImport`` 3802/3817)
* ``import github`` → ``GET  .../import/github/auth-status``    (coding.py:3865)
                    → ``POST .../import/github/branches``       (coding.py:3885)
                    → ``POST .../import/github/clone`` (``_GithubClone`` 3809/3953)
                    → poll ``GET .../import/github/clone/{job_id}`` (coding.py:3986)

**Directory binding (spec §5.1, decision #5).** ``new`` / ``import`` / ``open`` /
``switch`` write a ``.errorta-project`` pointer into the working directory so a
later bare ``errorta`` in that dir resolves the project (``config.resolve_project_id``).
The pointer is a LOCAL scratch file, never part of the engine store.

**Mutations** (``new`` / ``import *`` clone/import / ``delete``) go through
``_mutate.guard_sole_owner`` + the confirm/``--yes`` gate (invariants #5/#7); the
``SidecarClient`` attaches the origin header to every request (#2). Reads
(``projects`` / ``open`` show / ``import github`` auth-status) don't guard.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable

from .. import config
from ..client import SidecarClient
from ..errors import CliError
from ..registry import Command, Param, register, render_json
from ..render import is_no_project, muted, no_project, render
from ..render import project as _rp
from ..session import Context
from . import _base, _mutate

# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #

def _slug(name: str) -> str:
    """Best-effort project-id slug matching ``^[A-Za-z0-9._-]{1,64}$``."""
    cleaned = "".join(c if (c.isalnum() or c in "._-") else "-" for c in str(name))
    cleaned = cleaned.strip("-.")
    return cleaned[:64] or "project"


def _github_slug(url: str) -> str | None:
    """Derive ``owner__repo`` from a GitHub URL WITHOUT importing the engine."""
    s = str(url).strip().rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    if "//" not in s and ":" in s:  # scp-like git@github.com:owner/repo
        s = s.split(":", 1)[1]
    parts = [p for p in s.split("/") if p]
    if len(parts) >= 2:
        return _slug(f"{parts[-2]}__{parts[-1]}")
    return None


def _write_binding(ctx: Context, project_id: str, directory: Path | None = None) -> str | None:
    """Write a ``.errorta-project`` pointer; return its path (or None on failure).

    Best-effort: a pointer write must never crash a command whose HTTP side
    already succeeded (e.g. an import that landed but whose dir became unwritable).
    """
    target = Path(directory) if directory is not None else ctx.bind_cwd()
    try:
        if not target.is_dir():
            return None
        return str(config.write_pointer(target, project_id))
    except OSError:
        return None


def _project_dir(project: dict[str, Any]) -> Path | None:
    """The on-disk directory of an imported/existing project, if it exists."""
    for key in ("repo_path", "planned_delivery_dir", "delivery_root"):
        val = project.get(key)
        if val and Path(str(val)).is_dir():
            return Path(str(val))
    return None


def _resolve_new_root(args: dict[str, Any], base_dir: Path) -> str | None:
    """F149: resolve the greenfield delivery ROOT from the mutually-exclusive
    ``--here`` / positional ``location`` / ``--delivery-root`` inputs.

    ``base_dir`` is the binding cwd (``ctx.bind_cwd()``) used for ``--here`` and
    to absolutize a relative location. Returns an absolute path string (expanded,
    NOT symlink-resolved — the server does its own resolve and hands back the
    canonical dir), or None for the server default (~/Errorta Projects). Raises
    CliError on a conflict."""
    here = bool(args.get("here"))
    location = (str(args.get("location")).strip() if args.get("location") else "")
    droot = (str(args.get("delivery-root")).strip() if args.get("delivery-root") else "")

    if here and (location or droot):
        raise CliError("--here cannot be combined with a location / --delivery-root.")
    if location and droot and location != droot:
        raise CliError(
            f"location '{location}' and --delivery-root '{droot}' disagree — pass one.")
    if here:
        return str(base_dir)
    if not (location or droot):
        return None
    p = Path(location or droot).expanduser()
    if not p.is_absolute():
        p = base_dir / p
    return str(p)


def emit_cd_target(path: str | os.PathLike[str] | None) -> None:
    """F149: hand a directory to the shell-integration hook so it can `cd` there.

    The `errorta shell-init` wrapper exports ``ERRORTA_CD_FILE``; we append the
    absolute path when that env var is set. A no-op otherwise, so a plain shell
    (no hook) sees zero behavior change."""
    if not path:
        return
    cd_file = os.environ.get("ERRORTA_CD_FILE")
    if not cd_file:
        return
    try:
        # Defense-in-depth: the hook hands us a fresh, empty temp file. Refuse to
        # truncate a pre-existing non-empty file, so a misconfigured/hostile
        # ERRORTA_CD_FILE can't turn `errorta new` into a clobber primitive.
        if os.path.exists(cd_file) and os.path.getsize(cd_file) > 0:
            return
        with open(cd_file, "w", encoding="utf-8") as fh:
            fh.write(str(Path(path)) + "\n")
    except OSError:
        pass  # best-effort; never break the command over the cd handshake


# --------------------------------------------------------------------------- #
# `projects` — list.
# --------------------------------------------------------------------------- #

def _projects_call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> Any:
    return client.get_json("/coding/projects")


# --------------------------------------------------------------------------- #
# `new` — create a greenfield project.
# --------------------------------------------------------------------------- #

def _new_call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    project_id = str(args.get("id") or "").strip()
    if not project_id:
        return _base.usage("new <id> [location] [--here] [--north-star ...] [--dod ...]")
    _mutate.guard_sole_owner(ctx)
    if not _mutate.confirm(ctx, args, f"create project '{project_id}'",
                           note="creates a new project on disk",
                           interactive_prompt=False):
        return {"_kind": "aborted"}
    body: dict[str, Any] = {
        "project_id": project_id,
        "target": "new",
        "north_star": str(args.get("north-star") or ""),
        "definition_of_done": str(args.get("dod") or ""),
        "work_request": str(args.get("work-request") or ""),
    }
    root = _resolve_new_root(args, ctx.bind_cwd())  # None => default (~/Errorta Projects)
    if root is not None:
        body["delivery_root"] = root
    result = client.post_json("/coding/projects", json=body)
    project = (result or {}).get("project") or {}
    ctx.switch_project(project_id)

    # F149: the project's working directory = planned_delivery_dir (<root>/<id>).
    # Create it (empty) so the user has a folder to sit in — deliver() reuses an
    # empty dir. Bind the pointer INTO that dir so it self-identifies even when
    # several projects share one delivery root (e.g. the default ~/Errorta
    # Projects); delivery ignores the pointer file. Then hand the dir to the hook.
    cd_dir: Path | None = None
    planned = project.get("planned_delivery_dir")
    if planned:
        cd_dir = Path(str(planned))
        try:
            cd_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            cd_dir = None
    pointer = _write_binding(ctx, project_id, directory=cd_dir)
    emit_cd_target(cd_dir)
    return {"_kind": "created", "project": project, "pointer": pointer,
            "cd_dir": str(cd_dir) if cd_dir else None,
            "hooked": bool(os.environ.get("ERRORTA_CD_FILE"))}


# --------------------------------------------------------------------------- #
# `open` / `switch` — bind the session to a project + render it.
# --------------------------------------------------------------------------- #

def _open_call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    project_id = str(args.get("id") or "").strip()
    if not project_id:
        return _base.usage("open <id>")
    # GET FIRST — a 404 (NotFound, exit 8) surfaces before we bind/write a pointer.
    result = client.get_json(f"/coding/projects/{project_id}")
    ctx.switch_project(project_id)
    pointer = _write_binding(ctx, project_id)
    project = (result or {}).get("project") or result
    return {"_kind": "opened", "project": project, "pointer": pointer}


# --------------------------------------------------------------------------- #
# `delete` — destroy a project (refused while a run is active → 409/LockBusy).
# --------------------------------------------------------------------------- #

def _delete_call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    project_id = str(args.get("id") or "").strip()
    if not project_id:
        return _base.usage("delete <id>")
    _mutate.guard_sole_owner(ctx)
    if not _mutate.confirm(ctx, args, f"delete project '{project_id}'",
                           note="permanently removes the project and its worktrees",
                           interactive_prompt=False):
        return {"_kind": "aborted"}
    # DELETE /coding/projects/{id} (coding.py:556) — 409 "project run is still
    # active" surfaces as LockBusy (exit 3) with the real detail string.
    result = client.delete_json(f"/coding/projects/{project_id}")
    if ctx.project_id == project_id:
        ctx.switch_project(None)
    return {"_kind": "deleted", "project_id": project_id, "result": result}


# --------------------------------------------------------------------------- #
# `import` — local folder / GitHub clone.
# --------------------------------------------------------------------------- #

def _import_local(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    raw_path = args.get("a")
    path = str(raw_path) if raw_path else str(ctx.bind_cwd())
    project_id = str(args.get("id") or "").strip() or _slug(Path(path).name)
    _mutate.guard_sole_owner(ctx)
    if not _mutate.confirm(ctx, args, f"import '{path}' as project '{project_id}'",
                           note="registers this folder as an existing-repo project",
                           interactive_prompt=False):
        return {"_kind": "aborted"}
    body: dict[str, Any] = {"project_id": project_id, "folder_path": path}
    if args.get("git-init"):
        # The user explicitly opted into initializing git in a non-git folder.
        body["git_init"] = True
        body["confirm"] = True
    try:
        result = client.post_json("/coding/projects/import/local", json=body)
    except CliError as exc:
        if exc.code == "not_a_git_repo":
            raise CliError(
                f"'{path}' is not a git repo — pass --git-init to initialize it",
                code=exc.code,
            ) from exc
        raise
    project = (result or {}).get("project") or {}
    ctx.switch_project(project_id)
    pointer = _write_binding(ctx, project_id, _project_dir(project))
    return {"_kind": "imported", "project": project, "pointer": pointer}


def _poll_clone(
    client: SidecarClient,
    job_id: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    interval: float = 1.0,
    max_attempts: int = 600,
) -> dict[str, Any]:
    """Poll the clone-job registry until it reports ``done``/``error`` (coding.py:3986)."""
    for _ in range(max_attempts):
        job = client.get_json(f"/coding/projects/import/github/clone/{job_id}") or {}
        if str(job.get("status")) in ("done", "error"):
            return job
        sleep(interval)
    raise CliError("GitHub clone timed out", code="clone_timeout")


def _import_github(
    client: SidecarClient,
    ctx: Context,
    args: dict[str, Any],
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    url = args.get("a")
    if not url:
        # `import github` (no url) → the project-less auth probe (never a token).
        return {"_kind": "auth", "auth": client.get_json(
            "/coding/projects/import/github/auth-status")}
    url = str(url)
    project_id = str(args.get("id") or "").strip() or _github_slug(url)
    if not project_id:
        raise CliError("could not derive a project id from the URL — pass --id <slug>",
                       code="bad_import_id")
    _mutate.guard_sole_owner(ctx)
    if not _mutate.confirm(ctx, args, f"clone '{url}' into project '{project_id}'",
                           note="clones a GitHub repo and creates a project",
                           interactive_prompt=False):
        return {"_kind": "aborted"}
    # Informational branch probe (never blocks — returns {ok:false,...} on failure).
    branches = client.post_json("/coding/projects/import/github/branches",
                                json={"repo_url": url})
    clone_body: dict[str, Any] = {"project_id": project_id, "repo_url": url}
    if args.get("branch"):
        clone_body["ref"] = str(args["branch"])
    started = client.post_json("/coding/projects/import/github/clone", json=clone_body)
    job_id = str((started or {}).get("job_id") or "")
    if not job_id:
        raise CliError("clone did not start (no job id returned)", code="clone_failed")
    job = _poll_clone(client, job_id, sleep=sleep)
    if str(job.get("status")) == "error":
        raise CliError(f"GitHub clone failed: {job.get('message', 'unknown error')}",
                       code="clone_error")
    cloned_id = str(job.get("project_id") or project_id)
    project = ((client.get_json(f"/coding/projects/{cloned_id}") or {}).get("project")) or {}
    ctx.switch_project(cloned_id)
    pointer = _write_binding(ctx, cloned_id, _project_dir(project))
    return {"_kind": "cloned", "project": project, "branches": branches,
            "pointer": pointer, "job": job}


def _import_call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    sub = str(args.get("sub") or "").lower()
    if sub == "local":
        return _import_local(client, ctx, args)
    if sub == "github":
        return _import_github(client, ctx, args)
    return _base.usage("import local [PATH] [--id ID] [--git-init] | "
                       "import github [<url>] [--branch B] [--id ID]")


# --------------------------------------------------------------------------- #
# Renderers.
# --------------------------------------------------------------------------- #

def _project_render(payload: Any, verbosity: Any, json_mode: bool) -> str:
    if json_mode:
        return render_json(payload)
    return _rp.render_projects(payload, verbosity)


def _lifecycle_render(payload: Any, verbosity: Any, json_mode: bool) -> str:
    if json_mode:
        return render_json(payload)
    if is_no_project(payload):
        return no_project()
    usage = payload.get("_usage") if isinstance(payload, dict) else None
    if usage:
        return render(muted(f"usage: {usage}"))
    kind = (payload or {}).get("_kind")
    if kind == "aborted":
        return render(muted("aborted — nothing changed."))
    if kind in ("created", "opened", "imported", "cloned"):
        verb = {"created": "created", "opened": "opened",
                "imported": "imported", "cloned": "cloned"}[kind]
        body = _rp.render_project({"project": payload.get("project")}, verbosity)
        pointer = payload.get("pointer")
        note = f"\n{render(muted(f'bound this directory ({pointer})'))}" if pointer else ""
        # F149: a `new` with a resolved project dir. If the shell hook moved us
        # there, say so; otherwise print the path + a one-time integration hint.
        cd_dir = payload.get("cd_dir")
        if cd_dir:
            if payload.get("hooked"):
                note += "\n" + render(muted(f"→ {cd_dir}"))
            else:
                tip = ('tip: add  eval "$(errorta shell-init zsh)"  to ~/.zshrc '
                       'to jump into new projects automatically')
                note += "\n" + render(f"cd {cd_dir}") + "\n" + render(muted(tip))
        if kind == "cloned":
            body = _rp.render_branches(payload.get("branches"), verbosity) + "\n" + body
        return f"{render(muted(verb + ':'))}\n{body}{note}"
    if kind == "auth":
        return _rp.render_auth_status(payload.get("auth"), verbosity)
    if kind == "deleted":
        return render(f"deleted project '{payload.get('project_id')}'.")
    return render(muted("nothing to show"))


# --------------------------------------------------------------------------- #
# Registration.
# --------------------------------------------------------------------------- #

_YES = Param("yes", "Skip the confirmation prompt (required non-interactively).",
             is_flag=True)

register(Command(
    name="projects",
    help="List all coding projects (with derived status).",
    call=_projects_call,
    render=_project_render,
))

register(Command(
    name="new",
    help="Create a greenfield project and bind this directory to it.",
    call=_new_call,
    render=_lifecycle_render,
    params=(
        Param("id", "Project id (slug).", required=True),
        Param("location", "Directory to create the project under (its <id> folder "
                          "lands here; created if missing). Default: ~/Errorta Projects.",
              is_flag=False),
        Param("here", "Use the current directory as the delivery root.", is_flag=True),
        Param("delivery-root", "Alias for the positional location (parent directory).",
              is_flag=False),
        Param("north-star", "The project's North Star.", is_flag=False),
        Param("dod", "Definition of Done.", is_flag=False),
        Param("work-request", "Initial Current Focus directive.", is_flag=False),
        _YES,
    ),
    mutating=True,
))

register(Command(
    name="open",
    help="Bind this directory to a project and show it.",
    call=_open_call,
    render=_lifecycle_render,
    params=(Param("id", "Project id.", required=True),),
))

register(Command(
    name="switch",
    help="Switch the session to another project (alias of open).",
    call=_open_call,
    render=_lifecycle_render,
    params=(Param("id", "Project id.", required=True),),
))

register(Command(
    name="delete",
    help="Delete a project (refused while a run is active).",
    call=_delete_call,
    render=_lifecycle_render,
    params=(Param("id", "Project id.", required=True), _YES),
    mutating=True,
))

register(Command(
    name="import",
    help="Import an existing project (local folder or GitHub clone).",
    call=_import_call,
    render=_lifecycle_render,
    params=(
        Param("sub", "local | github", default=""),
        Param("a", "PATH (local) or <url> (github).", default=None),
        Param("id", "Override the derived project id.", is_flag=False),
        Param("branch", "github: branch/ref to clone.", is_flag=False),
        Param("git-init", "local: initialize git in a non-git folder.", is_flag=True),
        _YES,
    ),
    mutating=True,
))
