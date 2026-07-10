"""Live demo: run the FULL Coding Team loop for "build a calculator" against a
real local Ollama model, streaming the turn-by-turn logs, then print the run
transcript + final state so we can see how the members work together.

Run: ERRORTA_OLLAMA_HOST=http://127.0.0.1:11434 \
     python scripts/demo_calculator_live.py
"""
from __future__ import annotations

import logging
import os
import sys

from errorta_council.coding.autonomy import CodingAutonomyPolicy, CADENCE_OFF
from errorta_council.coding.evidence import merge_review
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import CodingRunner, gateway_member_caller

MODEL = os.environ.get("DEMO_MODEL", "qwen2.5:3b")
ROUTE = f"local.ollama.{MODEL}"


def _local_member(mid: str, role: str) -> dict:
    return {"id": mid, "enabled": True, "metadata": {"coding_role": role},
            "gateway_route_id": ROUTE, "provider_kind": "local", "model": MODEL}


def _cli_member(mid: str, role: str, provider: str, model: str) -> dict:
    # F040 subscription CLI member (claude_cli / codex_cli). Route by prefix.
    return {"id": mid, "enabled": True, "metadata": {"coding_role": role},
            "gateway_route_id": f"{provider}.{model}", "provider_kind": provider,
            "model": model}


def _team() -> list[dict]:
    if os.environ.get("DEMO_PROVIDER", "local").lower() == "cli":
        # Register the F040 CLI handlers (registration happens on import).
        import errorta_model_gateway.providers.async_claude_cli  # noqa: F401
        import errorta_model_gateway.providers.async_codex_cli   # noqa: F401
        claude = os.environ.get("DEMO_CLAUDE_MODEL", "haiku")
        # A heterogeneous cloud team: Claude (cloud CLI) + ChatGPT (codex CLI).
        return [
            _cli_member("pm-claude", "pm", "claude_cli", claude),
            _cli_member("dev-codex", "dev", "codex_cli", "default"),
            _cli_member("rev-claude", "reviewer", "claude_cli", claude),
            _cli_member("test-codex", "tester", "codex_cli", "default"),
        ]
    return [_local_member("pm-1", "pm"), _local_member("dev-1", "dev"),
            _local_member("rev-1", "reviewer"), _local_member("test-1", "tester")]


TEAM = _team()

# A real test command the tester can choose (runs in the isolated worktree).
CALC_TEST = {
    "argv": [sys.executable, "-c",
             "import sys; sys.path.insert(0, '.'); from calculator import add, "
             "subtract, multiply, divide; "
             "assert add(2, 3) == 5; assert subtract(5, 2) == 3; "
             "assert multiply(3, 4) == 12; assert divide(10, 2) == 5; "
             "print('all calculator tests passed')"],
    "cwd": ".", "timeout_seconds": 30, "label": "calculator unit tests",
}


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s")
    logging.getLogger("errorta.coding").setLevel(logging.INFO)

    pid = "demo-calculator"
    store = LedgerStore(pid)
    store.create_project(
        north_star="Build a Python calculator module `calculator.py` exposing "
                   "add, subtract, multiply, divide — with a passing test.",
        definition_of_done="calculator.py exists and the unit test passes.",
        target="new", repo_path=None)
    store.set_test_commands({"unit": CALC_TEST})

    print("\n" + "#" * 78)
    print(f"# RUNNING CODING TEAM — model={MODEL} — North Star: build a calculator")
    print("#" * 78 + "\n", flush=True)

    runner = CodingRunner(pid, TEAM, gateway_member_caller(_gateway()),
                          guardrail_enabled=True)
    result = runner.run(CodingAutonomyPolicy(
        checkpoint_cadence=CADENCE_OFF, max_iterations=int(os.environ.get("DEMO_MAX", "16"))))

    print("\n" + "#" * 78)
    print(f"# RUN ENDED — stop_reason={result.stop_reason} "
          f"iterations={result.counters.iterations}")
    print("#" * 78 + "\n")

    # --- how did they work together? ---
    print("== TASK BACKLOG (who did what) ==")
    for t in store.list_tasks():
        print(f"  [{t.state:>7}] {t.role:>8}: {t.title}")

    print("\n== DECISIONS (verdicts / gates) ==")
    for d in store.list_decisions():
        print(f"  {d['choice']:>22}: {d['title']} — {d['rationale'][:90]}")

    print("\n== GROUNDED TEST RUNS ==")
    for r in store.list_test_runs():
        exits = "; ".join(f"{x['command_id']}={x['status']}/{x['exit_code']}"
                          for x in r["results"])
        print(f"  passed={r['passed']} sandbox={r['sandbox']} head={r['head'][:8]} :: {exits}")

    print("\n== FILES THE TEAM WROTE ==")
    for a in store.list_artifacts():
        print(f"  {a['status']:>8}  {a['path']}")

    print("\n== TURN TRANSCRIPT (verbatim, truncated) ==")
    for i, t in enumerate(store.list_turns(), 1):
        resp = (t["response"] or "").strip().replace("\n", " ")
        print(f"  turn {i}: {t['role']:>8} -> {t['outcome']:<14} "
              f"({t['duration_ms']}ms)  resp[:120]={resp[:120]!r}")

    gate = merge_review(store, runner.workspace)["_gate"]
    print(f"\n== MERGE GATE == allowed={gate.allowed} "
          f"blockers={[b.code for b in gate.blockers]}")
    return 0


def _gateway():
    from errorta_council.gateway_local import LocalGateway
    return LocalGateway()


if __name__ == "__main__":
    raise SystemExit(main())
