"""F041 policy engine unit coverage."""
from __future__ import annotations

import ast
from pathlib import Path

from errorta_policy import PolicyAction, PolicyContext, PolicyEngine, PolicyPhase


def test_tool_policy_allows_granted_tool_without_consent() -> None:
    decision = PolicyEngine().evaluate(
        PolicyContext(
            phase=PolicyPhase.TOOL_CALL,
            run_id="run-1",
            member_id="m-1",
            tool_id="web_fetch",
            request_sha256="abc",
            policy={
                "enabled_tool_ids": ["web_fetch"],
                "require_first_use_consent": False,
            },
        )
    )

    assert decision.action == PolicyAction.ALLOW
    assert decision.reason_code == "tool_policy_allow"


def test_configured_allow_is_authorization() -> None:
    # F087-17: a configured "allow" IS the authorization (callers like the tool
    # runner rely on it to run an already-approved tool with granted env). It is
    # NOT reordered behind requires_approval — that broke the runner's granted-env
    # path. The coding test path is guarded at its own layer instead.
    for meta in ({}, {"requires_approval": True}):
        decision = PolicyEngine().evaluate(
            PolicyContext(
                phase=PolicyPhase.MODEL_REQUEST,
                run_id="run-1",
                member_id="m-1",
                policy={"action": "allow"},
                metadata=meta,
            )
        )
        assert decision.action == PolicyAction.ALLOW


def test_tool_policy_denies_ungranted_tool() -> None:
    decision = PolicyEngine().evaluate(
        PolicyContext(
            phase=PolicyPhase.TOOL_CALL,
            run_id="run-1",
            member_id="m-1",
            tool_id="code_exec",
            request_sha256="abc",
            policy={
                "enabled_tool_ids": ["web_fetch"],
                "require_first_use_consent": False,
            },
        )
    )

    assert decision.action == PolicyAction.DENY
    assert decision.reason_code == "tool_not_granted"
    assert decision.pending_request is None


def test_tool_policy_asks_for_first_use_consent_without_raw_arguments() -> None:
    decision = PolicyEngine().evaluate(
        PolicyContext(
            phase=PolicyPhase.TOOL_CALL,
            run_id="run-1",
            member_id="m-1",
            tool_id="web_fetch",
            request_sha256="abc123",
            requester={"type": "council_member"},
            safe_request={
                "call_id": "tc-1",
                "tool_id": "web_fetch",
                "args_sha256": "abc123",
            },
            policy={
                "enabled_tool_ids": ["web_fetch"],
                "require_first_use_consent": True,
            },
        )
    )

    assert decision.action == PolicyAction.ASK
    assert decision.reason_code == "tool_consent_required"
    assert decision.pending_request is not None
    pending = decision.pending_request
    assert pending.safe_request["args_sha256"] == "abc123"
    assert "arguments" not in pending.safe_request
    assert [w.key for w in pending.state_writes_on_approve] == [
        "tool_consent:web_fetch"
    ]


def test_policy_package_imports_no_egress_modules() -> None:
    policy_dir = Path(__file__).parents[2] / "errorta_policy"
    forbidden = {"mcp", "subprocess", "requests", "urllib", "aiohttp", "httpx"}
    violations: list[str] = []
    for path in sorted(policy_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    if root in forbidden:
                        violations.append(
                            f"{path.relative_to(policy_dir)} imports {alias.name}"
                        )
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".", 1)[0]
                if root in forbidden:
                    violations.append(
                        f"{path.relative_to(policy_dir)} imports from {node.module}"
                    )
    assert violations == []
