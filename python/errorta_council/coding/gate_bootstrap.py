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

import json
import logging
import re
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

# Signatures that mean the command could not RUN (vs a real test failure). A
# candidate that trips one of these is refused — registering it would create a
# gate that is red forever for an environment reason, wedging the run.
#
# Split into two tiers because some phrases are ambiguous. "No such file or
# directory" and "Cannot find module" print BOTH when an interpreter/loader fails
# to launch AND in the output of a real, running test that merely asserts on a
# missing file or an unresolved module. Substring-matching them anywhere would
# misclassify a genuinely-executing, genuinely-failing acceptance test as
# unrunnable and REFUSE it — the exact opposite of the D1 intent (a real non-zero
# failure DOES register). So the ambiguous tier only refuses when there is no
# evidence a test framework actually ran.

# Unambiguous launch failures: the interpreter/entrypoint itself never started a
# test. Safe to match anywhere in the output.
_LAUNCH_FAILURE_SIGNATURES = (
    "command not found",               # shell: interpreter absent from PATH
    "is not recognized",               # windows: interpreter absent
    "err_module_not_found",            # node: loader failed before user code
    "modulenotfounderror",             # python: import failed at collection
    "could not determine executable",  # npx: the runner package is not installed
)

# Ambiguous: appears in a failed launch AND in a real test's assertion output.
# Only treated as unrunnable when no test-framework output co-occurs (no test ran).
_AMBIGUOUS_LAUNCH_SIGNATURES = (
    "cannot find module",              # node: missing dependency (jsdom, …)
    "no module named",                 # python: missing import (sans traceback)
    "no such file or directory",       # entrypoint / interpreter missing
)

# Retained as the union for any caller/introspection that wants the full set.
_UNRUNNABLE_SIGNATURES = _LAUNCH_FAILURE_SIGNATURES + _AMBIGUOUS_LAUNCH_SIGNATURES

# Output characteristic of a test framework actually running tests. If any of
# these is present, a test executed — an ambiguous file/module phrase in the same
# output is then a real assertion failure (register), not a failed launch. The
# bootstrap only ever proposes node (*.test.js) and pytest, so these cover the
# runners in play; the list biases toward REGISTERING (matching one keeps a real
# test), which is the intended bias.
_TEST_OUTPUT_MARKERS = (
    "test session starts",             # pytest banner
    "collected ",                      # pytest collection line
    "short test summary",              # pytest
    " passed", " failed",              # pytest summary ("1 failed, 2 passed")
    "passing", "failing",              # mocha
    "test suites:", "tests:",          # jest
    "# tests ", "# pass ", "# fail ",  # node:test summary
    "\nok ", "\nnot ok ",              # TAP
    "✓", "✗",                          # mocha/jest/node tick marks
)


def _looks_like_test_output(blob: str) -> bool:
    """True if the process emitted output characteristic of a test framework
    actually running tests. Distinguishes a real test that RAN and failed a
    file/module assertion from an interpreter/loader that never launched a test."""
    return any(m in blob for m in _TEST_OUTPUT_MARKERS)


# SPEC-34 S2 — POSITIVE evidence that a green (exit 0) run executed ZERO tests. A
# suite that exits 0 having run nothing (empty suite, `--passWithNoTests`, a
# Playwright file mis-run so no `describe` registered) would otherwise register a
# gate that verifies nothing — the "prove-it-works-by-doing-nothing" hole the
# black-canvas oracle had. So the ONLY green refused is one whose own output SAYS
# it ran zero tests. A green run whose count is merely unreadable (a silent
# ``node assert`` script, a head-truncated pytest summary) is NOT refused — it ran
# clean and is registered exactly as before SPEC-34 (no false wedge, no regression;
# review lock: never refuse a run that genuinely passed).
_ZERO_TEST_SIGNATURES = (
    "0 passed", "0 passing", "0 tests passed", "no tests ran", "no tests found",
    "passwithnotests", "ran 0 tests", "collected 0 items", "# tests 0",
    "# pass 0", "tests:       0", "tests: 0",
)


def _ran_zero_tests(blob: str) -> bool:
    """SPEC-34 S2: does the runner's own output POSITIVELY report zero executed
    tests? Conservative by design — absence of a count is NOT zero (that would
    wedge a silently-passing test); only an explicit zero-count marker counts."""
    b = blob.lower()
    return any(sig in b for sig in _ZERO_TEST_SIGNATURES)


def _list_master(workspace: Any) -> list[str]:
    try:
        return [f for f in workspace.list_files(scope="master") if f != ".gitignore"]
    except Exception:  # noqa: BLE001
        return []


