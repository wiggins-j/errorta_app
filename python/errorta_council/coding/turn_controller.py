"""F087-08 - tool-backed Coding Mode turn controller.

Models propose intents; this controller executes the allowed workspace tools and
records the resulting facts in the Coding ledger. This slice executes dev
``code_write`` calls plus the legacy ``files`` shape while keeping task
completion dependent on tool outcomes.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from typing import Any

from .ledger import LedgerStore, Task
from .schemas import TurnErrorCode
from .topology import DEV, PM, REVIEWER, TESTER
from .workspace import CodingWorkspace

# F087-14 WS-3: advertise ONLY tools that are actually executed. The dev's
# code_write is the single member-driven executed tool. The reviewer and tester
# are verdict roles that receive context directly (the reviewer is shown the real
# cumulative diff in its prompt; the tester verdict is derived from a real,
# grounded test run) rather than dispatching member-named tools — so they expose
# no executable tool surface here (previously they advertised git_diff/code_read/
# code_exec with no executor, which over-promised).
_ROLE_TOOLS: dict[str, tuple[str, ...]] = {
    PM: (),
    DEV: ("code_write",),
    REVIEWER: (),
    TESTER: (),
}


def allowed_tools_for_role(role: str) -> tuple[str, ...]:
    return _ROLE_TOOLS.get(role, ())


class _BinaryDecodeError(ValueError):
    """A ``code_write`` declared binary content that failed to base64-decode."""


def _resolve_write_content(args: dict[str, Any]) -> str | bytes:
    """Resolve a ``code_write`` payload to text or bytes.

    A binary asset (a real PNG/font/etc. — the class the UTF-8-only text channel
    could only mangle into an undecodable placeholder) is emitted as base64 via
    ``content_base64`` (preferred) or ``content`` with ``encoding: "base64"``.
    Anything else is text. Raises :class:`_BinaryDecodeError` on malformed base64
    so the caller records a clean failed tool event (no partial write) rather than
    silently writing corrupt bytes.
    """
    b64 = args.get("content_base64")
    if b64 is None and str(args.get("encoding", "")).lower() == "base64":
        b64 = args.get("content")
    if b64 is not None:
        # Strip whitespace so MIME-wrapped base64 (newlines every 76 chars) decodes,
        # then validate=True so a genuine text placeholder — which still carries
        # non-alphabet punctuation after whitespace removal — is REJECTED, not
        # silently decoded to garbage bytes.
        packed = "".join(str(b64).split())
        try:
            return base64.b64decode(packed, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise _BinaryDecodeError(f"invalid_base64: {exc}") from exc
    return str(args.get("content", ""))


def tool_catalog_text(role: str) -> str:
    tools = ", ".join(allowed_tools_for_role(role)) or "none"
    return f"Available Coding Mode tools for role {role}: {tools}."


@dataclass(frozen=True)
class ToolExecutionSummary:
    declared_count: int = 0
    success_count: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)
    # F139 WS-C: distinct files this turn actually changed on its branch vs
    # master. `success_count` counts write CALLS; `net_changed_files` counts real
    # tree deltas — re-emitting an existing file byte-for-byte is a successful
    # call with zero net change. The runner uses this to score productivity.
    net_changed_files: int = 0

    @property
    def failed(self) -> bool:
        return bool(self.failures)


class CodingTurnController:
    def __init__(self, store: LedgerStore, workspace: CodingWorkspace | None) -> None:
        self.store = store
        self.workspace = workspace

    def execute_dev_turn(
        self,
        *,
        task: Task,
        member: dict[str, Any],
        data: dict[str, Any],
    ) -> ToolExecutionSummary:
        calls = self._dev_write_calls(data)
        turn_id = f"turn-{task.task_id}-{member.get('id', 'm-dev')}"
        failures: list[tuple[str, str]] = []
        successes = 0
        for idx, call in enumerate(calls):
            tool = str(call.get("tool", ""))
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            intent = self._safe_intent(tool=tool, args=args, index=idx)
            if tool not in allowed_tools_for_role(DEV):
                reason = TurnErrorCode.tool_not_allowed.value
                failures.append((tool or "<missing-tool>", reason))
                self.store.record_tool_event(
                    turn_id=turn_id,
                    task_id=task.task_id,
                    member_id=str(member.get("id", "m-dev")),
                    role=DEV,
                    tool=tool or "<missing-tool>",
                    status="failed",
                    intent=intent,
                    error=reason,
                )
                continue
            path = str(args.get("path", ""))
            try:
                content = _resolve_write_content(args)
            except _BinaryDecodeError as exc:
                reason = str(exc)
                failures.append((path, reason))
                self.store.record_tool_event(
                    turn_id=turn_id,
                    task_id=task.task_id,
                    member_id=str(member.get("id", "m-dev")),
                    role=DEV,
                    tool="code_write",
                    status="failed",
                    intent=intent,
                    error=reason,
                )
                continue
            try:
                if self.workspace is None:
                    raise RuntimeError("coding_workspace_unavailable")
                head = self.workspace.write_file(path, content, task_id=task.task_id)
            except Exception as exc:
                reason = str(exc)
                failures.append((path, reason))
                self.store.record_tool_event(
                    turn_id=turn_id,
                    task_id=task.task_id,
                    member_id=str(member.get("id", "m-dev")),
                    role=DEV,
                    tool="code_write",
                    status="failed",
                    intent=intent,
                    error=reason,
                )
                continue
            successes += 1
            self.store.record_tool_event(
                turn_id=turn_id,
                task_id=task.task_id,
                member_id=str(member.get("id", "m-dev")),
                role=DEV,
                tool="code_write",
                status="succeeded",
                intent=intent,
                result={"head": head, "path": path},
            )
        # F139 WS-C: measure the turn's real net contribution from git (files the
        # branch changes vs master), not the number of write calls — so a turn
        # that only re-emitted existing files reports zero net change. This is an
        # informational signal on the summary; the runner's productivity gate is
        # the authoritative `pr_diff` of the branch. Falls back to the
        # successful-write count if the branch/git is unavailable.
        net_changed = successes
        if self.workspace is not None:
            try:
                branch = self.workspace.task_branch(task.task_id)
                net_changed = len(self.workspace.changed_paths(branch))
            except Exception:
                net_changed = successes
        return ToolExecutionSummary(
            declared_count=len(calls),
            success_count=successes,
            failures=failures,
            net_changed_files=net_changed,
        )

    @staticmethod
    def _dev_write_calls(data: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        tool_calls = data.get("tool_calls")
        if isinstance(tool_calls, list):
            for raw in tool_calls:
                if not isinstance(raw, dict):
                    continue
                tool = str(raw.get("tool", ""))
                args = raw.get("args")
                if not isinstance(args, dict):
                    args = {}
                out.append({"tool": tool, "args": args})
        if out:
            return out
        for raw in data.get("files") or []:
            if not isinstance(raw, dict) or raw.get("path") is None:
                continue
            out.append({
                "tool": "code_write",
                "args": {"path": str(raw["path"]), "content": str(raw.get("content", ""))},
            })
        return out

    @staticmethod
    def _safe_intent(*, tool: str, args: dict[str, Any], index: int) -> dict[str, Any]:
        intent: dict[str, Any] = {"tool_call_index": index, "tool": tool}
        if "path" in args:
            intent["path"] = str(args["path"])
        if tool == "code_write":
            try:
                resolved = _resolve_write_content(args)
            except _BinaryDecodeError:
                intent["binary"] = True
                intent["content_bytes"] = 0
            else:
                if isinstance(resolved, (bytes, bytearray)):
                    intent["binary"] = True
                    intent["content_bytes"] = len(resolved)
                else:
                    intent["content_bytes"] = len(resolved.encode("utf-8"))
        elif args:
            intent["args_keys"] = sorted(str(k) for k in args.keys())
        return intent


__all__ = [
    "CodingTurnController",
    "ToolExecutionSummary",
    "allowed_tools_for_role",
    "tool_catalog_text",
]
