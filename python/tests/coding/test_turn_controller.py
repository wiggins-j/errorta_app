from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.turn_controller import (
    CodingTurnController,
    allowed_tools_for_role,
    tool_catalog_text,
)
from errorta_council.coding.workspace import CodingWorkspace


def _store(tmp_errorta_home: Path, project_id: str = "tc") -> LedgerStore:
    s = LedgerStore(project_id)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    return s


def _workspace(project_id: str, store: LedgerStore) -> CodingWorkspace:
    ws = CodingWorkspace(project_id, store)
    ws.setup(target="new", repo_path=None)
    return ws


def test_role_tool_catalog_is_scoped() -> None:
    # F087-14 WS-3: only the dev holds executed tools (code_write + code_edit);
    # reviewer/tester are verdict roles with no executable tool surface (no
    # over-promised tools).
    assert allowed_tools_for_role("dev") == ("code_write", "code_edit")
    assert allowed_tools_for_role("reviewer") == ()
    assert allowed_tools_for_role("tester") == ()
    assert "merge_back" not in allowed_tools_for_role("dev")
    assert "code_exec" not in allowed_tools_for_role("dev")
    assert "merge_back" not in tool_catalog_text("dev", repo_read=False, gate=False)


def test_dev_tool_calls_write_and_record_tool_event(tmp_errorta_home: Path) -> None:
    store = _store(tmp_errorta_home, "tcwrite")
    task = store.add_task(title="impl", role="dev")
    ws = _workspace("tcwrite", store)
    data = {
        "tool_calls": [
            {"tool": "code_write", "args": {"path": "app.py", "content": "print('ok')\n"}}
        ]
    }

    summary = CodingTurnController(store, ws).execute_dev_turn(
        task=task, member={"id": "m-dev"}, data=data)

    assert summary.declared_count == 1 and summary.success_count == 1
    assert (ws.root() / "app.py").read_text("utf-8") == "print('ok')\n"
    events = store.list_tool_events()
    assert events[0]["tool"] == "code_write"
    assert events[0]["status"] == "succeeded"
    assert events[0]["result"]["path"] == "app.py"
    assert {a["path"] for a in store.list_artifacts()} == {"app.py"}


def test_legacy_files_normalize_to_code_write_events(tmp_errorta_home: Path) -> None:
    store = _store(tmp_errorta_home, "tclegacy")
    task = store.add_task(title="impl", role="dev")
    ws = _workspace("tclegacy", store)

    summary = CodingTurnController(store, ws).execute_dev_turn(
        task=task,
        member={"id": "m-dev"},
        data={"files": [{"path": "legacy.py", "content": "x = 1\n"}]},
    )

    assert summary.success_count == 1
    assert store.list_tool_events()[0]["intent"]["path"] == "legacy.py"


def test_failed_write_records_failed_tool_event(tmp_errorta_home: Path) -> None:
    store = _store(tmp_errorta_home, "tcfail")
    task = store.add_task(title="impl", role="dev")
    ws = _workspace("tcfail", store)

    summary = CodingTurnController(store, ws).execute_dev_turn(
        task=task,
        member={"id": "m-dev"},
        data={"tool_calls": [{"tool": "code_write", "args": {"path": "../x", "content": "x"}}]},
    )

    assert summary.failed is True
    events = store.list_tool_events()
    assert events[0]["status"] == "failed"
    assert events[0]["intent"]["path"] == "../x"
    assert store.list_artifacts() == []


def test_disallowed_dev_tool_records_failed_event(tmp_errorta_home: Path) -> None:
    store = _store(tmp_errorta_home, "tcdeny")
    task = store.add_task(title="impl", role="dev")
    ws = _workspace("tcdeny", store)

    summary = CodingTurnController(store, ws).execute_dev_turn(
        task=task,
        member={"id": "m-dev"},
        data={"tool_calls": [{"tool": "code_exec", "args": {"command": "pytest"}}]},
    )

    assert summary.failed is True
    assert summary.success_count == 0
    events = store.list_tool_events()
    assert events[0]["tool"] == "code_exec"
    assert events[0]["status"] == "failed"
    # Spec 17 (Item 3a): the recorded error still fails closed but now names the
    # allowed tool and the real read path.
    assert events[0]["error"].startswith("tool_not_allowed")
    assert "code_write" in events[0]["error"]
    assert events[0]["intent"]["args_keys"] == ["command"]


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
