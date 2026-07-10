"""Live validation of F087-10 — the tester verdict comes from a REAL test run.

Two layers, mirroring the repo's live-harness convention:

* HARD checks (gate the exit code) — model-independent. They drive the tester
  branch against a REAL isolated worktree using the DEFAULT OS sandbox (seatbelt
  on macOS / bwrap on Linux — the CI tests pin "none", so this is the extra
  coverage): a green command completes the task, a red command blocks it, an
  unknown command_id blocks (invalid_test_command), and the legacy
  ``{"passed": true}`` self-report no longer validates. Every verdict is checked
  against the grounded ``test-runs.jsonl`` record (real exit codes), never model
  text.

* SOFT check (informational) — drives the FULL CodingRunner loop against the
  real example-host Ollama models and prints what the tester actually chose + the
  real test output. Skips cleanly when Ollama is unreachable.

Run: ERRORTA_OLLAMA_HOST=http://127.0.0.1:11435 python scripts/validate_f087_live.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import build_run_turn, members_by_coding_role
from errorta_council.coding.topology import TESTER, Assign
from errorta_council.coding.workspace import CodingWorkspace

GOOD = {"argv": [sys.executable, "-c",
                 "import sys; sys.path.insert(0, '.'); from calc import add; "
                 "assert add(2, 3) == 5; print('ok')"],
        "cwd": ".", "timeout_seconds": 30, "label": "unit (green)"}
RED = {"argv": [sys.executable, "-c",
                "import sys; sys.path.insert(0, '.'); from calc import add; "
                "assert add(2, 3) == 99"],
       "cwd": ".", "timeout_seconds": 30, "label": "unit (red)"}

TEAM = [
    {"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"}},
    {"id": "m-test", "enabled": True, "metadata": {"coding_role": "tester"}},
]


def _envelope(task_id: str, command_ids: list[str]) -> str:
    return json.dumps({
        "schema_version": "coding_turn.v1", "role": "tester", "task_id": task_id,
        "intent": {"kind": "test_plan", "command_ids": command_ids,
                   "scope": "full_project", "rationale": "validate"}})


def _tester(store, workspace, returns: str):
    vt = store.add_task(title="validate: add()", role=TESTER)

    def caller(member, prompt):
        return returns.replace("__TID__", vt.task_id)

    rt = build_run_turn(store, workspace, members_by_coding_role(TEAM), caller,
                        guardrail_enabled=True)
    return vt, rt(Assign(member_id="m-test", task_id=vt.task_id, role=TESTER), store)


def hard_checks() -> list[tuple[str, bool]]:
    store = LedgerStore("f087-10-live")
    store.create_project(north_star="add(a,b) that returns a+b",
                         definition_of_done="add works", target="new", repo_path=None)
    store.set_test_commands({"unit": GOOD, "broken": RED})
    ws = CodingWorkspace("f087-10-live", store)
    ws.setup(target="new", repo_path=None)
    ws.write_file("calc.py", "def add(a, b):\n    return a + b\n", task_id="seed")

    results: list[tuple[str, bool]] = []

    # 1. real GREEN run completes the task and grounds a passing record.
    _vt, out = _tester(store, ws, _envelope("__TID__", ["unit"]))
    runs = store.list_test_runs()
    green = (out.kind == "task_done" and runs and runs[-1]["passed"] is True
             and runs[-1]["results"][0]["exit_code"] == 0)
    green_exit = runs[-1]["results"][0]["exit_code"] if runs else None
    print(f"\n[green] outcome={out.kind} exit={green_exit}")
    results.append(("real green run completes the task (grounded exit 0)", bool(green)))

    # 2. real RED run blocks the task and grounds a failing record.
    _vt, out = _tester(store, ws, _envelope("__TID__", ["broken"]))
    runs = store.list_test_runs()
    red = (out.kind == "task_blocked" and runs[-1]["passed"] is False
           and runs[-1]["results"][0]["exit_code"] not in (0, None))
    print(f"[red] outcome={out.kind} exit={runs[-1]['results'][0]['exit_code']}")
    results.append(("real red run blocks the task (grounded non-zero)", bool(red)))

    # 3. unknown command_id blocks (invalid_test_command), never runs.
    before = len(store.list_test_runs())
    _vt, out = _tester(store, ws, _envelope("__TID__", ["ghost"]))
    ran_nothing = len(store.list_test_runs()) == before
    unknown = (out.kind == "task_blocked"
               and "invalid_test_command" in out.reason and ran_nothing)
    print(f"[unknown] outcome={out.kind} reason={out.reason!r} ran_nothing={ran_nothing}")
    results.append(("unknown command_id blocks without running", bool(unknown)))

    # 4. the dead path: legacy self-report no longer validates.
    _vt, out = _tester(store, ws, json.dumps({"passed": True, "output": "1 passed"}))
    legacy = out.kind == "task_blocked"
    print(f"[legacy self-report] outcome={out.kind} reason={out.reason!r}")
    results.append(("legacy {passed:true} self-report no longer validates", bool(legacy)))

    return results


def soft_real_model_loop() -> None:
    print("\n=== SOFT: real-model full loop (informational) ===")
    try:
        import validate_council_live as V  # noqa: F401  (example-host model ids)

        from errorta_council.coding.autonomy import CADENCE_OFF, CodingAutonomyPolicy
        from errorta_council.coding.runner import CodingRunner, gateway_member_caller
        from errorta_council.gateway_local import LocalGateway
    except Exception as exc:  # pragma: no cover - environment dependent
        print(f"  skipped (imports unavailable): {exc}")
        return

    store = LedgerStore("f087-10-live-real")
    store.create_project(
        north_star="Create calc.py with add(a, b) returning a+b.",
        definition_of_done="add works and the unit test passes",
        target="new", repo_path=None)
    store.set_test_commands({"unit": GOOD})

    def m(mid, role, model):
        return {"id": mid, "enabled": True, "metadata": {"coding_role": role},
                "role": "answerer", "provider_kind": "local", "model": model,
                "gateway_route_id": f"local.ollama.{model}"}

    members = [m("m-pm", "pm", V.GEMMA), m("m-dev", "dev", V.GEMMA),
               m("m-rev", "reviewer", V.MISTRAL), m("m-test", "tester", V.GEMMA)]
    try:
        runner = CodingRunner("f087-10-live-real", members,
                              gateway_member_caller(LocalGateway()),
                              guardrail_enabled=True)
        res = runner.run(CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF,
                                              max_iterations=24))
        print(f"  loop stop_reason={res.stop_reason!r}")
        for r in store.list_test_runs():
            print(f"  tester ran command_ids={r['command_ids']} passed={r['passed']} "
                  f"exits={[x['exit_code'] for x in r['results']]}")
        if not store.list_test_runs():
            print("  (the real models did not produce a valid test_plan this run — "
                  "non-deterministic; the HARD checks already prove the grounding)")
    except Exception as exc:  # pragma: no cover - Ollama may be down
        print(f"  skipped (Ollama unreachable / model error): {exc}")


def main() -> None:
    # errorta_home() is read lazily at LedgerStore call time, so setting it here
    # (before any store use) is sufficient — no import-time side effect needed.
    os.environ.setdefault("ERRORTA_HOME", tempfile.mkdtemp(prefix="f087-10-live-"))
    print(f"ERRORTA_HOME={os.environ['ERRORTA_HOME']}")
    results = hard_checks()
    soft_real_model_loop()

    print("\n========== F087-10 LIVE VALIDATION ==========")
    allok = True
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        allok = allok and ok
    print("=" * 44)
    print("F087-10 LIVE: HARD CHECKS OK" if allok else "F087-10 LIVE: HARD CHECKS FAILED")
    sys.exit(0 if allok else 1)


if __name__ == "__main__":
    main()
