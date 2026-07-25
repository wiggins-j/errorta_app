"""Spec 13 (S2) — the foundation gate recognizes BUILDLESS web projects.

The gravity-golf run was clamped to one worker for its entire life because
``foundation_ready`` classified any ``.js`` on master as manifest-bound and then
demanded a ``package.json`` that a deliberately-buildless project (``index.html``
+ relative ``<script src>`` modules the browser resolves itself) never
legitimately produces. This suite locks the new recognition and — crucially — the
regression that a BUNDLED web app (bare-specifier imports, JSX) still requires a
manifest, so the reddit-look-a-like protection holds.

Phase 1 is a pure unit test of ``_buildless_web_ready`` over an in-memory file
list + reader (fast, table-driven). Phase 2 exercises the wiring through
``foundation_ready`` on the real-git workspace harness (so ``read_master_file`` is
hit for real), reusing the F142 pattern.
"""
from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import (
    _buildless_web_ready,
    foundation_ready,
    refresh_foundation_status,
)
from errorta_council.coding.workspace import CodingWorkspace

# --------------------------------------------------------------------------- #
# Phase 1 — the pure predicate. `read(rel) -> str | None`.
# --------------------------------------------------------------------------- #


def _reader(tree: dict[str, str]):
    return lambda rel: tree.get(rel)


_INDEX_REL_SCRIPT = {
    "index.html": (
        '<!doctype html><html><head>'
        '<link rel="stylesheet" href="style.css">'
        '</head><body>'
        '<script src="src/main.js"></script>'
        '<script src="./src/engine.js"></script>'
        "</body></html>"),
    "style.css": "body{margin:0}",
    "src/main.js": 'import {init} from "./engine.js";\ninit();',
    "src/engine.js": 'export function init(){ return 1; }',
}


def test_buildless_tree_is_ready() -> None:
    files = list(_INDEX_REL_SCRIPT)
    assert _buildless_web_ready(files, _reader(_INDEX_REL_SCRIPT)) is True


def test_bare_specifier_import_is_not_ready() -> None:
    tree = dict(_INDEX_REL_SCRIPT)
    tree["src/main.js"] = 'import React from "react";\n'
    assert _buildless_web_ready(list(tree), _reader(tree)) is False


def test_bare_side_effect_import_is_not_ready() -> None:
    tree = dict(_INDEX_REL_SCRIPT)
    tree["src/main.js"] = 'import "some-polyfill";\n'
    assert _buildless_web_ready(list(tree), _reader(tree)) is False


def test_require_call_is_not_ready() -> None:
    tree = dict(_INDEX_REL_SCRIPT)
    tree["src/main.js"] = 'const x = require("lodash");\n'
    assert _buildless_web_ready(list(tree), _reader(tree)) is False


def test_relative_and_absolute_path_imports_are_ready() -> None:
    tree = dict(_INDEX_REL_SCRIPT)
    tree["src/main.js"] = (
        'import {init} from "./engine.js";\n'
        'import {util} from "/src/engine.js";\n'
        "init(); util();")
    assert _buildless_web_ready(list(tree), _reader(tree)) is True


def test_tsx_anywhere_is_not_ready() -> None:
    tree = dict(_INDEX_REL_SCRIPT)
    tree["src/App.tsx"] = "export const App = () => <div/>;"
    assert _buildless_web_ready(list(tree), _reader(tree)) is False


def test_cdn_script_src_is_not_ready() -> None:
    tree = dict(_INDEX_REL_SCRIPT)
    tree["index.html"] = (
        '<script src="https://cdn.example.com/three.min.js"></script>'
        '<script src="src/main.js"></script>')
    assert _buildless_web_ready(list(tree), _reader(tree)) is False


def test_protocol_relative_script_src_is_not_ready() -> None:
    tree = dict(_INDEX_REL_SCRIPT)
    tree["index.html"] = '<script src="//cdn.example.com/x.js"></script>'
    assert _buildless_web_ready(list(tree), _reader(tree)) is False


def test_external_stylesheet_link_is_not_ready() -> None:
    tree = dict(_INDEX_REL_SCRIPT)
    tree["index.html"] = (
        '<link rel="stylesheet" href="https://fonts.googleapis.com/x">'
        '<script src="src/main.js"></script>')
    assert _buildless_web_ready(list(tree), _reader(tree)) is False


