"""F140 — a dev code_write can never DELETE an existing file.

Live failure this locks against: a dev turn emitted ``code_write`` whose
``content`` was a placeholder sentinel (``PRESERVE_CURRENT_FILE_AND_APPLY``)
instead of the real body, replacing a ~2000-line module with a stub — the whole
file and its shared API surface deleted in one write, only caught later by a
reviewer. The write must be BLOCKED before it lands, and the turn must be
unproductive so the F136/F127 escalate-up ladder engages instead of a PR that
deletes the codebase.
"""
from pathlib import Path

import pytest

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.schemas import TurnErrorCode
from errorta_council.coding.turn_controller import CodingTurnController
from errorta_council.coding.workspace import CodingWorkspace, CodingWorkspaceError
from errorta_council.coding.write_guard import BLOCKED_REASON, classify_destructive_write

# A codebase-scale module (>= 40 non-blank lines): stands in for the 2000-line
# game.py. The collapse rules only apply to files this large.
_BIG_FILE = "\n".join(f"def f{i}(x):\n    return x + {i}" for i in range(40)) + "\n"

# A substantial-but-moderate file (>= 8 lines, < 40) — protected against
# placeholder/blank writes but NOT against a legitimate shrink-to-stub refactor.
_MODERATE = (
    '"""game module."""\n'
    "class Battle:\n"
    "    def __init__(self, a, b):\n"
    "        self.a, self.b = a, b\n"
    "    def turn_order(self):\n"
    "        return sorted([self.a, self.b], key=lambda c: -c.speed)\n"
    "def run():\n"
    "    return Battle(1, 2).turn_order()\n"
)


# --- unit: the pure classifier ---------------------------------------------- #


def test_new_or_trivial_old_file_is_never_destructive() -> None:
    # An empty/absent/trivial old file can't be "destroyed" even by sentinel text.
    assert classify_destructive_write("", "anything") is None
    assert classify_destructive_write("   \n", "PRESERVE_CURRENT_FILE") is None
    assert classify_destructive_write("x = 1\n", "PRESERVE_CURRENT_FILE_AND_APPLY") is None


def test_exact_live_sentinel_is_blocked() -> None:
    assert classify_destructive_write(
        _BIG_FILE, "PRESERVE_CURRENT_FILE_AND_APPLY stub") == "placeholder"
    # A sentinel is destruction over any substantial file, not only huge ones.
    assert classify_destructive_write(_MODERATE, "PRESERVE_CURRENT_FILE") == "placeholder"


@pytest.mark.parametrize(
    "stub",
    [
        "# ... existing code ...",
        "// keep existing code",
        "<unchanged>",
        "rest of the file unchanged",
        "leave this file unchanged",
        "PRESERVE_EXISTING",
        "do not modify this file",
    ],
)
def test_placeholder_markers_are_blocked(stub: str) -> None:
    assert classify_destructive_write(_BIG_FILE, stub) == "placeholder"


def test_padded_sentinel_is_still_caught() -> None:
    # A marker wrapped in a few comment lines must not slip past the placeholder
    # size gate (>5 lines / >600 bytes was the evasion).
    padded = (
        "# NOTE: applying the requested change below\n"
        "# (the rest of the file is large so summarizing)\n"
        "# preserve_current_file\n"
        "# ... remainder omitted for brevity ...\n"
        "# end\n"
        "# regards\n"
    )
    assert classify_destructive_write(_BIG_FILE, padded) == "placeholder"


def test_large_file_emptied_or_collapsed_is_blocked() -> None:
    assert classify_destructive_write(_BIG_FILE, "") == "emptied"
    assert classify_destructive_write(_BIG_FILE, "   \n\n") == "emptied"
    # <=5 non-blank lines and a small fraction → truncation.
    assert classify_destructive_write(_BIG_FILE, "raise NotImplementedError\n") == (
        "truncation"
    )
    # Hollow multi-line rewrite (>5 lines) but a tiny fraction of the bytes → gutted.
    hollow = "".join(f"g{i}=None\n" for i in range(8))
    assert classify_destructive_write(_BIG_FILE, hollow) == "gutted"


