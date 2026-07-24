"""Spec 12 (S1) — automatic gate acquisition for a greenfield run.

The gravity-golf run had a Definition of Done of "iterate until the acceptance
gate passes", and NO gate: the test-command registry is only ever written by the
app UI / ``errorta test-commands set``, and ``runtime.detect`` is only ever called
from an HTTP route — so an autonomous headless run never has anything to run, and
every gate is vacuously satisfied.

This module closes that wiring gap. Called at run start and after each merge that
advances master, it:

* registers detected runtime profiles when none are stored (so a buildless web
  target gets its ``python -m http.server`` static profile — the exact right way
  to run gravity-golf — without an operator visiting the UI); and
* registers an ACCEPTANCE-scoped test command when the team has authored a
  runnable test on master AND that command is *proven to execute* by a one-shot
  smoke run.

The smoke run is the load-bearing safeguard (review finding D1): "never invent a
command whose entrypoint is absent" is not enough — ``node test/acceptance.test.js``
also fails ``Cannot find module 'jsdom'`` on every tree because no ``npm install``
ever runs, and a gate that can never pass is a wedge, not a gate. So a candidate
is registered only if it actually ran; a missing interpreter / missing module /
immediate crash is refused and recorded.

Acceptance scope (never unit) is deliberate: these commands run on the integrated
master tree via the in-loop gate + delivery, and must NOT gate a per-PR merge (a
whole-project acceptance script fails by construction on a single-module branch).

Import surface is stdlib + ``.runtime`` / ``.testing`` (the sanctioned execution
primitive) — no gateway/HTTP. Must NOT import ``runner`` (F159 ``paths.py``
discipline: ``runner`` imports this, not the reverse).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

# stderr/stdout signatures that mean the command could not RUN (vs a real test
# failure). A candidate that trips one of these is refused — registering it would
# create a gate that is red forever for an environment reason, wedging the run.
_UNRUNNABLE_SIGNATURES = (
    "cannot find module",              # node: missing dependency (jsdom, …)
    "no module named",                 # python: missing pytest / import
    "modulenotfounderror",
    "command not found",               # shell: interpreter absent from PATH
    "is not recognized",               # windows: interpreter absent
    "no such file or directory",       # entrypoint / interpreter missing
    "err_module_not_found",
)


def _list_master(workspace: Any) -> list[str]:
    try:
        return [f for f in workspace.list_files(scope="master") if f != ".gitignore"]
    except Exception:  # noqa: BLE001
        return []


def _detect_acceptance_command(files: list[str]) -> Optional[tuple[str, dict[str, Any]]]:
    """Propose ONE acceptance command that a runnable test file on master implies.
    Returns ``(command_id, spec)`` or ``None``. Grounded: only proposes an argv
    whose entrypoint file is present on master (the smoke run then proves it can
    actually execute)."""
    fileset = set(files)

    # 1) A team-authored JS test file the browser-less runtime runs directly with
    #    node — the gravity-golf case (test/acceptance.test.js). Prefer a file
    #    literally named for acceptance, else the first *.test.js under test(s)/.
    js_tests = sorted(
        f for f in files
        if f.endswith(".test.js")
        and (f.startswith("test/") or f.startswith("tests/") or "/test" in f))
    if js_tests:
        chosen = next((f for f in js_tests if "acceptance" in f), js_tests[0])
        return ("acceptance", {
            "argv": ["node", chosen], "cwd": ".", "timeout_seconds": 120,
            "label": f"acceptance ({chosen})", "scope": "acceptance"})

    # 2) A python test suite runnable with pytest.
    py_tests = [f for f in files
                if (f.startswith("tests/") or f.startswith("test/"))
                and f.endswith(".py") and "test" in f.rsplit("/", 1)[-1]]
    if py_tests:
        test_dir = "tests" if any(f.startswith("tests/") for f in py_tests) else "test"
        if any(f.startswith(f"{test_dir}/") for f in fileset):
            return ("acceptance", {
                "argv": ["python", "-m", "pytest", test_dir, "-q"], "cwd": ".",
                "timeout_seconds": 180, "label": f"acceptance (pytest {test_dir})",
                "scope": "acceptance"})

    return None


def _smoke_ran_cleanly(session: Any) -> tuple[bool, str]:
    """Did the candidate actually EXECUTE (regardless of pass/fail)? Returns
    ``(ran, reason)``. A real test failure (process completed, non-zero exit) is
    "ran" — that is the signal we want to register. A blocked/failed launch, or a
    completed run whose output shows a missing interpreter/module, is not."""
    results = list(getattr(session, "results", []) or [])
    if not results:
        return False, "no result"
    r = results[0]
    status = str(getattr(r, "status", ""))
    if status not in ("completed",):
        # blocked (sandbox), failed (launch), timed_out -> could not run cleanly.
        return False, f"status={status} ({getattr(r, 'reason', '') or 'launch failed'})"
    blob = (str(getattr(r, "stderr_preview", "") or "")
            + "\n" + str(getattr(r, "stdout_preview", "") or "")).lower()
    for sig in _UNRUNNABLE_SIGNATURES:
        if sig in blob:
            return False, f"unrunnable: matched {sig!r}"
    return True, "ran"


def maybe_bootstrap(store: Any, workspace: Any, policy: Any) -> None:
    """Idempotent, fail-open gate acquisition. Registers profiles/commands only
    when none are configured, so it never overwrites an operator's setup and is a
    no-op on every call after the first success. Any failure is swallowed — a
    bootstrap hiccup must never break a merge or a run."""
    if workspace is None or not getattr(policy, "gate_bootstrap", True):
        return
    try:
        _bootstrap_runtime(store, workspace)
    except Exception as exc:  # noqa: BLE001
        log.debug("gate_bootstrap runtime step failed: %s", exc)
    try:
        _bootstrap_acceptance_command(store, workspace)
    except Exception as exc:  # noqa: BLE001
        log.debug("gate_bootstrap command step failed: %s", exc)


def _bootstrap_runtime(store: Any, workspace: Any) -> None:
    from .runtime import RuntimeProfileStore, detect

    rstore = RuntimeProfileStore.for_ledger(store)
    if rstore.list_profiles():
        return  # already configured (operator or a prior bootstrap)
    proposals = detect(workspace.root(), project_id=store.project_id)
    if not proposals:
        return
    # Register EVERY proposal, not just the primary: detect() tries _detect_node
    # before _detect_static, so a jsdom-only package.json would hide the correct
    # `python -m http.server` static profile. runtime_resolve's grounded-or-refuse
    # rule discards a proposal whose start entrypoint is absent at use time.
    for p in proposals:
        rstore.upsert_profile(p)
    try:
        store.record_decision(
            title="gate bootstrapped: runtime", context="gate_bootstrap",
            choice="gate_bootstrapped",
            rationale=("registered runtime profiles from detection: "
                       + ", ".join(getattr(p, "profile_id", "?") for p in proposals)))
    except Exception:  # noqa: BLE001
        pass


def _bootstrap_acceptance_command(store: Any, workspace: Any) -> None:
    from .testing import run_test_commands

    if store.get_test_commands():
        return  # already configured — never overwrite
    files = _list_master(workspace)
    proposed = _detect_acceptance_command(files)
    if proposed is None:
        return
    cmd_id, spec = proposed
    # Smoke-run the candidate ONCE on master before registering. This is the D1
    # safeguard: a command whose entrypoint exists can still be unrunnable (needs
    # a dependency install no engine path performs). Only register what actually
    # executed.
    try:
        session = run_test_commands(
            workspace.root(), {cmd_id: spec}, [cmd_id],
            require_sandbox=store.get_require_sandbox())
    except Exception as exc:  # noqa: BLE001
        _refuse(store, cmd_id, f"smoke run raised: {exc}")
        return
    ran, reason = _smoke_ran_cleanly(session)
    if not ran:
        _refuse(store, cmd_id, reason)
        return
    store.set_test_commands({cmd_id: spec})
    try:
        store.record_decision(
            title="gate bootstrapped: acceptance command",
            context="gate_bootstrap", choice="gate_bootstrapped",
            rationale=(f"registered acceptance-scoped command {cmd_id!r} "
                       f"(argv={spec['argv']}); smoke run confirmed it executes"))
    except Exception:  # noqa: BLE001
        pass


def _refuse(store: Any, cmd_id: str, reason: str) -> None:
    try:
        store.record_decision(
            title="gate bootstrap refused a command",
            context="gate_bootstrap", choice="gate_bootstrap_refused",
            rationale=(f"candidate {cmd_id!r} not registered — {reason}; a command "
                       "that cannot run would be a red gate forever (a wedge, not "
                       "a gate)"))
    except Exception:  # noqa: BLE001
        pass


__all__ = ["maybe_bootstrap"]
