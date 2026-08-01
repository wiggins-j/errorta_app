"""SPEC-34 — run the team's own behavioral oracle, and gate on it.

Run 10 shipped a numerically inert gravity mechanic to definition_of_done: the
council authored the correct acceptance test but it was mis-invoked as
``node acceptance.test.js`` (a Playwright suite -> exit 1) and dropped, while the
only firing oracle (web:probe) checks render+input, blind to trajectory. These
tests lock the four in-scope parts:

* S1 — ``_detect_acceptance_command`` proposes the project's DECLARED runner
  (playwright/vitest/jest/mocha/npm-test) instead of blindly ``node <file>``.
* S2 — a GREEN smoke is registered only when it PROVES it asserted something.
* S3 — a provisionable, DoD-required, un-run acceptance test BLOCKS ``done``;
  the genuinely-unprovisionable case is the escape hatch (no wedge).
* S4 — ``done`` records which oracles actually verified the artifact.
"""
from __future__ import annotations

from typing import Optional

from errorta_council.coding import gate_bootstrap as gb
from errorta_council.coding.runner import (
    _acceptance_unrun_blocks_done,
    _dod_requires_acceptance,
    _record_completion_oracles,
)


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
    # up: flag it so the eventual refusal is classified unprovisionable (S3).
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
# S2 — a green gate must prove it asserted something
# --------------------------------------------------------------------------- #
def test_s2_assertions_executed_parses_common_runners() -> None:
    assert gb._assertions_executed("Tests: 3 passed, 3 total")   # jest
    assert gb._assertions_executed("12 passing")                 # mocha
    assert gb._assertions_executed("# pass 4")                   # node:test
    assert gb._assertions_executed("5 tests passed")             # vitest
    assert gb._assertions_executed("collected 2 items\n2 passed")  # pytest
    assert gb._assertions_executed("  ✓ sinks on a curve")       # tick
    # vacuous / nothing asserted
    assert not gb._assertions_executed("")
    assert not gb._assertions_executed("no tests ran")
    assert not gb._assertions_executed("0 passed")
    assert not gb._assertions_executed("Done in 0.4s")


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


def _run_command_step(monkeypatch, session: _Session, spec_extra=None) -> _BSStore:
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


def test_s2_green_with_no_assertions_is_not_registered(monkeypatch) -> None:
    store = _run_command_step(monkeypatch, _Session([_Result(0, stdout="Done.")]))
    assert store.get_test_commands() == {}, "a vacuous green must not register"
    assert any(d["choice"] == "test_runtime_unavailable" for d in store.decisions)


def test_s2_green_with_a_pass_count_is_registered(monkeypatch) -> None:
    store = _run_command_step(
        monkeypatch, _Session([_Result(0, stdout="2 passing\n  ✓ curves")]))
    assert "acceptance" in store.get_test_commands()
    assert any(d["choice"] == "gate_bootstrapped" for d in store.decisions)


def test_s2_real_failure_still_registers_a_red_gate(monkeypatch) -> None:
    # A genuine non-zero failure IS a valid gate (it runs red) — S2 only guards the
    # vacuous GREEN case, so this must still register.
    store = _run_command_step(
        monkeypatch, _Session([_Result(1, stdout="1 passing\n1 failing")]))
    assert "acceptance" in store.get_test_commands()


def test_s1_unprovisionable_browser_suite_is_not_registered_as_a_red_gate(
        monkeypatch) -> None:
    # A declared Playwright suite whose browser/webServer cannot be provisioned here
    # exits non-zero WITHOUT a launch-failure signature (npx could-not-determine, a
    # webServer connect error). It must be recorded unprovisionable (escape hatch),
    # NOT registered as a gate that is red forever (the wedge SPEC-34 warns against).
    store = _run_command_step(
        monkeypatch,
        _Session([_Result(1, stderr="npm error could not determine executable")]),
        spec_extra={"argv": ["npx", "--no-install", "playwright", "test"],
                    "runtime_hint": "browser"})
    assert store.get_test_commands() == {}, "unprovisionable suite must not register"
    unrun = store.get_run_state()["acceptance_test_unrun"]
    assert unrun["unprovisionable"] is True  # escape hatch -> done not wedged


