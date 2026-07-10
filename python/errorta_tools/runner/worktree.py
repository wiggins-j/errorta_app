"""Git checkpoint/worktree planning skeleton for code-write runners."""
from __future__ import annotations

from dataclasses import dataclass

from .paths import resolve_workspace_root
from .types import ToolRunnerRequest


@dataclass(frozen=True)
class GitCheckpointPlan:
    workspace_root: str
    checkpoint_ref: str
    request_id: str
    run_id: str
    tool_call_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "workspace_root": self.workspace_root,
            "checkpoint_ref": self.checkpoint_ref,
            "request_id": self.request_id,
            "run_id": self.run_id,
            "tool_call_id": self.tool_call_id,
        }


def build_git_checkpoint_plan(request: ToolRunnerRequest) -> GitCheckpointPlan:
    root = resolve_workspace_root(request.workspace_root)
    return GitCheckpointPlan(
        workspace_root=str(root),
        checkpoint_ref=f"refs/errorta/runner/{_safe_ref(request.run_id)}/{_safe_ref(request.request_id)}",
        request_id=request.request_id,
        run_id=request.run_id,
        tool_call_id=request.tool_call_id,
    )


def _safe_ref(value: str) -> str:
    safe = "".join(ch for ch in value if ch.isalnum() or ch in {"-", "_", "."})
    if not safe:
        raise ValueError("empty_checkpoint_ref_segment")
    return safe


__all__ = ["GitCheckpointPlan", "build_git_checkpoint_plan"]
