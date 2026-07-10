"""F039 slices 4-5 — code_read and code_write (propose_only) handlers.

Both are LOCAL-egress tools scoped to the room's granted workspace, with
path-traversal guarded by the F043 runner's ``resolve_workspace_path``.

- code_read returns a file's contents as an (untrusted) source.
- code_write in ``propose_only`` mode returns a unified DIFF only — it never
  writes to the user's working tree. Applying a patch (auto_apply) is the
  build_review loop's job (F039 slice 7) under a git worktree + checkpoint.
"""
from __future__ import annotations

import difflib
import time
from typing import Any

from ..gateway import FatalToolError, ToolCallRequest, ToolCallResult
from ..runner.paths import (
    WorkspacePathError,
    resolve_workspace_path,
    safe_workspace_relative_path,
)

_DEFAULT_MAX_BYTES = 2_000_000


def _sub_policy(request: ToolCallRequest, family: str) -> dict[str, Any]:
    tp = request.metadata.get("tool_policy")
    if isinstance(tp, dict) and isinstance(tp.get(family), dict):
        return tp[family]
    return {}


def _workspace_root(request: ToolCallRequest) -> str:
    """The granted workspace root. code_read carries it; code_write reuses the
    same grant (its policy has no separate path), or an explicit _extras one."""
    write = _sub_policy(request, "code_write")
    if write.get("workspace_path"):
        return str(write["workspace_path"])
    read = _sub_policy(request, "code_read")
    if read.get("workspace_path"):
        return str(read["workspace_path"])
    raise FatalToolError("code_no_workspace_granted")


class CodeReadHandler:
    tool_id = "code_read"

    async def invoke(self, request: ToolCallRequest) -> ToolCallResult:
        ws = _workspace_root(request)
        rel = str(request.arguments.get("path") or "").strip()
        if not rel:
            raise FatalToolError("code_read_missing_path")
        max_bytes = int(
            _sub_policy(request, "code_read").get("max_bytes") or _DEFAULT_MAX_BYTES
        )
        start = time.monotonic()
        try:
            path = resolve_workspace_path(ws, rel, must_exist=True)
        except WorkspacePathError as exc:
            raise FatalToolError(f"code_read_{exc}") from None
        if not path.is_file():
            raise FatalToolError("code_read_not_a_file")
        data = path.read_bytes()
        truncated = len(data) > max_bytes
        text = data[:max_bytes].decode("utf-8", errors="replace")
        if truncated:
            text += "\n[truncated]"
        return ToolCallResult.from_content(
            request=request,
            content=text,
            duration_ms=int((time.monotonic() - start) * 1000),
            egress_class="local",
            provenance={
                "path": safe_workspace_relative_path(ws, path),
                "bytes": len(data),
                "truncated": truncated,
            },
        )


class CodeWriteHandler:
    tool_id = "code_write"

    async def invoke(self, request: ToolCallRequest) -> ToolCallResult:
        policy = _sub_policy(request, "code_write")
        mode = str(policy.get("mode") or "propose_only")
        if mode == "auto_apply":
            return await self._auto_apply(request)
        if mode != "propose_only":
            raise FatalToolError("code_write_mode_unsupported")
        return await self._propose_only(request)

    async def _propose_only(self, request: ToolCallRequest) -> ToolCallResult:
        ws = _workspace_root(request)
        rel = str(request.arguments.get("path") or "").strip()
        if not rel:
            raise FatalToolError("code_write_missing_path")
        new_content = request.arguments.get("content")
        if not isinstance(new_content, str):
            raise FatalToolError("code_write_missing_content")
        start = time.monotonic()
        try:
            path = resolve_workspace_path(ws, rel, must_exist=False)
        except WorkspacePathError as exc:
            raise FatalToolError(f"code_write_{exc}") from None

        old = ""
        if path.exists():
            if not path.is_file():
                raise FatalToolError("code_write_not_a_file")
            old = path.read_text(encoding="utf-8", errors="replace")
        diff = "".join(
            difflib.unified_diff(
                old.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
            )
        )
        # propose_only: NEVER write to the user's tree. The diff is the result;
        # a reviewer/judge sees it and the build_review loop applies it later.
        body = diff if diff else "(no changes)"
        return ToolCallResult.from_content(
            request=request,
            content=body,
            duration_ms=int((time.monotonic() - start) * 1000),
            egress_class="local",
            provenance={
                "path": rel,
                "mode": "propose_only",
                "applied": False,
                "is_new_file": not path.exists(),
            },
        )

    async def _auto_apply(self, request: ToolCallRequest) -> ToolCallResult:
        """Apply into an ISOLATED per-run git workspace (never the user's tree),
        checkpointed for rollback. The result is the cumulative diff; merging
        back to the user's tree requires explicit human accept."""
        import asyncio

        from ..runner.apply_workspace import ApplyWorkspace, ApplyWorkspaceError

        ws = _workspace_root(request)
        start = time.monotonic()
        aw = ApplyWorkspace(run_id=request.run_id)

        # Rollback request: {"rollback": "<sha>"} undoes a failed apply.
        rollback_ref = request.arguments.get("rollback")
        try:
            if isinstance(rollback_ref, str) and rollback_ref:
                await asyncio.to_thread(aw.ensure, ws)
                await asyncio.to_thread(aw.rollback, rollback_ref)
                diff = await asyncio.to_thread(aw.cumulative_diff)
                return ToolCallResult.from_content(
                    request=request, content=diff or "(no changes)",
                    duration_ms=int((time.monotonic() - start) * 1000),
                    egress_class="local",
                    provenance={"mode": "auto_apply", "rolled_back_to": rollback_ref},
                )

            rel = str(request.arguments.get("path") or "").strip()
            if not rel:
                raise FatalToolError("code_write_missing_path")
            new_content = request.arguments.get("content")
            if not isinstance(new_content, str):
                raise FatalToolError("code_write_missing_content")

            await asyncio.to_thread(aw.ensure, ws)
            checkpoint = await asyncio.to_thread(aw.head_ref)  # rollback point
            head = await asyncio.to_thread(aw.write_and_commit, rel, new_content)
            diff = await asyncio.to_thread(aw.cumulative_diff)
        except ApplyWorkspaceError as exc:
            raise FatalToolError(str(exc)) from None
        except WorkspacePathError as exc:
            raise FatalToolError(f"code_write_{exc}") from None

        return ToolCallResult.from_content(
            request=request,
            content=diff or "(no changes)",
            duration_ms=int((time.monotonic() - start) * 1000),
            egress_class="local",
            provenance={
                "path": rel,
                "mode": "auto_apply",
                "applied": True,
                "isolated_workspace": True,
                "checkpoint": checkpoint,
                "head": head,
                "requires_human_accept_final": True,
            },
        )


__all__ = ["CodeReadHandler", "CodeWriteHandler"]