# SPEC-34 S1 — a JS test file is not always run with bare ``node``. Invoking a
# Playwright/vitest/jest/mocha suite as ``node <file>`` throws at import (run 10:
# ``node acceptance.test.js`` -> "Playwright Test did not expect test.describe() to
# be called here", exit 1) and the suite is then dropped as "unrunnable" — a
# MIS-INVOCATION mistaken for a missing runtime. So when the project declares its
# own runner (a framework dependency or a ``playwright.config``), propose THAT
# runner. ``node <file>`` remains the fallback for a plain assert/``node:test``
# script that declares nothing.
_PW_CONFIG_RE = re.compile(r"(^|/)playwright\.config\.[cm]?[jt]s$")
_VITEST_CONFIG_RE = re.compile(r"(^|/)v(itest|ite)\.config\.[cm]?[jt]s$")
_JEST_CONFIG_RE = re.compile(r"(^|/)jest\.config\.[cm]?[jt]s$")


def _read_package_json(read_master: Optional[Callable[[str], "bytes | None"]]
                       ) -> dict[str, Any]:
    """Best-effort parse of master ``package.json`` (``{}`` on any problem)."""
    if read_master is None:
        return {}
    try:
        raw = read_master("package.json")
        if not raw:
            return {}
        obj = json.loads(raw.decode("utf-8", "replace"))
        return obj if isinstance(obj, dict) else {}
    except Exception:  # noqa: BLE001 — a malformed manifest just means "no signal"
        return {}


def _choose_js_argv(
    chosen: str, files: list[str], pkg: dict[str, Any]
) -> tuple[list[str], Optional[str]]:
    """Pick the argv that actually RUNS ``chosen`` given what the project declares,
    plus a ``runtime_hint`` naming a runtime the network-off/minimal-env executor
    cannot provision (so an honest ``test_runtime_unavailable`` beats a misleading
    exit-1). Order: framework dependency / config wins, then a declared ``test``
    script, then bare ``node`` for a script that needs no framework."""
    deps = {}
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        d = pkg.get(key)
        if isinstance(d, dict):
            deps.update(d)
    scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
    has_cfg = lambda rx: any(rx.search(f) for f in files)  # noqa: E731

    # A browser runtime (Playwright — the @playwright/test RUNNER, a config, OR a
    # plain ``playwright``/``playwright-core`` library dep) needs a real browser +
    # webServer the sandbox (network-off, no Chromium) cannot stand up. The
    # ``browser`` hint routes a non-green smoke to the non-blocking SPEC-31 ack
    # instead of a red-forever gate — so it must be set whenever a browser is needed,
    # INCLUDING the run-11 shape (plain ``playwright`` dep + a ``scripts.test`` that
    # runs a node script driving chromium), not only for the @playwright/test runner.
    needs_browser = (
        "@playwright/test" in deps or "playwright" in deps
        or "playwright-core" in deps or has_cfg(_PW_CONFIG_RE))
    if "@playwright/test" in deps or has_cfg(_PW_CONFIG_RE):
        return (["npx", "--no-install", "playwright", "test"], "browser")
    if "vitest" in deps or has_cfg(_VITEST_CONFIG_RE):
        return (["npx", "--no-install", "vitest", "run"], None)
    if "jest" in deps or has_cfg(_JEST_CONFIG_RE):
        return (["npx", "--no-install", "jest"], None)
    if "mocha" in deps:
        return (["npx", "--no-install", "mocha", chosen], None)
    hint = "browser" if needs_browser else None
    if isinstance(scripts.get("test"), str) and scripts["test"].strip():
        return (["npm", "test", "--silent"], hint)
    # Nothing declared: a plain assert or ``node:test`` file runs under node.
    return (["node", chosen], hint)


