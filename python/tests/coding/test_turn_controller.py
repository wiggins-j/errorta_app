import json
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
    # F087-14 WS-3: only the dev's code_write is an executed tool; reviewer/tester
    # are verdict roles with no executable tool surface (no over-promised tools).
    assert allowed_tools_for_role("dev") == ("code_write",)
    assert allowed_tools_for_role("reviewer") == ()
    assert allowed_tools_for_role("tester") == ()
    assert "merge_back" not in allowed_tools_for_role("dev")
    assert "code_exec" not in allowed_tools_for_role("dev")
    assert "merge_back" not in tool_catalog_text("dev")


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
    assert events[0]["error"] == "tool_not_allowed"
    assert events[0]["intent"]["args_keys"] == ["command"]
