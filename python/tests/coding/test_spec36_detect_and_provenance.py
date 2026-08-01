"""SPEC-36 — close the detection gap + honest oracle provenance.

Run 11 shipped inert gravity to definition_of_done although the council authored a
valid straight-line-solver at test/acceptance.js: (B) gate_bootstrap only matched
*.test.js so the declared test was never detected -> no acceptance command -> SPEC-35
saw no_gate -> done allowed; and (C) SPEC-34 S4 then reported "none authored" though
the test existed on master. These lock both fixes.

B is necessary-not-sufficient (a browser test still smoke-fails to a non-blocking
test_runtime_unavailable); the behavioral oracle (SPEC-37) is what actually blocks.
"""
from __future__ import annotations

from typing import Optional

from errorta_council.coding import gate_bootstrap as gb
from errorta_council.coding.runner import (
    _AUTHORED_TEST_RE,
    _record_completion_oracles,
    _tree_has_authored_test,
)


def _reader(pkg: Optional[bytes]):
    def read(rel: str) -> Optional[bytes]:
        return pkg if rel == "package.json" else None
    return read


# --------------------------------------------------------------------------- #
# Fix B — detect the project's DECLARED test script (not only *.test.js)
# --------------------------------------------------------------------------- #
_RUN11_FILES = ["test/acceptance.js", "package.json", "game.js", "physics.js",
                "levels.js", "index.html"]


def test_b_detects_declared_test_script_for_plainly_named_file() -> None:
    # The run-11 case: test/acceptance.js + scripts.test, plain `playwright` dep.
    pkg = (b'{"scripts":{"test":"node test/acceptance.js"},'
           b'"devDependencies":{"playwright":"^1.40.0"}}')
    got = gb._detect_acceptance_command(_RUN11_FILES, read_master=_reader(pkg))
    assert got is not None, "declared test + a test/*.js file must now be detected"
    cmd_id, spec = got
    assert cmd_id == "acceptance" and spec["scope"] == "acceptance"
    # plain `playwright` (not @playwright/test) + scripts.test -> npm test
    assert spec["argv"] == ["npm", "test", "--silent"]
    assert "acceptance.js" in spec["label"]


def test_b_dot_test_js_branch_keeps_priority_over_1b() -> None:
    # The pre-existing *.test.js branch runs FIRST, so it selects the file (its
    # argv comes from _choose_js_argv — npm test here because scripts.test is
    # declared — but the label proves the .test.js branch won, not the 1b path).
    files = ["test/foo.test.js", "test/acceptance.js", "package.json"]
    got = gb._detect_acceptance_command(
        files, read_master=_reader(b'{"scripts":{"test":"x"}}'))
    assert got and "foo.test.js" in got[1]["label"]
    assert "via declared test script" not in got[1]["label"]


def test_b_dot_test_js_no_scripts_is_bare_node() -> None:
    # golden lock: a *.test.js with a package.json that has no test script -> node.
    got = gb._detect_acceptance_command(
        ["test/foo.test.js", "package.json"], read_master=_reader(b'{"name":"x"}'))
    assert got and got[1]["argv"] == ["node", "test/foo.test.js"]


def test_b_skips_npm_init_placeholder() -> None:
    # `npm init` leaves a placeholder test script; running it would register a
    # red-forever gate. Must NOT propose from it.
    pkg = b'{"scripts":{"test":"echo \\"Error: no test specified\\" && exit 1"}}'
    got = gb._detect_acceptance_command(_RUN11_FILES, read_master=_reader(pkg))
    assert got is None, "the npm-init placeholder must not be proposed"


def test_b_no_test_file_no_proposal() -> None:
    # A declared script but no JS/TS test file under test(s)/ -> nothing to run.
    files = ["src/app.js", "package.json", "index.html"]
    got = gb._detect_acceptance_command(
        files, read_master=_reader(b'{"scripts":{"test":"node x.js"}}'))
    assert got is None