def _detect_acceptance_command(
    files: list[str],
    read_master: Optional[Callable[[str], "bytes | None"]] = None,
) -> Optional[tuple[str, dict[str, Any]]]:
    """Propose ONE acceptance command that a runnable test file on master implies.
    Returns ``(command_id, spec)`` or ``None``. Grounded: only proposes an argv
    whose entrypoint file is present on master (the smoke run then proves it can
    actually execute). ``read_master`` (a ``rel_path -> bytes|None`` reader) lets
    S1 consult ``package.json`` for the project's declared runner; when omitted the
    JS path falls back to bare ``node <file>``."""
    fileset = set(files)

    # 1) A team-authored JS test file. Prefer a file literally named for
    #    acceptance, else the first *.test.js under test(s)/. Then invoke it with
    #    the runner the project actually declares (SPEC-34 S1), not blindly `node`.
    js_tests = sorted(
        f for f in files
        if f.endswith(".test.js")
        and (f.startswith("test/") or f.startswith("tests/") or "/test" in f))
    if js_tests:
        chosen = next((f for f in js_tests if "acceptance" in f), js_tests[0])
        argv, runtime_hint = _choose_js_argv(
            chosen, files, _read_package_json(read_master))
        spec: dict[str, Any] = {
            "argv": argv, "cwd": ".", "timeout_seconds": 120,
            "label": f"acceptance ({chosen})", "scope": "acceptance"}
        if runtime_hint:
            spec["runtime_hint"] = runtime_hint
        return ("acceptance", spec)

    # 1b) SPEC-36 (fix B) — no *.test.js matched, but the project DECLARES a test
    #     script AND ships a JS/TS test file under test(s)/. Run 11 authored
    #     test/acceptance.js (not *.test.js) with package.json
    #     "scripts":{"test":"node test/acceptance.js"}; nothing matched, so no
    #     acceptance command registered and SPEC-35's done-gate saw no_gate (a
    #     silent miss). Propose the DECLARED runner via _choose_js_argv, grounded on
    #     a real test file. Skip the npm-init placeholder ("... no test specified
    #     ... && exit 1"): running it exits 1 with no framework output and would
    #     register a gate that is red forever (a wedge). This CLOSES the detection
    #     gap; a browser test still smoke-fails to a non-blocking
    #     test_runtime_unavailable (that runnability gap is a separate change), so
    #     the effect is to turn a silent no_gate into an honest acknowledgement.
    pkg = _read_package_json(read_master)
    _scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
    declared = _scripts.get("test") if isinstance(_scripts.get("test"), str) else ""
    if declared.strip() and "no test specified" not in declared.lower():
        dir_tests = sorted(
            f for f in files
            if (f.startswith("test/") or f.startswith("tests/") or "/test" in f)
            and f.rsplit(".", 1)[-1] in (
                "js", "mjs", "cjs", "jsx", "ts", "tsx", "mts", "cts"))
        if dir_tests:
            chosen = next((f for f in dir_tests if "acceptance" in f), dir_tests[0])
            argv, runtime_hint = _choose_js_argv(chosen, files, pkg)
            spec = {
                "argv": argv, "cwd": ".", "timeout_seconds": 120,
                "label": f"acceptance ({chosen} via declared test script)",
                "scope": "acceptance"}
            if runtime_hint:
                spec["runtime_hint"] = runtime_hint
            return ("acceptance", spec)

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