def test_offsite_non_stylesheet_link_is_tolerated() -> None:
    """A preconnect / icon <link> with an off-site href is not a dependency the
    browser needs to run the app — it must not disqualify."""
    tree = dict(_INDEX_REL_SCRIPT)
    tree["index.html"] = (
        '<link rel="preconnect" href="https://example.com">'
        '<link rel="icon" href="https://example.com/favicon.ico">'
        '<script src="src/main.js"></script>')
    assert _buildless_web_ready(list(tree), _reader(tree)) is True


def test_script_referencing_absent_file_is_not_ready() -> None:
    tree = {"index.html": '<script src="src/missing.js"></script>'}
    assert _buildless_web_ready(list(tree), _reader(tree)) is False


def test_index_html_with_no_script_is_not_ready() -> None:
    tree = {"index.html": "<html><body>hi</body></html>"}
    assert _buildless_web_ready(list(tree), _reader(tree)) is False


def test_no_index_html_is_not_ready() -> None:
    tree = {"app.js": 'console.log("hi")'}
    assert _buildless_web_ready(list(tree), _reader(tree)) is False


def test_unreadable_index_html_fails_closed() -> None:
    # read() returns None (binary / unreadable) for index.html.
    assert _buildless_web_ready(["index.html", "src/main.js"],
                                lambda rel: None) is False


def test_unreadable_referenced_script_fails_closed() -> None:
    tree = {"index.html": '<script src="src/main.js"></script>'}

    # index.html reads, but the referenced script does not.
    def reader(rel):
        return tree["index.html"] if rel == "index.html" else None

    assert _buildless_web_ready(["index.html", "src/main.js"], reader) is False


def test_parent_relative_script_resolves() -> None:
    tree = {
        "index.html": '<script src="../shared/boot.js"></script>',
        "shared/boot.js": "console.log(1)",
    }
    # index.html is at repo root, so ../ escapes and collapses to shared/boot.js.
    assert _buildless_web_ready(list(tree), _reader(tree)) is True


# --------------------------------------------------------------------------- #
# Phase 2 — wiring through foundation_ready on the real-git workspace harness.
# --------------------------------------------------------------------------- #


def _store(pid: str, tmp_path: Path, *, target: str = "new") -> LedgerStore:
    s = LedgerStore(pid, root=tmp_path / f"ledger-{pid}")
    s.create_project(north_star="n", definition_of_done="d", target=target,
                     repo_path=None)
    return s


def _ws(pid: str, store: LedgerStore) -> CodingWorkspace:
    ws = CodingWorkspace(pid, store)
    ws.setup(target="new", repo_path=None)
    return ws


def _merge_file(ws: CodingWorkspace, task_id: str, path: str, content: str) -> None:
    branch = ws.start_task_branch(task_id)
    ws.write_file(path, content, task_id=task_id)
    assert ws.merge_pr(branch).get("merged")


