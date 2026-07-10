"""F042 child-run store, inbox, and route tests."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from errorta_app import server as server_mod
from errorta_council.children import AsyncInbox, ChildRunStore
from errorta_council.run_store import RunStore


def test_child_run_store_create_update_list_and_skip_corrupt(runs_dir_path) -> None:
    store = ChildRunStore(runs_dir=runs_dir_path)
    record = store.create(
        parent_run_id="run-1",
        member_id="m-1",
        task_kind="tester",
        title="Run focused tests",
        prompt="pytest tests/council/test_child_runs.py",
    )
    running = store.mark_running(record)
    completed = store.mark_completed(
        running,
        summary_ref={
            "class_": "child_run_summary",
            "child_run_id": running.child_run_id,
            "message_id": "crm-1",
            "content_sha256": "abc",
            "payload_preview": "ok",
            "preview_sha256": "ignored-in-store",
            "payload_bytes": 2,
        },
    )
    corrupt = runs_dir_path / "children" / "run-1" / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")

    assert completed.status == "completed"
    assert store.get("run-1", record.child_run_id).summary_ref is not None
    assert [r.child_run_id for r in store.list("run-1")] == [record.child_run_id]


def test_async_inbox_caps_preview_and_hashes_payload(runs_dir_path) -> None:
    inbox = AsyncInbox(runs_dir=runs_dir_path, max_preview_bytes=8)
    message = inbox.append_payload(
        parent_run_id="run-1",
        child_run_id="cr-1",
        message_kind="summary",
        payload="0123456789abcdef",
        artifact_refs=[{"kind": "log", "sha256": "abc"}],
    )

    assert message.payload_preview == "01234567\n[truncated]"
    assert message.payload_bytes == 16
    assert message.payload_sha256
    [loaded] = inbox.list("run-1", "cr-1")
    assert loaded.message_id == message.message_id
    assert loaded.artifact_refs[0]["kind"] == "log"


def test_child_routes_list_records_and_messages(tmp_errorta_home, runs_dir_path) -> None:
    runs = RunStore(runs_dir=runs_dir_path)
    meta = runs.create_run(
        room_id="rm-child",
        room_snapshot={"members": []},
        prompt="child status",
        corpus_ids=[],
    )
    child_store = ChildRunStore(runs_dir=runs.runs_dir)
    record = child_store.create(
        parent_run_id=meta.id,
        member_id="m-1",
        task_kind="reviewer",
        title="Review diff",
        prompt="review",
    )
    AsyncInbox(runs_dir=runs.runs_dir).append_payload(
        parent_run_id=meta.id,
        child_run_id=record.child_run_id,
        message_kind="summary",
        payload="looks good",
    )
    client = TestClient(server_mod.app)

    listed = client.get(f"/council/runs/{meta.id}/children")
    assert listed.status_code == 200, listed.text
    assert listed.json()["children"][0]["child_run_id"] == record.child_run_id

    messages = client.get(
        f"/council/runs/{meta.id}/children/{record.child_run_id}/messages"
    )
    assert messages.status_code == 200, messages.text
    assert messages.json()["messages"][0]["payload_preview"] == "looks good"


def test_child_run_event_projection_is_json_safe(runs_dir_path) -> None:
    record = ChildRunStore(runs_dir=runs_dir_path).create(
        parent_run_id="run-1",
        member_id="m-1",
        task_kind="researcher",
        title="Read source",
        prompt="source",
    )
    json.dumps(record.event_projection(), sort_keys=True)
