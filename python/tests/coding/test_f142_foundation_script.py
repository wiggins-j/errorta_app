"""F142 Slice 2 (WS-B) — foundation gate recognizes manifest-less script deliverables.

Acceptance criterion 3: a `new` script-style project (a root-level ``game.py`` with
no build manifest, e.g. the pokemon single-file North Star) reaches
``foundation_ready`` on its entrypoint alone — lifting the clamp and stopping the
false ``foundation_not_converging`` alert — WITHOUT weakening the gate for
manifest-load-bearing (node/web/compiled) ecosystems.

Reuses the F139 real-git workspace harness (LedgerStore + CodingWorkspace + merge_pr).
"""
from pathlib import Path

from errorta_council.coding.autonomy import (
    CodingAutonomyPolicy,
    LoopCounters,
    _account_foundation_stall,
    foundation_pending,
)
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import (
    foundation_ready,
    refresh_foundation_status,
)
from errorta_council.coding.workspace import CodingWorkspace


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


def _n_foundation_alerts(store: LedgerStore) -> int:
    return sum(1 for d in store.list_decisions()
               if d["choice"] == "foundation_not_converging")


# --- AC3: script-style single-file project is foundation-ready without a manifest


def test_script_only_game_py_is_foundation_ready(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    s = _store("fs-py", tmp_path)
    ws = _ws("fs-py", s)
    assert foundation_ready(s, ws) is False           # empty master
    _merge_file(ws, "t1", "game.py", "print('poke')\n")
    assert foundation_ready(s, ws) is True             # entrypoint alone is enough


def test_script_foundation_status_flips_off_pending_and_never_false_alerts(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    s = _store("fs-status", tmp_path)
    ws = _ws("fs-status", s)
    # empty master -> pending.
    assert refresh_foundation_status(s, ws) == "pending"
    assert foundation_pending(s) is True
    # the single-file game.py merges -> foundation now merged (NOT pending).
    _merge_file(ws, "t1", "game.py", "print('poke')\n")
    assert refresh_foundation_status(s, ws) == "merged"
    assert s.get_run_state()["foundation_status"] != "pending"
    assert foundation_pending(s) is False
    # and no foundation_not_converging alert can fire while merged.
    c = LoopCounters()
    policy = CodingAutonomyPolicy(foundation_stall_limit=1)
    for _ in range(5):
        _account_foundation_stall(s, c, policy)
    assert _n_foundation_alerts(s) == 0


# --- Review findings A3/A4/B3: non-source files and subdirs must NOT re-clamp a
#     script project (the earlier flat/<=3-total-files predicate wrongly did).


def test_script_with_assets_subdir_is_still_ready(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    """A pygame game with an assets/ subdir: a nested NON-source file must not
    disqualify the script-style foundation (finding A3 — the old `flat` check over
    all files clamped this common shape forever)."""
    s = _store("fs-assets", tmp_path)
    ws = _ws("fs-assets", s)
    _merge_file(ws, "t1", "game.py", "print('poke')\n")
    _merge_file(ws, "t2", "assets/sprite.txt", "placeholder\n")
    assert foundation_ready(s, ws) is True


def test_script_with_docs_files_over_old_cap_is_still_ready(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    """game.py + README + LICENSE + CHANGELOG = 4 flat files: ordinary repo-hygiene
    files must not push a single-source-file project past a count cap (finding A4 —
    the old `len(files) <= 3` over TOTAL files clamped this)."""
    s = _store("fs-docs", tmp_path)
    ws = _ws("fs-docs", s)
    _merge_file(ws, "t1", "game.py", "print('poke')\n")
    _merge_file(ws, "t2", "README.md", "# game\n")
    _merge_file(ws, "t3", "LICENSE", "MIT\n")
    _merge_file(ws, "t4", "CHANGELOG.md", "## 0.1\n")
    assert foundation_ready(s, ws) is True


def test_nested_pure_python_package_is_ready_on_entrypoint(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    """A pure-Python project split across a module dir (no node/web/compiled
    source, no manifest) is script-style and foundation-ready on its entrypoint —
    directory nesting is irrelevant (finding B3 — dropping the fragile flat/count
    cap in favor of the ecosystem rule)."""
    s = _store("fs-nested", tmp_path)
    ws = _ws("fs-nested", s)
    _merge_file(ws, "t1", "main.py", "from pkg.core import run\nrun()\n")
    _merge_file(ws, "t2", "pkg/core.py", "def run():\n    pass\n")
    assert foundation_ready(s, ws) is True


# --- AC3: manifest-load-bearing ecosystem protection is unchanged


def test_node_index_ts_without_package_json_is_not_ready(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    s = _store("fs-node", tmp_path)
    ws = _ws("fs-node", s)
    _merge_file(ws, "t1", "index.ts", "export const x = 1\n")
    # node/web source present but no manifest -> still clamped.
    assert foundation_ready(s, ws) is False
    # adding the manifest opens the gate (the original path still works).
    _merge_file(ws, "t2", "package.json", '{"name": "x"}\n')
    assert foundation_ready(s, ws) is True


def test_python_with_requirements_and_game_py_is_ready(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    """The manifest path still works for a Python project that ships one."""
    s = _store("fs-reqs", tmp_path)
    ws = _ws("fs-reqs", s)
    _merge_file(ws, "t1", "requirements.txt", "pygame\n")
    _merge_file(ws, "t2", "game.py", "import pygame\n")
    assert foundation_ready(s, ws) is True


# --- AC3: fail-closed + early returns preserved


def test_unreadable_git_fails_closed(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    s = _store("fs-fail", tmp_path)

    class _Boom:
        def list_files(self, *, scope: str) -> list[str]:
            raise RuntimeError("git unreadable")

    assert foundation_ready(s, _Boom()) is False


def test_existing_target_is_always_ready(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    s = _store("fs-exist", tmp_path, target="existing")
    # empty tree, but an existing-target project imports a real repo (the store's
    # target drives the early return; the workspace itself can seed empty).
    ws = _ws("fs-exist", s)
    assert foundation_ready(s, ws) is True


def test_workspace_none_is_ready(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    s = _store("fs-none", tmp_path)
    assert foundation_ready(s, None) is True
