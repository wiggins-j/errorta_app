"""``files`` / ``edit`` / ``diff`` / ``accept`` — the delivered-code surface.

Grounded against ``routes/coding.py`` (verified this session):

* ``files <path>``  → ``GET  /coding/projects/{id}/files?path=`` (coding.py:3401)
    returns ``{content, content_sha256, encoding, on_master, bytes, truncated}``.
* ``diff``          → ``GET  /coding/projects/{id}/worktree``    (worktree diff preview)
* ``edit <path>``   → ``GET  .../files?path=`` then
                      ``PUT  .../files?path=`` (``_UpdateProjectFile{content,
                      expected_sha256}`` coding.py:206/3447). Optimistic concurrency:
                      a mismatch is a **409 stale_file** the engine returns; edits are
                      also **refused while a run is live** (409 run_active — the runner
                      owns the worktree then, coding.py:3470). Both are rendered as
                      clear conflicts, not stack traces.
* ``accept``        → ``POST .../worktree/accept`` (coding.py:3548) body
                      ``{confirm: true, override?, allow_conflicts?}`` — the deliberate
                      human merge-back of the delivered tree into the real files. The
                      engine's merge gate returns **409 merge_gate_blocked** for
                      unreviewed / untested / incomplete / conflicting work unless a
                      SEPARATE ``--override`` is passed.

Reads (``files`` / ``diff``) don't guard. Mutations (``edit`` / ``accept``) go
through ``guard_sole_owner`` + the confirm/``--yes`` gate (invariants #5/#7).

**File content is never an argv value** (invariant #4): ``edit`` reads new content
from ``--content-file PATH`` or, interactively, from ``$EDITOR`` seeded with the
current file — never from a command-line string.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..client import SidecarClient
from ..errors import CliError, LockBusy
from ..registry import Command, Param, register, render_json
from ..render import is_no_project, muted, no_project, render, truncate
from ..session import Context
from . import _base, _mutate

# --------------------------------------------------------------------------- #
# files (read a merged file) + diff (worktree preview).
# --------------------------------------------------------------------------- #

def _files_call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    path = str(args.get("a") or "").strip()
    if not path:
        return _base.usage("files <path>")
    result = client.get_json(
        f"/coding/projects/{ctx.project_id}/files", params={"path": path}) or {}
    return {"_kind": "file", **result}


def _diff_call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    result = client.get_json(f"/coding/projects/{ctx.project_id}/worktree") or {}
    return {"_kind": "diff", **result}


# --------------------------------------------------------------------------- #
# edit (read-modify-write a merged file on master).
# --------------------------------------------------------------------------- #

def _editor_edit(current: str, *, suffix: str = "") -> str | None:
    """Open ``$EDITOR`` seeded with ``current``; return the edited text.

    A seam (monkeypatched in tests). Returns ``None`` if no ``$EDITOR`` is set.
    """
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if not editor:
        return None
    with tempfile.NamedTemporaryFile(
        "w+", suffix=suffix or ".txt", delete=False, encoding="utf-8") as tmp:
        tmp.write(current)
        tmp_path = tmp.name
    try:
        subprocess.run([*editor.split(), tmp_path], check=True)  # noqa: S603
        return Path(tmp_path).read_text(encoding="utf-8")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:  # pragma: no cover
            pass


def _resolve_new_content(args: dict[str, Any], current: str, path: str) -> str | None:
    """New file content from ``--content-file`` or ``$EDITOR`` — never from argv.

    Returns ``None`` when no content source is available (caller renders a hint).
    """
    content_file = args.get("content-file")
    if content_file:
        try:
            return Path(str(content_file)).read_text(encoding="utf-8")
        except OSError as exc:
            raise CliError(f"could not read --content-file: {exc}",
                           code="content_file_unreadable") from exc
        except UnicodeDecodeError as exc:
            raise CliError("--content-file must be UTF-8 text",
                           code="content_file_not_text") from exc
    if _mutate.is_interactive():
        return _editor_edit(current, suffix=Path(path).suffix)
    return None


def _edit_call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    path = str(args.get("a") or "").strip()
    if not path:
        return _base.usage("edit <path> [--content-file PATH]  (else $EDITOR)")
    pid = ctx.project_id
    # Read current — need the blob + its content_sha256 for the optimistic write.
    current = client.get_json(
        f"/coding/projects/{pid}/files", params={"path": path}) or {}
    sha = current.get("content_sha256")
    body_text = current.get("content")
    if not sha or body_text is None or current.get("encoding") != "utf-8":
        return {"_kind": "not_editable", "path": path,
                "reason": current.get("encoding") or "binary_or_missing"}
    new_content = _resolve_new_content(args, body_text, path)
    if new_content is None:
        return {"_kind": "needs_content", "path": path, "current": current}
    if new_content == body_text:
        return {"_kind": "unchanged", "path": path}
    _mutate.guard_sole_owner(ctx)
    if not _mutate.confirm(ctx, args, f"save an edit to '{path}'",
                           note="writes the file on the delivered master tree",
                           interactive_prompt=False):
        return {"_kind": "aborted"}
    try:
        result = client.put_json(
            f"/coding/projects/{pid}/files", params={"path": path},
            json={"content": new_content, "expected_sha256": sha}) or {}
    except LockBusy as exc:
        raise _edit_conflict(exc) from exc
    return {"_kind": "saved", **result}


def _edit_conflict(exc: LockBusy) -> CliError:
    """Re-message the two 409s the PUT can return so they read as conflicts."""
    text = f"{exc.code or ''} {exc.message or ''}".lower()
    if "stale" in text:
        return LockBusy(
            "the file changed since you read it (stale edit) — re-run `edit "
            "<path>` to pull the latest, then re-apply your change", code=exc.code)
    if "run" in text:
        return LockBusy(
            f"{exc.message} — the runner owns the worktree while a run is live; "
            "cancel the run (errorta cancel) or wait, then retry", code=exc.code)
    return exc


# --------------------------------------------------------------------------- #
# accept (merge-back the delivered tree).
# --------------------------------------------------------------------------- #

def _accept_call(client: SidecarClient, ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if not _base.has_project(ctx):
        return _base.no_project()
    _mutate.guard_sole_owner(ctx)
    # Destructive (writes the delivered tree into the user's real files): prompt
    # interactively (default) — a bare `accept` must not merge without a y/N.
    if not _mutate.confirm(ctx, args, "accept (merge-back) the delivered code",
                           note="merges the delivered tree into your real files"):
        return {"_kind": "aborted"}
    body: dict[str, Any] = {"confirm": True}
    if args.get("override"):
        body["override"] = True
    if args.get("allow-conflicts"):
        body["allow_conflicts"] = True
    try:
        result = client.post_json(
            f"/coding/projects/{ctx.project_id}/worktree/accept", json=body) or {}
    except LockBusy as exc:
        raise _accept_conflict(exc) from exc
    return {"_kind": "accepted", **result}


def _accept_conflict(exc: LockBusy) -> CliError:
    if exc.code == "merge_gate_blocked":
        return LockBusy(
            "the merge gate blocked this accept — the delivered work is "
            "unreviewed / untested / incomplete or conflicting. Inspect it "
            "(errorta diff / prs), fix the blockers, or pass --override to merge "
            "anyway.", code=exc.code)
    if exc.code == "delivery_destination_not_empty":
        return LockBusy(exc.message or
                        "the planned delivery directory already exists and is not "
                        "empty; delivery will not overwrite it", code=exc.code)
    return exc


# --------------------------------------------------------------------------- #
# Renderers.
# --------------------------------------------------------------------------- #

def _files_render(payload: Any, verbosity: Any, json_mode: bool) -> str:
    if json_mode:
        return render_json(payload)
    if is_no_project(payload):
        return no_project()
    usage = payload.get("_usage") if isinstance(payload, dict) else None
    if usage:
        return render(muted(f"usage: {usage}"))
    p = payload or {}
    content = p.get("content")
    if content is None:
        return render(muted(f"{p.get('path', '?')}: binary or not on master "
                            f"({p.get('encoding', '?')})"))
    header = render(muted(
        f"{p.get('path', '?')}  ({p.get('encoding', '?')}, {p.get('bytes', 0)} bytes"
        + (", truncated" if p.get('truncated') else "") + ")"))
    return f"{header}\n{content}"


def _diff_render(payload: Any, verbosity: Any, json_mode: bool) -> str:
    if json_mode:
        return render_json(payload)
    if is_no_project(payload):
        return no_project()
    p = payload or {}
    diff = p.get("diff")
    if not diff:
        return render(muted("(no worktree changes)"))
    return render(str(diff)) if not isinstance(diff, str) else diff


def _edit_render(payload: Any, verbosity: Any, json_mode: bool) -> str:
    if json_mode:
        return render_json(payload)
    if is_no_project(payload):
        return no_project()
    usage = payload.get("_usage") if isinstance(payload, dict) else None
    if usage:
        return render(muted(f"usage: {usage}"))
    kind = (payload or {}).get("_kind")
    path = (payload or {}).get("path", "?")
    if kind == "aborted":
        return render(muted("aborted — file unchanged."))
    if kind == "not_editable":
        return render(muted(f"{path}: not an editable text file on master "
                            f"({(payload or {}).get('reason')})"))
    if kind == "needs_content":
        return render(muted(
            f"no content source for {path} — pass --content-file PATH, or run "
            "interactively with $EDITOR set."))
    if kind == "unchanged":
        return render(muted(f"{path}: no change."))
    if kind == "saved":
        p = payload or {}
        return render(f"saved {path}",
                      muted(f"{p.get('bytes', 0)} bytes  head={p.get('head', '')[:12]}"))
    return render(muted("edit: nothing to show"))


def _accept_render(payload: Any, verbosity: Any, json_mode: bool) -> str:
    if json_mode:
        return render_json(payload)
    if is_no_project(payload):
        return no_project()
    if (payload or {}).get("_kind") == "aborted":
        return render(muted("aborted — nothing merged."))
    p = payload or {}
    lines = [render("merge-back accepted.")]
    delivered_to = p.get("delivered_to")
    if delivered_to:
        lines.append(render(muted(f"delivered to: {delivered_to}")))
    run_hint = p.get("run_hint")
    if run_hint:
        lines.append(render(muted(f"run it with: {run_hint}")))
    conflicts = p.get("conflicts") or p.get("conflict_paths")
    if conflicts:
        lines.append(render(muted(f"conflicts: {truncate(conflicts, 80)}")))
    if p.get("delivery_error"):
        lines.append(render(muted(f"delivery warning: {p.get('delivery_error')}")))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Registration.
# --------------------------------------------------------------------------- #

_YES = Param("yes", "Skip the confirmation prompt (required non-interactively).",
             is_flag=True)

register(Command(
    name="files",
    help="Show a delivered file on master (content + sha).",
    call=_files_call,
    render=_files_render,
    params=(Param("a", "File path (repo-relative).", default=None),),
))

register(Command(
    name="diff",
    help="Worktree diff preview of the delivered code.",
    call=_diff_call,
    render=_diff_render,
    params=(Param("watch", "re-render on the poll loop", is_flag=True),),
))

register(Command(
    name="edit",
    help="Edit a delivered file (--content-file or $EDITOR; never via argv).",
    call=_edit_call,
    render=_edit_render,
    params=(
        Param("a", "File path (repo-relative).", default=None),
        Param("content-file", "Read the new file content from this local path.",
              is_flag=False),
        _YES,
    ),
    mutating=True,
))

register(Command(
    name="accept",
    help="Merge-back the delivered tree into your real files (deliberate accept).",
    call=_accept_call,
    render=_accept_render,
    params=(
        Param("override", "Merge despite a blocked merge gate (deliberate).",
              is_flag=True),
        Param("allow-conflicts", "Permit conflicting files in the merge-back.",
              is_flag=True),
        _YES,
    ),
    mutating=True,
))
