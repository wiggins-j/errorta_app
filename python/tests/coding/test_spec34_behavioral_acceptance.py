"""SPEC-34 — run the team's own behavioral oracle, and gate on it.

Run 10 shipped a numerically inert gravity mechanic to definition_of_done: the
council authored the correct acceptance test but it was mis-invoked as
``node acceptance.test.js`` (a Playwright suite -> exit 1) and dropped, while the
only firing oracle (web:probe) checks render+input, blind to trajectory. These
tests lock the in-scope parts (a hard done-block is deferred as a SPEC-34 follow-on
because a correct one needs a recovery/re-smoke path — see the spec):

* S1 — ``_detect_acceptance_command`` proposes the project's DECLARED runner
  (playwright/vitest/jest/mocha/npm-test) instead of blindly ``node <file>``.
* S2 — a green smoke is refused ONLY on positive zero-test evidence (never a
  silently-passing test, so a working run is never wedged); real failures still
  register a red gate.
* S4 — ``done`` records which oracles actually verified the artifact, and never
  claims a test ran when none was authored.
"""
from __future__ import annotations

from typing import Optional

from errorta_council.coding import gate_bootstrap as gb
from errorta_council.coding.runner import _record_completion_oracles


# --------------------------------------------------------------------------- #
# S1 — detect the project's declared runner
# --------------------------------------------------------------------------- #
def _reader(pkg_json: Optional[bytes]):
    def read(rel: str) -> Optional[bytes]:
        return pkg_json if rel == "package.json" else None
    return read


_JS = ["test/acceptance.test.js", "index.html", "game.js"]


def test_s1_playwright_dep_uses_playwright_runner_and_flags_browser() -> None:
    got = gb._detect_acceptance_command(
        _JS, read_master=_reader(b'{"devDependencies":{"@playwright/test":"1"}}'))
    assert got is not None
    _, spec = got
    assert spec["argv"] == ["npx", "--no-install", "playwright", "test"]
    # Playwright needs a browser + webServer the network-off executor can't stand
    # up: the hint routes a non-green smoke to the non-blocking ack, not a red gate.
    assert spec.get("runtime_hint") == "browser"


def test_s1_playwright_config_file_alone_is_enough() -> None:
    files = _JS + ["playwright.config.ts"]
    got = gb._detect_acceptance_command(files, read_master=_reader(None))
    assert got and got[1]["argv"] == ["npx", "--no-install", "playwright", "test"]


def test_s1_vitest_and_jest_and_mocha() -> None:
    v = gb._detect_acceptance_command(
        _JS, read_master=_reader(b'{"devDependencies":{"vitest":"1"}}'))
    assert v and v[1]["argv"] == ["npx", "--no-install", "vitest", "run"]
    assert "runtime_hint" not in v[1]
    j = gb._detect_acceptance_command(
        _JS, read_master=_reader(b'{"dependencies":{"jest":"29"}}'))
    assert j and j[1]["argv"] == ["npx", "--no-install", "jest"]
    m = gb._detect_acceptance_command(
        _JS, read_master=_reader(b'{"devDependencies":{"mocha":"10"}}'))
    assert m and m[1]["argv"] == ["npx", "--no-install", "mocha",
                                  "test/acceptance.test.js"]


def test_s1_scripts_test_when_no_framework_dep() -> None:
    got = gb._detect_acceptance_command(
        _JS, read_master=_reader(b'{"scripts":{"test":"node --test"}}'))
    assert got and got[1]["argv"] == ["npm", "test", "--silent"]


def test_s1_no_package_json_falls_back_to_node() -> None:
    # Backward compatible with the pre-SPEC-34 behaviour (spec12 lock).
    got = gb._detect_acceptance_command(_JS, read_master=_reader(None))
    assert got and got[1]["argv"] == ["node", "test/acceptance.test.js"]
    # ...and the no-reader call path is identical.
    assert gb._detect_acceptance_command(_JS)[1]["argv"] == \
        ["node", "test/acceptance.test.js"]


