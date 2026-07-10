"""F087-11 — structured diff parsing + evidence-gated merge-back tests."""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding.diff_review import (
    FileDiff,
    Hunk,
    MergeBlocker,
    MergeGate,
    evaluate_merge_gate,
    parse_unified_diff,
)

FIX = Path(__file__).parent / "fixtures" / "diff_review"


# --- parse_unified_diff -----------------------------------------------------

_MULTI = """\
diff --git a/added.py b/added.py
new file mode 100644
index 0000000..e69de29
--- /dev/null
+++ b/added.py
@@ -0,0 +1,3 @@
+def hello():
+    return 1
+
diff --git a/mod.py b/mod.py
index 1111111..2222222 100644
--- a/mod.py
+++ b/mod.py
@@ -1,4 +1,4 @@
 import os
-x = 1
+x = 2
 y = 3
diff --git a/gone.py b/gone.py
deleted file mode 100644
index 3333333..0000000
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-obsolete = True
-also = False
diff --git a/old_name.py b/new_name.py
similarity index 90%
rename from old_name.py
rename to new_name.py
index 4444444..5555555 100644
--- a/old_name.py
+++ b/new_name.py
@@ -1,3 +1,3 @@
 keep = 1
-renamed_change = 0
+renamed_change = 1
 tail = 2
"""


def test_empty_input_returns_empty():
    assert parse_unified_diff("") == []
    assert parse_unified_diff("   \n  \n") == []


def test_multi_file_change_types_and_paths():
    files = parse_unified_diff(_MULTI)
    by_path = {f.path: f for f in files}
    assert set(by_path) == {"added.py", "mod.py", "gone.py", "new_name.py"}
    assert by_path["added.py"].change_type == "added"
    assert by_path["mod.py"].change_type == "modified"
    assert by_path["gone.py"].change_type == "deleted"
    assert by_path["new_name.py"].change_type == "renamed"
    assert by_path["new_name.py"].old_path == "old_name.py"


def test_added_file_counts_only_additions():
    f = {d.path: d for d in parse_unified_diff(_MULTI)}["added.py"]
    assert f.added_lines == 3
    assert f.removed_lines == 0


def test_modified_file_counts_both():
    f = {d.path: d for d in parse_unified_diff(_MULTI)}["mod.py"]
    assert f.added_lines == 1
    assert f.removed_lines == 1


def test_deleted_file_counts_only_removals():
    f = {d.path: d for d in parse_unified_diff(_MULTI)}["gone.py"]
    assert f.added_lines == 0
    assert f.removed_lines == 2
    assert f.old_path is None


def test_rename_with_edits_keeps_old_path_and_counts():
    f = {d.path: d for d in parse_unified_diff(_MULTI)}["new_name.py"]
    assert f.old_path == "old_name.py"
    assert f.added_lines == 1
    assert f.removed_lines == 1


def test_hunks_are_captured():
    f = {d.path: d for d in parse_unified_diff(_MULTI)}["mod.py"]
    assert len(f.hunks) == 1
    assert isinstance(f.hunks[0], Hunk)
    assert f.hunks[0].header.startswith("@@")
    # header line excluded from +/- counting but body lines retained
    assert any(line.startswith("+x = 2") for line in f.hunks[0].lines)


def test_header_lines_not_counted_as_changes():
    # the +++ / --- header lines must never inflate the +/- counts
    f = {d.path: d for d in parse_unified_diff(_MULTI)}["added.py"]
    assert f.added_lines == 3  # not 4 (the +++ b/added.py is excluded)


# --- evaluate_merge_gate ----------------------------------------------------

_CLEAR_TASKS = [
    {"taskId": "t1", "state": "done"},
    {"taskId": "t2", "state": "dropped"},
]


def _gate(**over):
    base = dict(
        tasks=list(_CLEAR_TASKS),
        reviewed_approved=True,
        tests_passed=True,
        conflicts=[],
        definition_of_done_met=True,
    )
    base.update(over)
    return evaluate_merge_gate(**base)


