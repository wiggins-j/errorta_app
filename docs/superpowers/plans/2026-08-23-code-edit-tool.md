# code_edit Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Coding Mode devs a `code_edit` tool (anchored find/replace, Claude Code Edit semantics) so a fix to a large file costs output proportional to the change, not the file.

**Architecture:** New pure module `edit_apply.py` (mirrors `write_guard.py`: dependency-free splice + typed errors) → `CodingWorkspace.edit_file` (path safety, missing/binary checks, delegates final content to the existing `write_file` so the F140 destructive-write guard, F139 no-op suppression, and provenance are shared) → `turn_controller` dispatch (`_ROLE_TOOLS[DEV]` gains `code_edit`) → dev prompt teaches the vocabulary.

**Tech Stack:** Python 3.11+, pydantic v2, pytest. All paths below are relative to `python/` in the repo (`python/`). Run tests from that directory.

**Spec:** `docs/superpowers/specs/2026-08-23-code-edit-tool-design.md`

## Global Constraints

- PUBLIC repo: no secrets, tokens, or PII in code, tests, or commits.
- Failure-reason strings MUST start with their stable code (`edit_no_match: ...`) — tests and the carry-forward prompt hint match on prefix.
- Matching is EXACT (`str` operations on the lenient UTF-8 decode). No regex, no whitespace normalization, no line numbers.
- `code_edit` never creates files and is text-only (no base64 arm).
- Scope is `errorta_council/coding/` only — do NOT touch `errorta_tools/catalog.py`, `errorta_council/schema.py`, or other legacy council tool surfaces.
- Comment style: comments state constraints/why, not narration; match the module's existing density.

---

### Task 1: `edit_apply.py` — pure splice + typed errors

**Files:**
- Create: `errorta_council/coding/edit_apply.py`
- Test (create): `tests/coding/test_edit_apply.py`

**Interfaces:**
- Consumes: nothing (dependency-free by design, like `write_guard.py`).
- Produces: `apply_code_edit(old_content: str, old_string: str, new_string: str, *, replace_all: bool = False) -> str` returning the new full content; `EditApplyError(ValueError)` with `.code: str` and `.detail: str`, `str(exc) == f"{code}: {detail}"`; module constants `EDIT_EMPTY_OLD_STRING = "edit_empty_old_string"`, `EDIT_NO_CHANGE = "edit_no_change"`, `EDIT_NO_MATCH = "edit_no_match"`, `EDIT_NOT_UNIQUE = "edit_not_unique"`. Task 2 imports all of these.

- [ ] **Step 1: Write the failing tests**

Create `tests/coding/test_edit_apply.py`:

```python
"""code_edit spec — the pure anchored-splice: exact-match semantics and the
typed failure taxonomy (each code is a stable prefix of the raised message)."""
import pytest

from errorta_council.coding.edit_apply import (
    EDIT_EMPTY_OLD_STRING,
    EDIT_NO_CHANGE,
    EDIT_NO_MATCH,
    EDIT_NOT_UNIQUE,
    EditApplyError,
    apply_code_edit,
)


def test_unique_match_is_spliced() -> None:
    out = apply_code_edit("def f():\n    return 1\n", "return 1", "return 2")
    assert out == "def f():\n    return 2\n"


def test_only_the_single_occurrence_changes() -> None:
    # Surrounding content is untouched byte-for-byte.
    old = "a = 1\nb = 2\nc = 3\n"
    assert apply_code_edit(old, "b = 2", "b = 20") == "a = 1\nb = 20\nc = 3\n"


def test_replace_all_replaces_every_occurrence() -> None:
    out = apply_code_edit("x, x, x", "x", "y", replace_all=True)
    assert out == "y, y, y"


def test_replace_all_with_single_match_is_fine() -> None:
    assert apply_code_edit("only once", "once", "twice", replace_all=True) \
        == "only twice"


def test_empty_old_string_is_rejected() -> None:
    with pytest.raises(EditApplyError) as exc:
        apply_code_edit("body", "", "new")
    assert exc.value.code == EDIT_EMPTY_OLD_STRING
    assert str(exc.value).startswith("edit_empty_old_string: ")


def test_no_change_is_rejected() -> None:
    with pytest.raises(EditApplyError) as exc:
        apply_code_edit("body", "body", "body")
    assert exc.value.code == EDIT_NO_CHANGE
    assert str(exc.value).startswith("edit_no_change: ")


def test_no_match_is_rejected_and_names_exactness() -> None:
    with pytest.raises(EditApplyError) as exc:
        apply_code_edit("def f():\n    return 1\n", "return  1", "return 2")
    assert exc.value.code == EDIT_NO_MATCH
    # The detail must remind the model that matching is exact — the whitespace
    # fumble above is the expected live failure mode.
    assert "exact" in exc.value.detail


def test_not_unique_is_rejected_with_count_and_recovery() -> None:
    with pytest.raises(EditApplyError) as exc:
        apply_code_edit("x = 1\nx = 1\nx = 1\n", "x = 1", "x = 2")
    assert exc.value.code == EDIT_NOT_UNIQUE
    assert "3" in exc.value.detail
    assert "replace_all" in exc.value.detail


def test_matching_is_whitespace_sensitive() -> None:
    with pytest.raises(EditApplyError) as exc:
        apply_code_edit("    indented\n", "indented \n", "x\n")
    assert exc.value.code == EDIT_NO_MATCH


def test_count_uses_non_overlapping_semantics() -> None:
    # "aaa" contains "aa" twice overlapping but ONCE non-overlapping — must
    # match str.replace's behaviour, which is what actually splices.
    assert apply_code_edit("aaa", "aa", "b") == "ba"


def test_unicode_content_round_trips() -> None:
    out = apply_code_edit("π = 3.14 # τ?\n", "3.14", "3.14159")
    assert out == "π = 3.14159 # τ?\n"


def test_whitespace_only_old_string_is_allowed_but_must_be_unique() -> None:
    # Only the EMPTY string is invalid per se; a whitespace anchor is legal and
    # subject to the same uniqueness rule as any other.
    assert apply_code_edit("a\t\tb", "\t\t", " ") == "a b"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd python/ && python -m pytest tests/coding/test_edit_apply.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'errorta_council.coding.edit_apply'`

