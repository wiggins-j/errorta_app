"""Live F127 proof: Opus PM with Haiku workers completes a small project.

This harness is opt-in because it consumes logged-in subscription CLI calls:

    F127_LIVE=1 PYTHONPATH=. python scripts/validate_f127_live.py

Without ``F127_LIVE=1`` or when Claude CLI is unavailable/not authenticated, it
skips cleanly. Once explicitly enabled, any non-complete run is a failing proof.
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid

from errorta_council.coding.autonomy import CADENCE_OFF, CodingAutonomyPolicy
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.member_health import preflight_members
from errorta_council.coding.runner import CodingRunner, gateway_member_caller


def _member(member_id: str, role: str, model: str) -> dict:
    return {
        "id": member_id,
        "enabled": True,
        "metadata": {"coding_role": role},
        "role": "answerer",
        "provider_kind": "claude_cli",
        "model": model,
        "gateway_route_id": f"claude_cli.{model}",
    }


TEAM = [
    _member("pm-opus", "pm", "opus"),
    _member("dev-haiku", "dev", "haiku"),
    _member("review-haiku", "reviewer", "haiku"),
    _member("test-haiku", "tester", "haiku"),
]


def main() -> int:
    if os.environ.get("F127_LIVE") != "1":
        print("SKIP: set F127_LIVE=1 to run subscription-CLI model calls")
        return 0

    import errorta_model_gateway.providers.async_claude_cli  # noqa: F401
    from errorta_council.gateway_local import LocalGateway

    unhealthy = preflight_members(TEAM)
    if unhealthy:
        print(f"SKIP: Claude CLI is not ready: {unhealthy}")
        return 0

    os.environ.setdefault("ERRORTA_HOME", tempfile.mkdtemp(prefix="f127-live-"))
    project_id = f"f127-live-{uuid.uuid4().hex[:8]}"
    store = LedgerStore(project_id)
    store.create_project(
        north_star="Create hello.py with greet(name) returning 'Hello, <name>!'.",
        definition_of_done="hello.py exists and the registered unit test passes.",
        target="new",
        repo_path=None,
    )
    store.set_test_commands({
        "unit": {
            "argv": [
                sys.executable,
                "-c",
                "from hello import greet; assert greet('Ada') == 'Hello, Ada!'",
            ],
            "cwd": ".",
            "timeout_seconds": 30,
            "label": "greet unit test",
        }
    })
    runner = CodingRunner(
        project_id,
        TEAM,
        gateway_member_caller(LocalGateway()),
        guardrail_enabled=True,
    )
    result = runner.run(CodingAutonomyPolicy(
        checkpoint_cadence=CADENCE_OFF,
        max_iterations=40,
        max_parallel_workers=1,
    ))
    print(
        f"stop={result.stop_reason} iterations={result.counters.iterations} "
        f"repairs={result.counters.turns_repaired} "
        f"reassignments={result.counters.task_reassignments} "
        f"pm_assists={result.counters.pm_assists}"
    )
    return 0 if result.stop_reason == "definition_of_done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