def test_s1_malformed_package_json_is_ignored() -> None:
    got = gb._detect_acceptance_command(_JS, read_master=_reader(b"{not json"))
    assert got and got[1]["argv"] == ["node", "test/acceptance.test.js"]



# --------------------------------------------------------------------------- #
# S2 — refuse a green gate ONLY on positive zero-test evidence
# --------------------------------------------------------------------------- #
def test_s2_ran_zero_tests_only_on_positive_evidence() -> None:
    # positive zero-count evidence -> refuse
    assert gb._ran_zero_tests("Tests: 0 passed, 0 total")
    assert gb._ran_zero_tests("no tests ran")
    assert gb._ran_zero_tests("collected 0 items")
    assert gb._ran_zero_tests("0 passing")
    assert gb._ran_zero_tests("Ran 0 tests in 0.0s")
    # a genuinely-passing / unquantified run is NOT zero (must never wedge it)
    assert not gb._ran_zero_tests("")                       # silent node assert
    assert not gb._ran_zero_tests("2 passing\n  ✓ curves")  # real pass
    assert not gb._ran_zero_tests("Done in 0.4s")           # unquantified
    assert not gb._ran_zero_tests("....\n(summary truncated")  # head-truncated


class _Result:
    def __init__(self, exit_code, stdout="", stderr="", status="completed"):
        self.exit_code = exit_code
        self.stdout_preview = stdout
        self.stderr_preview = stderr
        self.status = status
        self.reason = ""


class _Session:
    def __init__(self, results):
        self.results = results


class _BSStore:
    """Minimal store for the bootstrap command step."""
    def __init__(self) -> None:
        self._cmds: dict = {}
        self._rs: dict = {}
        self.decisions: list = []

    def get_test_commands(self) -> dict: return dict(self._cmds)
    def set_test_commands(self, c) -> None: self._cmds = dict(c)
    def get_run_state(self) -> dict: return dict(self._rs)
    def set_run_state(self, **p) -> None: self._rs.update(p)
    def get_require_sandbox(self) -> bool: return False
    def record_decision(self, **kw) -> None: self.decisions.append(kw)


class _WS:
    def root(self): return "/tmp/nope"
    def list_files(self, scope=None): return ["test/acceptance.test.js"]
    def read_master_file(self, rel): return None


def _run_command_step(monkeypatch, session, spec_extra=None) -> _BSStore:
    store = _BSStore()
    spec = {"argv": ["node", "test/acceptance.test.js"], "cwd": ".",
            "timeout_seconds": 10, "scope": "acceptance", "label": "acc"}
    spec.update(spec_extra or {})
    monkeypatch.setattr(
        gb, "_detect_acceptance_command",
        lambda files, **_kw: ("acceptance", spec))
    from errorta_council.coding import testing as _testing
    monkeypatch.setattr(_testing, "run_test_commands", lambda *a, **k: session)
    gb._bootstrap_acceptance_command(store, _WS())
    return store


def test_s2_green_reporting_zero_tests_is_not_registered(monkeypatch) -> None:
    store = _run_command_step(
        monkeypatch, _Session([_Result(0, stdout="Test Suites: 0\nno tests ran")]))
    assert store.get_test_commands() == {}, "a proven-vacuous green must not register"
    assert any(d["choice"] == "test_runtime_unavailable" for d in store.decisions)


def test_s2_silently_passing_test_still_registers(monkeypatch) -> None:
    # REVIEW LOCK: a node `assert` file passes SILENTLY (exit 0, empty output). It
    # ran and passed — it must NOT be refused as "vacuous" (that would wedge a
    # working project). Pre-SPEC-34 behaviour is preserved for the unquantified case.
    store = _run_command_step(monkeypatch, _Session([_Result(0, stdout="")]))
    assert "acceptance" in store.get_test_commands()
    assert any(d["choice"] == "gate_bootstrapped" for d in store.decisions)