- [ ] **Step 3: Write the implementation**

Create `errorta_council/coding/edit_apply.py`:

```python
"""code_edit — the pure anchored find/replace a dev turn splices a file with.

Why this tool exists (live incident, senditai-ng task t-e75a9c4d5a2e): the only
dev write tool was ``code_write``, which re-emits the WHOLE file — so a correct
fix to a 1600-line module collapsed under the turn's output budget and the F140
guard (rightly) blocked the truncated rewrite. Any fix to a large file was
structurally impossible. ``code_edit`` makes the write proportional to the
CHANGE: the model quotes the exact text to replace and its replacement.

Semantics are Claude Code's Edit tool, which strong models already know:
``old_string`` is matched EXACTLY (no regex, no whitespace normalization, no
line numbers — models fumble line arithmetic but quote text reliably) and must
occur exactly once unless ``replace_all``. Occurrence counting is
non-overlapping (``str.count``), matching the ``str.replace`` that splices.

Failures raise :class:`EditApplyError` whose message starts with a stable code
(``edit_no_match: ...``) — the tool-event / carry-forward channel matches on
that prefix. Pure/dependency-free so it is unit-testable without git or a
workspace (the ``write_guard`` idiom).
"""
from __future__ import annotations

EDIT_EMPTY_OLD_STRING = "edit_empty_old_string"
EDIT_NO_CHANGE = "edit_no_change"
EDIT_NO_MATCH = "edit_no_match"
EDIT_NOT_UNIQUE = "edit_not_unique"


class EditApplyError(ValueError):
    """A ``code_edit`` that cannot be applied; ``str()`` is ``"{code}: {detail}"``."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def apply_code_edit(old_content: str, old_string: str, new_string: str, *,
                    replace_all: bool = False) -> str:
    """Return ``old_content`` with ``old_string`` replaced by ``new_string``.

    Exactly-once matching unless ``replace_all`` (which accepts any count >= 1).
    Raises :class:`EditApplyError` — never returns partial/unchanged content.
    """
    if old_string == "":
        raise EditApplyError(
            EDIT_EMPTY_OLD_STRING,
            "old_string must be non-empty — quote the exact text to replace "
            "(code_edit cannot create a file; use code_write for that)")
    if old_string == new_string:
        raise EditApplyError(
            EDIT_NO_CHANGE, "old_string and new_string are identical")
    count = old_content.count(old_string)
    if count == 0:
        raise EditApplyError(
            EDIT_NO_MATCH,
            "old_string was not found in the file — matching is exact "
            "(whitespace and indentation included); copy the text verbatim "
            "from the current file contents")
    if count > 1 and not replace_all:
        raise EditApplyError(
            EDIT_NOT_UNIQUE,
            f"old_string matches {count} times — enlarge the anchor with "
            "surrounding lines so it is unique, or set \"replace_all\": true "
            "to change every occurrence")
    return old_content.replace(old_string, new_string)


__all__ = [
    "EDIT_EMPTY_OLD_STRING", "EDIT_NO_CHANGE", "EDIT_NO_MATCH",
    "EDIT_NOT_UNIQUE", "EditApplyError", "apply_code_edit",
]
```