def _codes(gate: MergeGate) -> set[str]:
    return {b.code for b in gate.blockers}


def test_all_clear_allows():
    g = _gate()
    assert g.allowed is True
    assert g.blockers == []
    assert g.allow_override is True


def test_override_always_true_even_when_allowed():
    assert _gate().allow_override is True
    assert _gate(tests_passed=False).allow_override is True


def test_open_task_blocks():
    g = _gate(tasks=[{"taskId": "t", "state": "todo"}])
    assert g.allowed is False
    assert "open_tasks" in _codes(g)


def test_blocked_task_is_open_blockers_not_open_tasks():
    g = _gate(tasks=[{"taskId": "t", "state": "blocked"}])
    assert "open_blockers" in _codes(g)
    assert "open_tasks" not in _codes(g)


def test_unreviewed_blocks():
    g = _gate(reviewed_approved=None)
    assert "unreviewed_changes" in _codes(g)
    assert "review_rejected" not in _codes(g)


def test_review_rejected_blocks():
    g = _gate(reviewed_approved=False)
    assert "review_rejected" in _codes(g)
    assert "unreviewed_changes" not in _codes(g)


def test_tests_missing_blocks():
    g = _gate(tests_passed=None)
    assert "tests_missing" in _codes(g)
    assert "tests_failing" not in _codes(g)


def test_tests_failing_blocks():
    g = _gate(tests_passed=False)
    assert "tests_failing" in _codes(g)
    assert "tests_missing" not in _codes(g)


def test_tests_missing_vacuous_when_not_required():
    # F146 Slice D: no registered tests AND no runnable runtime -> the tests gate
    # is vacuously satisfied; a missing verdict must NOT block forever.
    g = _gate(tests_passed=None, tests_required=False)
    assert "tests_missing" not in _codes(g)
    assert g.allowed is True


def test_tests_missing_still_blocks_when_required():
    # Default (tests OR a runtime exist): a missing verdict still blocks.
    assert "tests_missing" in _codes(_gate(tests_passed=None, tests_required=True))


def test_tests_failing_blocks_even_when_not_required():
    # A suite that RAN and failed always blocks — vacuousness only covers the
    # never-ran (None) case, never a real failure.
    g = _gate(tests_passed=False, tests_required=False)
    assert "tests_failing" in _codes(g)


def test_file_conflicts_block_and_list_paths():
    g = _gate(conflicts=["src/a.py", "src/b.py"])
    assert "file_conflicts" in _codes(g)
    detail = next(b.detail for b in g.blockers if b.code == "file_conflicts")
    assert "src/a.py" in detail and "src/b.py" in detail


def test_definition_of_done_not_met_blocks():
    g = _gate(definition_of_done_met=False)
    assert "definition_of_done" in _codes(g)


def test_blockers_are_independent():
    g = _gate(
        tasks=[{"taskId": "t", "state": "doing"}],
        reviewed_approved=False,
        tests_passed=False,
        conflicts=["x"],
        definition_of_done_met=False,
    )
    assert _codes(g) == {
        "open_tasks",
        "review_rejected",
        "tests_failing",
        "file_conflicts",
        "definition_of_done",
    }
    assert g.allowed is False


def test_merge_blocker_is_frozen():
    b = MergeBlocker(code="x", detail="y")
    try:
        b.code = "z"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("MergeBlocker should be frozen")


# --- golden diff fixtures ---------------------------------------------------


def test_golden_diffs():
    cases = {
        "added.diff": "added",
        "modified.diff": "modified",
        "deleted.diff": "deleted",
        "renamed.diff": "renamed",
    }
    for name, change_type in cases.items():
        files = parse_unified_diff((FIX / name).read_text())
        assert len(files) == 1, f"{name} should be one file"
        assert files[0].change_type == change_type, name
        assert isinstance(files[0], FileDiff)

    multi = parse_unified_diff((FIX / "multi.diff").read_text())
    assert {f.change_type for f in multi} == {
        "added",
        "modified",
        "deleted",
        "renamed",
    }