def test_ordinary_and_large_but_real_edits_are_allowed() -> None:
    # A real change of comparable size is fine.
    edited = _BIG_FILE.replace("return x + 0", "return x + 0  # base case")
    assert classify_destructive_write(_BIG_FILE, edited) is None
    # A large deletion that still leaves plenty of real code (well above a stub).
    trimmed = "\n".join(_BIG_FILE.splitlines()[:24]) + "\n"
    assert classify_destructive_write(_BIG_FILE, trimmed) is None


def test_moderate_file_shrunk_to_stub_is_allowed() -> None:
    # The collapse rules apply only to LARGE files; reducing a moderate module to
    # a small stub is a legitimate refactor left to the reviewer, not blocked.
    assert classify_destructive_write(_MODERATE, "from .impl import Battle, run\n") is None


def test_identifier_substring_is_not_a_sentinel() -> None:
    # Regression: "and_apply" / "code unchanged" must NOT be markers — an ordinary
    # short module over a moderate file is a normal edit, not destruction.
    real = "def load_and_apply_config(cfg):\n    return cfg.apply()\n"
    assert classify_destructive_write(_MODERATE, real) is None


# --- integration: through the workspace + dev-turn controller --------------- #


def _new_ws(project_id: str, tmp_path: Path) -> tuple[LedgerStore, CodingWorkspace]:
    store = LedgerStore(project_id, root=tmp_path / f"ledger-{project_id}")
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    ws = CodingWorkspace(project_id, store)
    ws.setup(target="new", repo_path=None)
    return store, ws


def _seed_master(ws: CodingWorkspace, path: str, content: str) -> None:
    branch = ws.start_task_branch("seed")
    ws.write_file(path, content, task_id="seed")
    assert ws.merge_pr(branch).get("merged")


def test_write_file_blocks_placeholder_over_existing(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    _store, ws = _new_ws("f140a", tmp_path)
    _seed_master(ws, "game.py", _BIG_FILE)
    ws.start_task_branch("t1")
    with pytest.raises(CodingWorkspaceError) as exc:
        ws.write_file("game.py", "PRESERVE_CURRENT_FILE_AND_APPLY stub", task_id="t1")
    assert str(exc.value) == BLOCKED_REASON
    # The original file is intact — nothing was destroyed.
    assert ws.read_master_file("game.py") == _BIG_FILE.encode("utf-8")


def test_write_file_still_allows_new_file_and_real_edit(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    _store, ws = _new_ws("f140b", tmp_path)
    ws.start_task_branch("t1")
    ws.write_file("game.py", _BIG_FILE, task_id="t1")            # new file: allowed
    head = ws.write_file("game.py", _BIG_FILE + "\n# extra\n", task_id="t1")
    assert head                                                 # real edit: allowed


def test_write_file_guards_non_utf8_existing_file(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    # A strict utf-8 read would have skipped the guard on a non-utf-8 file; the
    # lenient decode keeps it protected. Plant raw latin-1 bytes on disk in the
    # task worktree, then attempt to blank the file.
    _store, ws = _new_ws("f140d", tmp_path)
    ws.start_task_branch("t1")
    target = ws.task_root("t1") / "legacy.py"
    target.write_bytes(("# café résumé\n" + _BIG_FILE).encode("latin-1"))
    with pytest.raises(CodingWorkspaceError) as exc:
        ws.write_file("legacy.py", "", task_id="t1")
    assert str(exc.value) == BLOCKED_REASON


def test_dev_turn_reports_destructive_write_as_unproductive_failure(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    store, ws = _new_ws("f140c", tmp_path)
    _seed_master(ws, "game.py", _BIG_FILE)
    controller = CodingTurnController(store, ws)
    task = store.add_task(title="edit turn order", role="dev")
    ws.start_task_branch(task.task_id)
    data = {
        "task_type": "implementation",
        "tool_calls": [
            {"tool": "code_write",
             "args": {"path": "game.py",
                      "content": "PRESERVE_CURRENT_FILE_AND_APPLY stub"}},
        ],
    }
    summary = controller.execute_dev_turn(
        task=task, member={"id": "m-dev"}, data=data)
    assert summary.success_count == 0, "a destructive write must not count as success"
    assert summary.failures == [
        ("game.py", TurnErrorCode.destructive_write_blocked.value)
    ]
    # The file was never overwritten — its real content survives on master.
    assert ws.read_master_file("game.py") == _BIG_FILE.encode("utf-8")
