"""F039 slice 6 — code_exec handler, wired to the reviewed F043 LocalToolRunner.

The runner provides: env allowlist (no ambient secret inheritance), workspace
isolation, wall-clock timeout, and output cap. This handler translates a
ToolGateway call into a ToolRunnerRequest and bridges the result back. The
council-level F041 gate (tool_policy + first-use consent + ALLOW) runs in the
scheduler before invoke(); the runner additionally enforces workspace /
location / caps (it is NOT a second approval prompt — the council already
approved).

argv MUST be a list of strings — there is no shell string form, so there is no
shell-injection surface.

SANDBOX: ``execution.sandbox`` selects the hardened tier (``runner/sandbox.py``):
``none`` (default, legacy constrained subprocess), ``seatbelt`` (macOS
sandbox-exec — deny network, confine writes to workspace/home/tmp), or
``docker`` (--network none, workspace-only bind mount). A requested-but-
unavailable backend fails CLOSED in the runner (status=blocked).

NETWORK: off by default. The unsandboxed (``none``) tier CANNOT firewall the
network, so ``code_exec.network: true`` there fails closed
(``code_exec_network_requires_sandbox``) — and even with network unset the
child is not network-isolated, so treat exec output as untrusted. Under a
network-isolating sandbox the grant is honored (the sandbox enforces it);
otherwise the sandbox denies all egress.
"""
from __future__ import annotations

from typing import Any

from ..gateway import FatalToolError, ToolCallRequest, ToolCallResult
from ..runner.artifacts import RunnerArtifactStore
from ..runner.bridge import runner_result_to_tool_call_result
from ..runner.local import LocalToolRunner
from ..runner.types import ToolRunnerRequest
from .code import _workspace_root  # shared "granted workspace" resolution

_DEFAULT_TIMEOUT = 120
_DEFAULT_MAX_OUTPUT = 2_000_000


def _sub_policy(request: ToolCallRequest, family: str) -> dict[str, Any]:
    tp = request.metadata.get("tool_policy")
    if isinstance(tp, dict) and isinstance(tp.get(family), dict):
        return tp[family]
    return {}


class CodeExecHandler:
    tool_id = "code_exec"

    async def invoke(self, request: ToolCallRequest) -> ToolCallResult:
        import hashlib
        import os

        from errorta_council.paths import council_root

        from ..runner.sandbox import SANDBOX_NONE, normalize_backend
        from ..runner.sandbox import SandboxUnavailable as _SandboxUnavailable

        exec_policy = _sub_policy(request, "code_exec")
        execution = _sub_policy(request, "execution")
        location = str(execution.get("location") or "local")
        try:
            sandbox = normalize_backend(execution.get("sandbox"))
        except _SandboxUnavailable as exc:
            raise FatalToolError(f"code_exec_{exc.reason_code}") from None

        argv = request.arguments.get("argv")
        if not isinstance(argv, list) or not argv or not all(
            isinstance(a, str) for a in argv
        ):
            raise FatalToolError("code_exec_argv_must_be_list_of_strings")

        ws = _workspace_root(request)
        # If this run has an isolated auto-apply workspace, run against the
        # APPLIED changes (not the user's tree) so tests see the patch.
        from ..runner.apply_workspace import ApplyWorkspace

        apply_ws = ApplyWorkspace(run_id=request.run_id)
        if apply_ws.exists():
            ws = str(apply_ws.root)
        timeout = float(exec_policy.get("timeout_seconds") or _DEFAULT_TIMEOUT)
        # Network is OFF by default. The unsandboxed constrained-subprocess tier
        # CAN'T firewall the network, so a network grant there fails closed. A
        # network-isolating sandbox (seatbelt/docker) CAN enforce it, so under a
        # sandbox the grant is honored (the sandbox is configured to permit it;
        # otherwise the sandbox denies all egress).
        network = bool(exec_policy.get("network", False))
        if network and sandbox == SANDBOX_NONE:
            raise FatalToolError("code_exec_network_requires_sandbox")
        rel_cwd = str(request.arguments.get("cwd") or ".")

        root = council_root()
        runner_root = root / "runner-runtime" / hashlib.sha256(
            request.run_id.encode("utf-8")
        ).hexdigest()[:24]
        runner_home = runner_root / "home"
        runner_tmp = runner_root / "tmp"
        runner_home.mkdir(parents=True, exist_ok=True)
        runner_tmp.mkdir(parents=True, exist_ok=True)

        try:
            runner_request = ToolRunnerRequest(
                request_id=request.call_id,
                run_id=request.run_id,
                tool_call_id=request.call_id,
                argv=tuple(argv),
                workspace_root=ws,
                relative_cwd=rel_cwd,
                execution_location=location,
                timeout_seconds=timeout,
                max_output_bytes=_DEFAULT_MAX_OUTPUT,
                network_allowed=network and sandbox != SANDBOX_NONE,
                sandbox=sandbox,
                # The child writes to its synthetic HOME/TMP; the sandbox must
                # permit those in addition to the workspace (auto-granted).
                sandbox_writable_paths=(str(runner_home), str(runner_tmp)),
                sandbox_image=(
                    str(execution.get("sandbox_image"))
                    if execution.get("sandbox_image")
                    else None
                ),
            )
        except ValueError as exc:
            raise FatalToolError(f"code_exec_{exc}") from None

        source_env = dict(os.environ)
        # The constrained-subprocess tier is not a filesystem sandbox. Do not
        # hand it the operator's real HOME/TMP paths, which are common places
        # for credentials and provider CLI config to live.
        source_env["HOME"] = str(runner_home)
        for name in ("TMPDIR", "TMP", "TEMP"):
            source_env[name] = str(runner_tmp)

        store = RunnerArtifactStore(root=root / "runner-artifacts")
        runner = LocalToolRunner(
            artifact_store=store,
            source_env=source_env,
            # The council F041 gate already approved this call upstream; the
            # runner is not a second approval prompt. action=allow lets it run
            # while still enforcing workspace/location/env-allowlist/caps.
            policy={"action": "allow"},
        )
        result = await runner.run(runner_request)
        return runner_result_to_tool_call_result(request=request, result=result)


__all__ = ["CodeExecHandler"]