Note `replace_all` path and single path both use plain `replace` — with `count == 1` they are identical, so one call site serves both (no `replace(..., 1)` needed once count is verified).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/coding/test_edit_apply.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add python/errorta_council/coding/edit_apply.py python/tests/coding/test_edit_apply.py
git commit -m "feat(coding): edit_apply — pure anchored find/replace for code_edit"
```

---

### Task 2: `CodingWorkspace.edit_file` — worktree edit with guard parity

**Files:**
- Modify: `errorta_council/coding/workspace.py` (add `edit_file` directly after `write_file`, which ends ~line 329)
- Test (extend): `tests/coding/test_coding_workspace.py`

**Interfaces:**
- Consumes: `apply_code_edit` / `EditApplyError` from Task 1; existing `CodingWorkspace.write_file(rel_path, content, *, task_id, summary="") -> str` (returns HEAD sha, runs the F140 guard + F139 no-op suppression + provenance); `resolve_workspace_path` from `errorta_tools.runner.apply_workspace`.
- Produces: `CodingWorkspace.edit_file(rel_path: str, old_string: str, new_string: str, *, replace_all: bool = False, task_id: str, summary: str = "") -> str` (new HEAD sha). Raises `CodingWorkspaceError` whose message starts with `edit_target_missing`, `edit_target_binary`, one of Task 1's codes, or `destructive_write_blocked` (via the delegated `write_file`). Task 3 calls this.

- [ ] **Step 1: Write the failing tests**

Append to `tests/coding/test_coding_workspace.py` (match its existing fixture style — check the top of the file for how a workspace is constructed; the snippets below use the same `_store`/`CodingWorkspace(project_id, store)` + `ws.setup(target="new", repo_path=None)` pattern as `tests/coding/test_turn_controller.py`; adapt names to the file's local helpers if they differ):

```python
# --- code_edit: CodingWorkspace.edit_file ----------------------------------

def _edit_ws(tmp_errorta_home, project_id):
    store = LedgerStore(project_id)
    store.create_project(north_star="n", definition_of_done="d", target="new",
                         repo_path=None)
    ws = CodingWorkspace(project_id, store)
    ws.setup(target="new", repo_path=None)
    return store, ws


def test_edit_file_splices_commits_and_records_provenance(tmp_errorta_home) -> None:
    store, ws = _edit_ws(tmp_errorta_home, "editok")
    ws.write_file("app.py", "def f():\n    return 1\n", task_id="t-1")
    head = ws.edit_file("app.py", "return 1", "return 2", task_id="t-1")
    assert head
    assert (ws.root() / "app.py").read_text("utf-8") == "def f():\n    return 2\n"
    art = {a["path"]: a for a in store.list_artifacts()}
    assert art["app.py"]["status"] == "modified"


def test_edit_file_missing_target_is_typed(tmp_errorta_home) -> None:
    _, ws = _edit_ws(tmp_errorta_home, "editmiss")
    with pytest.raises(CodingWorkspaceError) as exc:
        ws.edit_file("nope.py", "a", "b", task_id="t-1")
    assert str(exc.value).startswith("edit_target_missing: ")
    assert "code_write" in str(exc.value)  # names the recovery tool


def test_edit_file_binary_target_is_typed(tmp_errorta_home) -> None:
    _, ws = _edit_ws(tmp_errorta_home, "editbin")
    ws.write_file("sprite.png", b"\x89PNG\x00\x1a", task_id="t-1")
    with pytest.raises(CodingWorkspaceError) as exc:
        ws.edit_file("sprite.png", "PNG", "JPG", task_id="t-1")
    assert str(exc.value).startswith("edit_target_binary: ")


def test_edit_file_traversal_is_rejected(tmp_errorta_home) -> None:
    _, ws = _edit_ws(tmp_errorta_home, "edittrav")
    with pytest.raises(Exception):
        ws.edit_file("../escape.py", "a", "b", task_id="t-1")