def _result_blob(r: Any) -> str:
    """Lower-cased stderr+stdout preview of a ``TestRunResult`` (``""`` if None)."""
    if r is None:
        return ""
    return (str(getattr(r, "stderr_preview", "") or "")
            + "\n" + str(getattr(r, "stdout_preview", "") or "")).lower()


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
    blob = _result_blob(r)
    # Unambiguous launch failures refuse regardless of anything else.
    for sig in _LAUNCH_FAILURE_SIGNATURES:
        if sig in blob:
            return False, f"unrunnable: launch failure {sig!r}"
    # Ambiguous file/module phrases only mean "could not run" when no test
    # framework actually ran; if a test executed, this is a real assertion
    # failure and IS the signal we want to register.
    if not _looks_like_test_output(blob):
        for sig in _AMBIGUOUS_LAUNCH_SIGNATURES:
            if sig in blob:
                return False, f"unrunnable: {sig!r} with no test output"
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
    # Memoize the ONE smoke attempt. A refused candidate registers nothing, so
    # without this guard `maybe_bootstrap` would re-run the smoke `run_test_commands`
    # on *every* subsequent merge — a repeated subprocess on the merge turn plus a
    # `gate_bootstrap_refused` decision each time (the smoke's failure reason —
    # missing jsdom, absent interpreter — is environmental and does not change
    # mid-run). Set the flag only AFTER a real smoke attempt, never when no
    # candidate exists yet: a later merge may add the test file.
    try:
        if store.get_run_state().get("gate_cmd_bootstrap_resolved"):
            return
    except Exception:  # noqa: BLE001
        pass
    files = _list_master(workspace)
    proposed = _detect_acceptance_command(
        files, read_master=getattr(workspace, "read_master_file", None))
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
        _mark_cmd_resolved(store)
        return
    ran, reason = _smoke_ran_cleanly(session)
    if not ran:
        # SPEC-31: a candidate the team authored (and the DoD may require) that
        # cannot EXECUTE is not silently dropped. The refusal is still correct (a
        # command that cannot run is not a gate), but it is recorded as a distinct
        # `test_runtime_unavailable` fact AND persisted to run_state, so the
        # completion path can honestly acknowledge that the authored acceptance test
        # was NOT run — instead of a `done` that overstates "tested" (run 10 shipped
        # definition_of_done with an unexecuted acceptance test, and nothing said
        # so). Provisioning the test runtime so it CAN run is a separate change:
        # the executor gives test runs a minimal env by design, so resolving Node
        # deps needs a sanctioned deps step, not arbitrary env injection.
        _record_test_runtime_unavailable(store, cmd_id, spec, reason)
        _mark_cmd_resolved(store)
        return
    r0 = (list(getattr(session, "results", []) or []) or [None])[0]
    green = getattr(r0, "exit_code", None) == 0
    blob = _result_blob(r0)
    # SPEC-34 S2 — a GREEN (exit 0) smoke that its OWN output says ran ZERO tests is
    # not a gate (empty suite, `--passWithNoTests`, a mis-run framework file that
    # registered no test). Refuse only on that positive zero-evidence; a green run
    # whose count is merely unreadable still registers (no false wedge). A real
    # non-zero failure also still registers — it IS a valid gate that runs red.
    if green and _ran_zero_tests(blob):
        _record_test_runtime_unavailable(
            store, cmd_id, spec,
            "ran clean but its output reports zero executed tests (a green gate "
            "that verifies nothing)")
        _mark_cmd_resolved(store)
        return
    # SPEC-34 S1 — a suite that declares a runtime the executor cannot provision (a
    # Playwright browser + webServer: network-off, no Chromium) can exit non-zero
    # WITHOUT a launch-failure signature (npx "could not determine executable", a
    # webServer connect error). Registering that makes it red forever — the wedge
    # SPEC-34 warns against. So a ``runtime_hint`` candidate that did not end GREEN is
    # recorded as unavailable (SPEC-31 ack), never registered as a red gate.
    if spec.get("runtime_hint") and not green:
        _record_test_runtime_unavailable(
            store, cmd_id, spec,
            f"{spec['runtime_hint']} runtime not provisionable here — {reason}; "
            f"exit={getattr(r0, 'exit_code', None)}")
        _mark_cmd_resolved(store)
        return
    store.set_test_commands({cmd_id: spec})
    _mark_cmd_resolved(store)
    try:
        store.record_decision(
            title="gate bootstrapped: acceptance command",
            context="gate_bootstrap", choice="gate_bootstrapped",
            rationale=(f"registered acceptance-scoped command {cmd_id!r} "
                       f"(argv={spec['argv']}); smoke run confirmed it executes"))
    except Exception:  # noqa: BLE001
        pass


def _mark_cmd_resolved(store: Any) -> None:
    """Record that the one-shot acceptance-command smoke has run (registered or
    refused), so it is not re-attempted on every subsequent merge."""
    try:
        store.set_run_state(gate_cmd_bootstrap_resolved=True)
    except Exception:  # noqa: BLE001
        pass


def _record_test_runtime_unavailable(
    store: Any, cmd_id: str, spec: dict[str, Any], reason: str) -> None:
    """SPEC-31: the authored acceptance test exists on master but could not be
    EXECUTED (or a green run reported zero tests). Record it as a distinct,
    first-class fact and persist it to run_state so the completion path can honestly
    acknowledge the test was not run — instead of a `done` that silently overstates
    "tested". This is the same refusal as before (an un-runnable/vacuous command is
    not registered as a gate); it is NON-BLOCKING by design — blocking `done` on it
    would wedge a run whose test simply cannot run or cannot be quantified in this
    environment (the review's blocker: no recovery path exists to lift such a block).
    A recoverable hard gate is deferred (SPEC-34 follow-on)."""
    try:
        store.record_decision(
            title="acceptance test present but not executed",
            context="gate_bootstrap", choice="test_runtime_unavailable",
            rationale=(f"the authored acceptance test {cmd_id!r} "
                       f"(argv={spec.get('argv')}) was not registered as a gate — "
                       f"{reason}. Rendering is still verified by the web:probe, but "
                       "the authored acceptance test was NOT executed; recorded so "
                       "`done` does not overstate coverage (SPEC-31)."))
    except Exception:  # noqa: BLE001
        pass
    try:
        store.set_run_state(acceptance_test_unrun={
            "command_id": cmd_id, "argv": list(spec.get("argv") or []),
            "reason": str(reason)})
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