def test_buildless_index_plus_scripts_is_ready_without_manifest(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    s = _store("bl-web", tmp_path)
    ws = _ws("bl-web", s)
    _merge_file(ws, "t1", "index.html",
                '<script src="src/main.js"></script>')
    # Only index.html so far — no script file on master yet -> not a base.
    assert foundation_ready(s, ws) is False
    _merge_file(ws, "t2", "src/main.js",
                'import {go} from "./engine.js";\ngo();')
    _merge_file(ws, "t3", "src/engine.js", "export function go(){}")
    # A complete self-resolving graph, NO package.json -> foundation-ready.
    assert foundation_ready(s, ws) is True


def test_adding_a_bare_import_reclamps(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    """Self-healing: the moment the tree grows a bundler dependency it flips back
    to needing a manifest — this is what makes the relaxation safe."""
    s = _store("bl-reclamp", tmp_path)
    ws = _ws("bl-reclamp", s)
    _merge_file(ws, "t1", "index.html", '<script src="app.js"></script>')
    _merge_file(ws, "t2", "app.js", 'console.log("hi")')
    assert foundation_ready(s, ws) is True
    _merge_file(ws, "t3", "app.js", 'import React from "react";\nconsole.log(React)')
    assert foundation_ready(s, ws) is False


def test_bundled_app_without_manifest_stays_not_ready(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    """The reddit-look-a-like regression lock: a .tsx tree with no package.json
    must NOT fan out."""
    s = _store("bl-bundled", tmp_path)
    ws = _ws("bl-bundled", s)
    _merge_file(ws, "t1", "index.html", '<script src="src/index.js"></script>')
    _merge_file(ws, "t2", "src/App.tsx", "export const App = () => <div/>;")
    assert foundation_ready(s, ws) is False
    # ...and a manifest still opens it via the original path.
    _merge_file(ws, "t3", "package.json", '{"name":"x"}')
    assert foundation_ready(s, ws) is True


def test_web_plus_compiled_source_stays_manifest_bound(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    s = _store("bl-mixed", tmp_path)
    ws = _ws("bl-mixed", s)
    _merge_file(ws, "t1", "index.html", '<script src="app.js"></script>')
    _merge_file(ws, "t2", "app.js", 'console.log("hi")')
    _merge_file(ws, "t3", "server.go", "package main\nfunc main(){}")
    # compiled source present -> never buildless, needs a manifest.
    assert foundation_ready(s, ws) is False


def test_buildless_status_flips_and_lifts_the_clamp(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    """The whole point: refresh_foundation_status reads `merged` for a buildless
    tree, so the concurrency clamp lifts. On main the same tree stays `pending`."""
    from errorta_council.coding.autonomy import (
        CodingAutonomyPolicy,
        runtime_cap,
    )

    s = _store("bl-clamp", tmp_path)
    ws = _ws("bl-clamp", s)
    _merge_file(ws, "t1", "index.html", '<script src="main.js"></script>')
    _merge_file(ws, "t2", "main.js", 'console.log("go")')
    assert refresh_foundation_status(s, ws) == "merged"
    # With the foundation merged, the clamp is no longer pinned at 1.
    members = [("m1", "dev"), ("m2", "dev"), ("m3", "dev")]
    assert runtime_cap(CodingAutonomyPolicy(), members, s) >= 2


# --------------------------------------------------------------------------- #
# Phase 3 — the foundation-unlocking PR flag.
# --------------------------------------------------------------------------- #

from errorta_council.coding import runner  # noqa: E402


def _plain_store(pid: str, tmp_path: Path) -> LedgerStore:
    s = LedgerStore(pid, root=tmp_path / f"ledger-{pid}")
    s.create_project(north_star="n", definition_of_done="d", target="new",
                     repo_path=None)
    return s


def test_pr_unlocks_foundation_only_while_pending() -> None:
    class _S:
        def __init__(self, status):
            self._status = status

        def get_run_state(self):
            return {"foundation_status": self._status}

    pending = _S("pending")
    assert runner._pr_unlocks_foundation(pending, ["package.json"]) is True
    assert runner._pr_unlocks_foundation(pending, ["src/main.js"]) is True
    assert runner._pr_unlocks_foundation(pending, ["README.md"]) is False
    assert runner._pr_unlocks_foundation(pending, []) is False
    # Once merged, nothing is "unlocking" anymore.
    assert runner._pr_unlocks_foundation(_S("merged"), ["package.json"]) is False


def test_foundation_files_in_picks_manifests_and_entrypoints() -> None:
    got = runner._foundation_files_in(
        ["package.json", "src/app.js", "docs/x.md", "assets/logo.png", "main.py"])
    assert set(got) == {"package.json", "src/app.js", "main.py"}


# --------------------------------------------------------------------------- #
# Phase 4 — off-scope rejection of a foundation-unlocking PR escalates; an
# in-scope or all-pathless one does not (the escalation-storm lock).
# --------------------------------------------------------------------------- #


def _fpr(store: LedgerStore, *, unlocks: bool, changed: list[str],
         branch: str = "task-t-f") -> dict:
    task = store.add_task(title="scaffold", role="dev")
    pr = store.record_pr(task_id=task.task_id, branch=branch, head="h1",
                         dev_member="m-dev")
    store.update_pr(pr["pr_id"], unlocks_foundation=unlocks, changed_paths=changed)
    return store.get_pr(pr["pr_id"])


def _reject(store: LedgerStore, pr: dict, findings: list[dict]) -> None:
    review_task = store.add_task(title="review: scaffold", role="reviewer")
    runner._handle_review_rejection(
        store, None, pr=pr, task=review_task, findings=findings, source="reviewer")


def _n_offscope(store: LedgerStore) -> int:
    return sum(1 for d in store.list_decisions()
               if d.get("choice") == "foundation_pr_rejected_offscope")


def _pm_escalations(store: LedgerStore) -> list:
    return [t for t in store.list_tasks()
            if t.role == "pm" and str(t.title or "").startswith("foundation blocked:")]


def test_offscope_rejection_records_decision_and_one_escalation(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    s = _plain_store("os1", tmp_path)
    pr = _fpr(s, unlocks=True, changed=["package.json"])
    # A finding naming an UNRELATED file -> off-scope for the foundation.
    _reject(s, pr, [{"severity": "blocking", "title": "bad var",
                     "path": "src/ui.js", "blocking": True}])
    assert _n_offscope(s) == 1
    assert len(_pm_escalations(s)) == 1


def test_inscope_rejection_does_not_escalate(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    s = _plain_store("os2", tmp_path)
    pr = _fpr(s, unlocks=True, changed=["package.json"])
    # A finding ON the foundation file itself -> genuinely in scope.
    _reject(s, pr, [{"severity": "blocking", "title": "bad manifest",
                     "path": "package.json", "blocking": True}])
    assert _n_offscope(s) == 0
    assert _pm_escalations(s) == []


def test_all_pathless_rejection_does_not_escalate(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    """The storm lock: a rejection whose findings carry no path is scope-UNKNOWN,
    not off-scope, so it must not escalate on every ordinary rejection."""
    s = _plain_store("os3", tmp_path)
    pr = _fpr(s, unlocks=True, changed=["package.json"])
    _reject(s, pr, [{"severity": "blocking", "title": "no evidence tests ran",
                     "blocking": True}])
    assert _n_offscope(s) == 0
    assert _pm_escalations(s) == []


def test_non_unlocking_pr_never_escalates(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    s = _plain_store("os4", tmp_path)
    pr = _fpr(s, unlocks=False, changed=["src/ui.js"])
    _reject(s, pr, [{"severity": "blocking", "title": "x", "path": "src/y.js",
                     "blocking": True}])
    assert _n_offscope(s) == 0


def test_second_offscope_rejection_raises_one_deduped_alert(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    from errorta_council.coding import attention

    s = _plain_store("os5", tmp_path)
    pr = _fpr(s, unlocks=True, changed=["package.json"])
    off = [{"severity": "blocking", "title": "bad var", "path": "src/ui.js",
            "blocking": True}]
    _reject(s, pr, off)
    assert not any(sig.source == "foundation_deadlock"
                   for sig in attention.list_open("os5", store=s))
    _reject(s, pr, off)
    alerts = [sig for sig in attention.list_open("os5", store=s)
              if sig.source == "foundation_deadlock"]
    assert len(alerts) == 1
    # A third does not stack a second alert (dedup), and the PM escalation stays
    # a single task (lineage dedup).
    _reject(s, pr, off)
    alerts = [sig for sig in attention.list_open("os5", store=s)
              if sig.source == "foundation_deadlock"]
    assert len(alerts) == 1
    assert len(_pm_escalations(s)) == 1


def test_ordinary_reviewer_rejection_still_spawns_a_revise(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    """Regression: the Phase-4 escalation is ADDITIVE — the revise task still
    fires for every rejection, foundation or not."""
    s = _plain_store("os6", tmp_path)
    pr = _fpr(s, unlocks=True, changed=["package.json"])
    _reject(s, pr, [{"severity": "blocking", "title": "x", "path": "src/y.js",
                     "blocking": True}])
    assert any(t.title.startswith("revise:") for t in s.list_tasks())


# --------------------------------------------------------------------------- #
# Phase 5 — the stall rationale no longer asserts a manifest is required.
# --------------------------------------------------------------------------- #


def test_stall_rationale_mentions_the_buildless_web_foundation(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    from errorta_council.coding.autonomy import (
        CodingAutonomyPolicy,
        LoopCounters,
        _account_foundation_stall,
    )

    s = _plain_store("stall1", tmp_path)
    s.set_run_state(foundation_status="pending")
    c = LoopCounters()
    pol = CodingAutonomyPolicy(foundation_stall_limit=1)
    _account_foundation_stall(s, c, pol)
    rationale = next(d["rationale"] for d in s.list_decisions()
                     if d["choice"] == "foundation_not_converging")
    assert "index.html" in rationale
    # It must NOT flatly claim a manifest is the only foundation.
    assert "no build manifest + source entrypoint has merged" not in rationale