def test_edit_file_no_match_propagates_typed_code(tmp_errorta_home) -> None:
    _, ws = _edit_ws(tmp_errorta_home, "editnom")
    ws.write_file("app.py", "x = 1\n", task_id="t-1")
    with pytest.raises(CodingWorkspaceError) as exc:
        ws.edit_file("app.py", "y = 2", "y = 3", task_id="t-1")
    assert str(exc.value).startswith("edit_no_match: ")


def test_edit_that_guts_a_large_file_hits_the_f140_guard(tmp_errorta_home) -> None:
    # The spec's required guard interaction: the spliced result is a full
    # old->new pair, so classify_destructive_write applies to code_edit exactly
    # as to code_write — deleting a huge unique block from a large file blocks.
    _, ws = _edit_ws(tmp_errorta_home, "editgut")
    body = "def start():\n    pass\n" + "\n".join(
        f"def f{i}():\n    return {i}" for i in range(200))
    ws.write_file("big.py", body, task_id="t-1")
    huge_block = "\n".join(f"def f{i}():\n    return {i}" for i in range(200))
    with pytest.raises(CodingWorkspaceError) as exc:
        ws.edit_file("big.py", huge_block, "# gone", task_id="t-1")
    assert "destructive_write_blocked" in str(exc.value)
```

Add any missing imports at the top of the test file (`pytest`, `CodingWorkspaceError`) if not already imported.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/coding/test_coding_workspace.py -q -k edit_file or edit_that`
Expected: FAIL with `AttributeError: 'CodingWorkspace' object has no attribute 'edit_file'`

- [ ] **Step 3: Write the implementation**

In `errorta_council/coding/workspace.py`, add directly after `write_file` (after its `return head`, before `changed_paths`):

```python
    def edit_file(self, rel_path: str, old_string: str, new_string: str, *,
                  replace_all: bool = False, task_id: str,
                  summary: str = "") -> str:
        """Apply an anchored find/replace (``code_edit``) to an existing text
        file in the worktree and commit via :meth:`write_file`. Returns the new
        HEAD sha.

        Delegating the spliced FULL content to ``write_file`` is the point: the
        F140 destructive-write guard, the F139 no-op-commit suppression, and
        provenance recording all apply to an edit exactly as to a whole-file
        write — an edit that guts a large file is still blocked. Raises
        :class:`CodingWorkspaceError` whose message starts with a stable code
        (``edit_target_missing`` / ``edit_target_binary`` / the
        ``edit_apply`` codes) so the failed tool event carries the taxonomy."""
        from errorta_tools.runner.apply_workspace import resolve_workspace_path

        from .edit_apply import EditApplyError, apply_code_edit
        root = (
            self.task_root(task_id)
            if self._ws.has_worktree(task_id)
            else self._ws.root
        )
        target = resolve_workspace_path(root, rel_path, must_exist=False)
        if not target.exists() or not target.is_file():
            raise CodingWorkspaceError(
                f"edit_target_missing: {rel_path} does not exist in the "
                "worktree — code_edit cannot create a file; use code_write")
        try:
            raw = target.read_bytes()
        except OSError as exc:
            raise CodingWorkspaceError(
                f"edit_target_missing: {rel_path} is unreadable: {exc}") from exc
        # Same NUL heuristic as write_file's guard path: a genuine binary asset
        # is not editable text; a non-UTF-8 TEXT file has no NUL and is decoded
        # leniently below, so it stays editable.
        if b"\x00" in raw:
            raise CodingWorkspaceError(
                f"edit_target_binary: {rel_path} is a binary asset — code_edit "
                "is text-only; re-emit it with code_write content_base64")
        old_content = raw.decode("utf-8", errors="replace")
        try:
            new_content = apply_code_edit(
                old_content, old_string, new_string, replace_all=replace_all)
        except EditApplyError as exc:
            raise CodingWorkspaceError(str(exc)) from exc
        return self.write_file(rel_path, new_content, task_id=task_id,
                               summary=summary)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/coding/test_coding_workspace.py -q`
Expected: all pass (new and pre-existing)

- [ ] **Step 5: Commit**

```bash
git add python/errorta_council/coding/workspace.py python/tests/coding/test_coding_workspace.py
git commit -m "feat(coding): CodingWorkspace.edit_file — anchored edit with F140 guard parity"
```

---

### Task 3: turn_controller wiring — allowlist, dispatch, safe intent