def test_b_no_package_json_still_falls_back_to_node() -> None:
    # Backward-compat: a *.test.js with no package.json is still bare node.
    got = gb._detect_acceptance_command(
        ["test/a.test.js"], read_master=_reader(None))
    assert got and got[1]["argv"] == ["node", "test/a.test.js"]


def test_b_no_declared_script_no_proposal_for_plain_js() -> None:
    # No *.test.js, no scripts.test -> None (a plain acceptance.js alone is not a
    # signal without a declared runner).
    got = gb._detect_acceptance_command(
        ["test/acceptance.js", "package.json"],
        read_master=_reader(b'{"name":"x"}'))
    assert got is None


# --------------------------------------------------------------------------- #
# Fix C — S4 provenance reads the delivered tree
# --------------------------------------------------------------------------- #
def test_c_authored_test_regex() -> None:
    for f in ("test/acceptance.js", "tests/foo.js", "src/a.test.ts",
              "b.spec.jsx", "acceptance.mjs", "conftest.py", "test_x.py",
              "pkg/foo_test.py"):
        assert _AUTHORED_TEST_RE.search(f), f
    for f in ("game.js", "physics.js", "index.html", "README.md", "levels.js"):
        assert not _AUTHORED_TEST_RE.search(f), f


class _WS:
    def __init__(self, files, pkg=None):
        self._files = files
        self._pkg = pkg

    def list_files(self, scope=None):
        return list(self._files)

    def read_master_file(self, rel):
        return self._pkg if rel == "package.json" else None


def test_c_tree_has_authored_test_by_file() -> None:
    assert _tree_has_authored_test(_WS(["test/acceptance.js", "game.js"]))
    assert not _tree_has_authored_test(_WS(["game.js", "index.html"]))


def test_c_tree_has_authored_test_by_declared_script() -> None:
    ws = _WS(["game.js", "index.html", "package.json"],
             pkg=b'{"scripts":{"test":"node run.js"}}')
    assert _tree_has_authored_test(ws)
    # placeholder does not count
    ws2 = _WS(["game.js", "package.json"],
              pkg=b'{"scripts":{"test":"echo \\"Error: no test specified\\""}}')
    assert not _tree_has_authored_test(ws2)


def test_c_fail_open_on_no_workspace_or_error() -> None:
    assert _tree_has_authored_test(None) is False


class _Store:
    def __init__(self, unrun=None, cmds=None):
        self._rs = {"acceptance_test_unrun": unrun} if unrun else {}
        self._cmds = dict(cmds or {})
        self.decisions: list = []

    def get_run_state(self): return dict(self._rs)
    def get_test_commands(self): return dict(self._cmds)
    def record_decision(self, **kw): self.decisions.append(kw)


def _oracle(store, ws) -> str:
    _record_completion_oracles(store, ws)
    d = [x for x in store.decisions if x["choice"] == "completion_oracles"]
    assert len(d) == 1
    return d[0]["rationale"]


def test_c_authored_but_unregistered_is_not_none_authored() -> None:
    # The run-11 case: a test file on master, but no registered acceptance command.
    r = _oracle(_Store(), _WS(["test/acceptance.js", "game.js"]))
    assert "authored but NOT registered" in r
    assert "none authored" not in r


def test_c_none_authored_when_tree_is_clean() -> None:
    r = _oracle(_Store(), _WS(["game.js", "index.html"]))
    assert "none authored" in r


def test_c_registered_reports_executed() -> None:
    r = _oracle(_Store(cmds={"acc": {"scope": "acceptance"}}),
                _WS(["test/acceptance.js"]))
    assert "executed (registered acceptance gate)" in r


def test_c_unrun_still_reported_not_executed() -> None:
    r = _oracle(_Store(unrun={"command_id": "acc", "reason": "no browser"}),
                _WS(["test/acceptance.js"]))
    assert "NOT executed" in r