# --------------------------------------------------------------------------- #
# S3 — classify + block done
# --------------------------------------------------------------------------- #
def test_s3_classify_unprovisionable() -> None:
    assert gb._classify_unprovisionable({"runtime_hint": "browser"}, "anything")
    assert gb._classify_unprovisionable({}, "Error: Cannot find module 'jsdom'")
    assert gb._classify_unprovisionable({}, "chromium executable doesn't exist")
    # a fixable gap (green-but-vacuous) is provisionable
    assert not gb._classify_unprovisionable(
        {}, "ran clean but reported no executed tests/assertions")


def test_s3_record_sets_unprovisionable_flag() -> None:
    store = _BSStore()
    gb._record_test_runtime_unavailable(
        store, "acceptance", {"argv": ["npx", "playwright", "test"],
                              "runtime_hint": "browser"}, "no chromium")
    assert store.get_run_state()["acceptance_test_unrun"]["unprovisionable"] is True
    store2 = _BSStore()
    gb._record_test_runtime_unavailable(
        store2, "acceptance", {"argv": ["node", "x"]},
        "ran clean but reported no executed tests/assertions")
    assert store2.get_run_state()["acceptance_test_unrun"]["unprovisionable"] is False


class _Proj:
    def __init__(self, dod: str) -> None:
        self.definition_of_done = dod


class _DoneStore:
    def __init__(self, unrun=None, dod="") -> None:
        self._rs = {"acceptance_test_unrun": unrun} if unrun else {}
        self._dod = dod
        self.decisions: list = []

    def get_run_state(self) -> dict: return dict(self._rs)
    def get_project(self): return _Proj(self._dod)
    def record_decision(self, **kw) -> None: self.decisions.append(kw)


def test_s3_dod_requires_acceptance() -> None:
    assert _dod_requires_acceptance("iterate until the acceptance gate passes")
    assert _dod_requires_acceptance("all tests green")
    assert _dod_requires_acceptance("the mechanic is verified")
    assert not _dod_requires_acceptance("ship a fun playable game")


_PROVISIONABLE = {"command_id": "acceptance", "argv": ["node", "x"],
                  "reason": "vacuous", "unprovisionable": False}
_UNPROVISIONABLE = {"command_id": "acceptance", "argv": ["npx", "playwright"],
                    "reason": "no browser", "unprovisionable": True}


def test_s3_blocks_when_provisionable_and_dod_requires() -> None:
    reason = _acceptance_unrun_blocks_done(
        _DoneStore(unrun=_PROVISIONABLE, dod="acceptance gate must pass"))
    assert reason and "never run" in reason


def test_s3_escape_hatch_unprovisionable_never_wedges() -> None:
    assert _acceptance_unrun_blocks_done(
        _DoneStore(unrun=_UNPROVISIONABLE, dod="acceptance gate must pass")) is None


def test_s3_no_block_when_dod_is_silent_on_testing() -> None:
    assert _acceptance_unrun_blocks_done(
        _DoneStore(unrun=_PROVISIONABLE, dod="a fun playable game")) is None


def test_s3_no_block_when_nothing_unrun() -> None:
    assert _acceptance_unrun_blocks_done(_DoneStore(dod="acceptance required")) is None


# --------------------------------------------------------------------------- #
# S4 — oracle provenance at done
# --------------------------------------------------------------------------- #
def test_s4_records_oracle_provenance_when_test_ran() -> None:
    store = _DoneStore()
    _record_completion_oracles(store)
    oracles = [d for d in store.decisions if d["choice"] == "completion_oracles"]
    assert len(oracles) == 1
    r = oracles[0]["rationale"]
    assert "liveness gate" in r and "acceptance test" in r


def test_s4_names_the_test_as_unrun_when_flagged() -> None:
    store = _DoneStore(unrun=_UNPROVISIONABLE)
    _record_completion_oracles(store)
    r = [d for d in store.decisions if d["choice"] == "completion_oracles"][0]["rationale"]
    assert "NOT executed" in r