def test_s2_green_with_a_pass_count_is_registered(monkeypatch) -> None:
    store = _run_command_step(
        monkeypatch, _Session([_Result(0, stdout="2 passing\n  ✓ curves")]))
    assert "acceptance" in store.get_test_commands()


def test_s2_real_failure_still_registers_a_red_gate(monkeypatch) -> None:
    # A genuine non-zero failure IS a valid gate (it runs red) — must still register.
    store = _run_command_step(
        monkeypatch, _Session([_Result(1, stdout="1 passing\n1 failing")]))
    assert "acceptance" in store.get_test_commands()


def test_s1_unprovisionable_browser_suite_is_not_registered_as_a_red_gate(
        monkeypatch) -> None:
    # A declared Playwright suite whose browser/webServer cannot be provisioned here
    # exits non-zero WITHOUT a launch-failure signature (npx could-not-determine, a
    # webServer connect error). It must be recorded unavailable (SPEC-31 ack), NOT
    # registered as a gate that is red forever (the wedge SPEC-34 warns against).
    store = _run_command_step(
        monkeypatch,
        _Session([_Result(1, stderr="npm error could not determine executable")]),
        spec_extra={"argv": ["npx", "--no-install", "playwright", "test"],
                    "runtime_hint": "browser"})
    assert store.get_test_commands() == {}, "unprovisionable suite must not register"
    assert store.get_run_state().get("acceptance_test_unrun")  # non-blocking ack


def test_record_unavailable_is_non_blocking_ack_only() -> None:
    # SPEC-31 behaviour preserved: a plain (command_id, argv, reason) record, no
    # done-blocking flag (the review's blocker: a block here has no recovery path).
    store = _BSStore()
    gb._record_test_runtime_unavailable(
        store, "acceptance", {"argv": ["node", "x"]}, "no browser")
    unrun = store.get_run_state()["acceptance_test_unrun"]
    assert unrun["command_id"] == "acceptance" and "no browser" in unrun["reason"]
    assert "unprovisionable" not in unrun  # no blocking machinery
    assert any(d["choice"] == "test_runtime_unavailable" for d in store.decisions)


# --------------------------------------------------------------------------- #
# S4 — oracle provenance at done (never claims a test ran when none was authored)
# --------------------------------------------------------------------------- #
class _DoneStore:
    def __init__(self, unrun=None, cmds=None) -> None:
        self._rs = {"acceptance_test_unrun": unrun} if unrun else {}
        self._cmds = dict(cmds or {})
        self.decisions: list = []

    def get_run_state(self) -> dict: return dict(self._rs)
    def get_test_commands(self) -> dict: return dict(self._cmds)
    def record_decision(self, **kw) -> None: self.decisions.append(kw)


def _oracle_rationale(store) -> str:
    _record_completion_oracles(store)
    d = [x for x in store.decisions if x["choice"] == "completion_oracles"]
    assert len(d) == 1
    return d[0]["rationale"]


def test_s4_distinguishes_liveness_from_acceptance() -> None:
    r = _oracle_rationale(_DoneStore(
        cmds={"acceptance": {"scope": "acceptance"}}))
    assert "liveness gate" in r and "executed (registered acceptance gate)" in r


def test_s4_says_none_authored_when_no_test_exists() -> None:
    # REVIEW LOCK: with no authored test, S4 must NOT claim one executed.
    r = _oracle_rationale(_DoneStore(cmds={}))
    assert "none authored" in r
    assert "executed (registered acceptance gate)" not in r


def test_s4_names_the_test_as_unrun_when_flagged() -> None:
    r = _oracle_rationale(_DoneStore(
        unrun={"command_id": "acceptance", "argv": ["x"], "reason": "no browser"}))
    assert "NOT executed" in r