**Files:**
- Modify: `errorta_council/coding/turn_controller.py` (`_ROLE_TOOLS` ~line 27; `execute_dev_turn` ~lines 180–276; `_safe_intent` ~lines 302–321)
- Test (extend): `tests/coding/test_turn_controller.py`
- Test (update asserts): `tests/coding/test_turn_controller.py:27`, `tests/coding/test_spec25_expressibility.py:778` and `:931`, `tests/coding/test_spec15_capability_aware.py:69`

**Interfaces:**
- Consumes: `CodingWorkspace.edit_file(rel_path, old_string, new_string, *, replace_all=False, task_id, summary="") -> str` from Task 2 (raises `CodingWorkspaceError` with typed-prefix messages).
- Produces: `allowed_tools_for_role("dev") == ("code_write", "code_edit")`; `execute_dev_turn` executes `code_edit` calls, recording tool events with `tool="code_edit"`, result `{"head", "path"}` on success, the typed reason on failure (plus executor-level `edit_invalid_args` for non-string args); `_safe_intent` for `code_edit` records `path`, `old_bytes`, `new_bytes`, `replace_all` and never the strings. `tool_catalog_text` picks up the new tool automatically (derives from `allowed_tools_for_role`).

- [ ] **Step 1: Update the existing tuple asserts (they lock the old reality)**

- `tests/coding/test_turn_controller.py:27`: change to `assert allowed_tools_for_role("dev") == ("code_write", "code_edit")`
- `tests/coding/test_spec25_expressibility.py:778` and `:931`: same change (`allowed_tools_for_role(DEV) == ("code_write", "code_edit")`) — the surrounding invariant ("nothing in this spec mutates the role→tool table") is unchanged; only the table's value moved.
- `tests/coding/test_spec15_capability_aware.py:69`: `assert man[DEV].tools == ("code_write", "code_edit")`

- [ ] **Step 2: Write the failing tests**

Append to `tests/coding/test_turn_controller.py`:

```python
def test_code_edit_executes_and_records_succeeded_event(tmp_errorta_home: Path) -> None:
    store = _store(tmp_errorta_home, "tcedit")
    task = store.add_task(title="impl", role="dev")
    ws = _workspace("tcedit", store)
    ws.write_file("app.py", "def f():\n    return 1\n", task_id=task.task_id)
    data = {"tool_calls": [{"tool": "code_edit", "args": {
        "path": "app.py", "old_string": "return 1", "new_string": "return 2"}}]}

    summary = CodingTurnController(store, ws).execute_dev_turn(
        task=task, member={"id": "m-dev"}, data=data)

    assert summary.success_count == 1 and not summary.failed
    assert (ws.root() / "app.py").read_text("utf-8") == "def f():\n    return 2\n"
    ev = [e for e in store.list_tool_events() if e["tool"] == "code_edit"]
    assert ev and ev[-1]["status"] == "succeeded"
    assert ev[-1]["result"]["path"] == "app.py"
    assert ev[-1]["result"]["head"]


def test_code_edit_failure_records_typed_failed_event(tmp_errorta_home: Path) -> None:
    store = _store(tmp_errorta_home, "tceditfail")
    task = store.add_task(title="impl", role="dev")
    ws = _workspace("tceditfail", store)
    ws.write_file("app.py", "x = 1\n", task_id=task.task_id)
    data = {"tool_calls": [{"tool": "code_edit", "args": {
        "path": "app.py", "old_string": "y = 2", "new_string": "y = 3"}}]}

    summary = CodingTurnController(store, ws).execute_dev_turn(
        task=task, member={"id": "m-dev"}, data=data)

    assert summary.success_count == 0 and summary.failed
    path, reason = summary.failures[0]
    assert path == "app.py" and reason.startswith("edit_no_match: ")
    ev = [e for e in store.list_tool_events() if e["tool"] == "code_edit"]
    assert ev[-1]["status"] == "failed"
    assert ev[-1]["error"].startswith("edit_no_match: ")


def test_code_edit_non_string_args_fail_before_workspace(tmp_errorta_home: Path) -> None:
    store = _store(tmp_errorta_home, "tceditargs")
    task = store.add_task(title="impl", role="dev")
    ws = _workspace("tceditargs", store)
    data = {"tool_calls": [{"tool": "code_edit", "args": {
        "path": "app.py", "old_string": 7, "new_string": ["x"]}}]}

    summary = CodingTurnController(store, ws).execute_dev_turn(
        task=task, member={"id": "m-dev"}, data=data)

    assert summary.failed
    assert summary.failures[0][1].startswith("edit_invalid_args: ")


def test_code_edit_safe_intent_records_sizes_not_content(tmp_errorta_home: Path) -> None:
    store = _store(tmp_errorta_home, "tceditintent")
    task = store.add_task(title="impl", role="dev")
    ws = _workspace("tceditintent", store)
    ws.write_file("app.py", "alpha beta\n", task_id=task.task_id)
    data = {"tool_calls": [{"tool": "code_edit", "args": {
        "path": "app.py", "old_string": "beta", "new_string": "gamma",
        "replace_all": True}}]}

    CodingTurnController(store, ws).execute_dev_turn(
        task=task, member={"id": "m-dev"}, data=data)

    intent = [e for e in store.list_tool_events()
              if e["tool"] == "code_edit"][-1]["intent"]
    assert intent["path"] == "app.py"
    assert intent["old_bytes"] == 4 and intent["new_bytes"] == 5
    assert intent["replace_all"] is True
    assert "old_string" not in intent and "new_string" not in intent


def test_mixed_write_and_edit_turn_composes(tmp_errorta_home: Path) -> None:
    # A code_write creating a file and a code_edit refining it in the SAME turn:
    # calls run in order, each sees the prior call's tree.
    store = _store(tmp_errorta_home, "tcmixed")
    task = store.add_task(title="impl", role="dev")
    ws = _workspace("tcmixed", store)
    data = {"tool_calls": [
        {"tool": "code_write", "args": {"path": "m.py", "content": "v = 1\n"}},
        {"tool": "code_edit", "args": {
            "path": "m.py", "old_string": "v = 1", "new_string": "v = 2"}},
    ]}

    summary = CodingTurnController(store, ws).execute_dev_turn(
        task=task, member={"id": "m-dev"}, data=data)

    assert summary.declared_count == 2 and summary.success_count == 2
    assert (ws.root() / "m.py").read_text("utf-8") == "v = 2\n"


def test_sequential_edits_to_same_file_compose(tmp_errorta_home: Path) -> None:
    store = _store(tmp_errorta_home, "tcseq")
    task = store.add_task(title="impl", role="dev")
    ws = _workspace("tcseq", store)
    ws.write_file("s.py", "a = 1\nb = 2\n", task_id=task.task_id)
    data = {"tool_calls": [
        {"tool": "code_edit", "args": {
            "path": "s.py", "old_string": "a = 1", "new_string": "a = 10"}},
        {"tool": "code_edit", "args": {
            "path": "s.py", "old_string": "b = 2", "new_string": "b = 20"}},
    ]}

    summary = CodingTurnController(store, ws).execute_dev_turn(
        task=task, member={"id": "m-dev"}, data=data)

    assert summary.success_count == 2
    assert (ws.root() / "s.py").read_text("utf-8") == "a = 10\nb = 20\n"


def test_code_edit_not_allowed_for_other_roles() -> None:
    assert "code_edit" in allowed_tools_for_role("dev")
    for role in ("pm", "reviewer", "tester", "designer"):
        assert "code_edit" not in allowed_tools_for_role(role)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/coding/test_turn_controller.py -q`
Expected: the new `code_edit` tests fail (the call is rejected as `tool_not_allowed`, so success_count == 0 / no `code_edit` events); `test_role_tool_catalog_is_scoped` fails on the updated tuple.

- [ ] **Step 4: Write the implementation**

In `errorta_council/coding/turn_controller.py`:

(a) `_ROLE_TOOLS` — change the DEV entry:

```python
    DEV: ("code_write", "code_edit"),
```

and update the stale comment above it ("The dev's code_write is the single member-driven executed tool") to say the dev's `code_write`/`code_edit` are the member-driven executed tools.

(b) In `execute_dev_turn`, after the allowlist rejection block (`continue`) and before the current `path = str(args.get("path", ""))` line, insert the `code_edit` branch so the existing code_write body stays as the fall-through:

```python
            path = str(args.get("path", ""))
            if tool == "code_edit":
                old_string = args.get("old_string")
                new_string = args.get("new_string")
                replace_all = bool(args.get("replace_all", False))
                if not isinstance(old_string, str) or not isinstance(new_string, str):
                    reason = ("edit_invalid_args: old_string and new_string "
                              "must both be strings")
                    failures.append((path, reason))
                    self.store.record_tool_event(
                        turn_id=turn_id,
                        task_id=task.task_id,
                        member_id=str(member.get("id", "m-dev")),
                        role=DEV,
                        tool="code_edit",
                        status="failed",
                        intent=intent,
                        error=reason,
                    )
                    continue
                try:
                    if self.workspace is None:
                        raise RuntimeError("coding_workspace_unavailable")
                    head = self.workspace.edit_file(
                        path, old_string, new_string,
                        replace_all=replace_all, task_id=task.task_id)
                except Exception as exc:
                    reason = str(exc)
                    failures.append((path, reason))
                    self.store.record_tool_event(
                        turn_id=turn_id,
                        task_id=task.task_id,
                        member_id=str(member.get("id", "m-dev")),
                        role=DEV,
                        tool="code_edit",
                        status="failed",
                        intent=intent,
                        error=reason,
                    )
                    continue
                successes += 1
                self.store.record_tool_event(
                    turn_id=turn_id,
                    task_id=task.task_id,
                    member_id=str(member.get("id", "m-dev")),
                    role=DEV,
                    tool="code_edit",
                    status="succeeded",
                    intent=intent,
                    result={"head": head, "path": path},
                )
                continue
```

(The pre-existing `path = str(args.get("path", ""))` line that currently sits after the allowlist check is subsumed by the one above — remove the duplicate so `path` is assigned once before the branch.)

(c) `_safe_intent` — extend the tool-specific arm (current `if tool == "code_write": ... elif args:`) with a `code_edit` case between them:

```python
        elif tool == "code_edit":
            old = args.get("old_string")
            new = args.get("new_string")
            intent["old_bytes"] = (
                len(old.encode("utf-8")) if isinstance(old, str) else 0)
            intent["new_bytes"] = (
                len(new.encode("utf-8")) if isinstance(new, str) else 0)
            intent["replace_all"] = bool(args.get("replace_all", False))
```

(the full strings never enter the ledger event — same hygiene rationale as `content_bytes`).

- [ ] **Step 5: Run the wider affected suites**

Run: `python -m pytest tests/coding/test_turn_controller.py tests/coding/test_spec17_tool_catalog.py tests/coding/test_spec25_expressibility.py tests/coding/test_spec15_capability_aware.py tests/coding/test_gl03_capability_alarm.py tests/coding/test_gl05_parallelism.py -q`
Expected: all pass. If a spec17/gl03 test hard-codes the string `"code_write"` as the full expected tools rendering, it derives from `allowed_tools_for_role` per its own invariant — fix only genuine literal-tuple asserts, never weaken derivation-based ones.

- [ ] **Step 6: Commit**

```bash
git add python/errorta_council/coding/turn_controller.py python/tests/coding/test_turn_controller.py python/tests/coding/test_spec25_expressibility.py python/tests/coding/test_spec15_capability_aware.py
git commit -m "feat(coding): execute code_edit dev tool calls (allowlist, dispatch, safe intent)"
```

---

### Task 4: Vocabulary — dev prompt, schema example, cosmetic strings

**Files:**
- Modify: `errorta_council/coding/runner.py` (`_dev_prompt` comment ~line 4044; `_dev_prompt_segments` ~lines 4055–4081; `write_missing` rationale ~line 7455)
- Modify: `errorta_council/coding/schemas.py` (`_MINIMAL_INTENT_EXAMPLES` ~line 581)
- Modify: `errorta_council/coding/capabilities.py` (CLOSURE_TABLE comment ~line 430)
- Test (update): `tests/coding/test_prompt_segments_golden.py` (`_old_dev_prompt` ~lines 210–265)

**Interfaces:**
- Consumes: nothing new (text only; Task 3 already landed the tools).
- Produces: the dev prompt teaches `code_edit`; `minimal_valid_example("dev", "tool_plan_edit")` returns a valid envelope (Spec-25 test auto-enumerates it).

- [ ] **Step 1: Update the prompt text in `runner._dev_prompt_segments`**

Replace the `existing` assignment:

```python
    existing = (f"Current files in the worktree (EXTEND these — do not drop "
                f"existing code; code_write replaces the whole file so include "
                f"all of it, or use code_edit for a targeted "
                f"change):\n{readback}\n" if readback
                else "The worktree is empty; create the files from scratch.\n")
```

In the `envelope` string, insert between the binary-asset sentence (ends `"undecodable .png is not a valid image). "`) and `"Reply with ONLY a coding_turn.v1 envelope: "`:

```python
        # code_edit: an anchored find/replace so a fix to a LARGE file costs
        # output proportional to the change — a whole-file re-emit of a large
        # module gets truncated by the output budget and then (rightly) blocked
        # by the destructive-write guard, making the fix impossible to land.
        "To CHANGE an existing file, prefer code_edit: "
        '{"tool": "code_edit", "args": {"path": "rel/path", "old_string": '
        '"<exact existing text>", "new_string": "<replacement>"}} — '
        "old_string must be copied EXACTLY from the current file (whitespace "
        "included) and must match exactly once; add surrounding lines to make "
        'it unique, or set "replace_all": true to change every occurrence. '
        "For a large file ALWAYS use code_edit (a whole-file re-emit gets "
        "truncated and blocked); use code_write only for a new file, a full "
        "rewrite of a small file, or a binary asset. "
```

Also update the stale `_dev_prompt` comment above (~line 4044): "code_write replaces the WHOLE file, so it must include everything that should remain" → append "; code_edit (anchored find/replace) is the targeted alternative for existing files."

- [ ] **Step 2: Mirror the same two text changes in `tests/coding/test_prompt_segments_golden.py::_old_dev_prompt`**

The golden's contract is that the reference mirrors the live text byte-for-byte — apply the identical `existing` string and the identical inserted envelope sentence at the identical position.

- [ ] **Step 3: Add the schema example**

In `errorta_council/coding/schemas.py::_MINIMAL_INTENT_EXAMPLES`, after the `("dev", "tool_plan")` entry:

```python
    ("dev", "tool_plan_edit"): {
        "kind": "tool_plan", "task_type": "implementation",
        "tool_calls": [{"tool": "code_edit",
                        "args": {"path": "src/hud.py",
                                 "old_string": "def render_hud():",
                                 "new_string": "def render_hud(score):"}}],
    },
```

- [ ] **Step 4: Cosmetic string updates**

- `runner.py` ~line 7455: `rationale="implementation task completed no code_write tool event"` → `rationale="implementation task completed no successful write tool event (code_write/code_edit)"`. First check nothing asserts the old string: `grep -rn "no code_write tool event" python/` — update any test that does.
- `capabilities.py` ~line 430 comment: `` `_ROLE_TOOLS[DEV] == ("code_write",)` `` → `` `_ROLE_TOOLS[DEV] == ("code_write", "code_edit")` ``.
- `capabilities.py` line 4 docstring ("A DEV has exactly one tool — ``code_write``") → "A DEV's executed tools are ``code_write`` and ``code_edit``" (keep the sentence's surrounding sense).

- [ ] **Step 5: Run the affected suites**

Run: `python -m pytest tests/coding/test_prompt_segments_golden.py tests/coding/test_coding_schemas.py tests/coding/test_spec25_expressibility.py -q`
Expected: all pass (the expressibility parametrization now includes `("dev", "tool_plan_edit")` and round-trips it).

- [ ] **Step 6: Full coding suite**

Run: `python -m pytest tests/coding -q`
Expected: all pass. Fix any straggler that asserted the old single-tool text (derivation-based tests must not be weakened; only literal mirrors move).

- [ ] **Step 7: Commit**

```bash
git add python/errorta_council/coding/runner.py python/errorta_council/coding/schemas.py python/errorta_council/coding/capabilities.py python/tests/coding/test_prompt_segments_golden.py
git commit -m "feat(coding): teach devs the code_edit vocabulary (prompt, schema example)"
```

---

### Task 5: Verification sweep

**Files:** none created; read-only checks plus any straggler fixes.

- [ ] **Step 1: Repo-wide coupling grep**

```bash
grep -rn "code_write" python/errorta_council/coding/ python/tests/coding/ | grep -v "code_edit" | grep -iv "test_" | head -40
```

Confirm every remaining `code_write` mention in `errorta_council/coding/` is either correct as-is (binary channel, legacy `files` normalization, F140 docstrings that describe code_write specifically) or was updated by Tasks 3–4. The F140 `write_guard.py` docstring may keep saying `code_write` in its history narrative but its guard now also runs for `code_edit` via `write_file` — add one sentence to `write_guard.py`'s module docstring noting the guard also covers `code_edit` splices (they arrive as full old→new pairs through `write_file`).

- [ ] **Step 2: Full test suite for the coding package + the two adjacent packages that import it**

```bash
python -m pytest tests/coding -q && python -m pytest tests/council tests/mobile -q
```

Expected: all pass.

- [ ] **Step 3: Commit any straggler fixes**

```bash
git add -A python/
git commit -m "chore(coding): code_edit coupling sweep (guard docstring, stragglers)"
```

(Skip the commit if the sweep changed nothing.)
