from pathlib import Path

import pytest

from errorta_project_grounding.memory_store import (
    InvalidMemoryItem,
    MemoryItem,
    MemorySourceRef,
    ProjectMemoryStore,
)


def _store(tmp_path: Path) -> ProjectMemoryStore:
    return ProjectMemoryStore("p", root=tmp_path)


def _item(**overrides) -> MemoryItem:
    data = dict(
        project_id="p",
        authority="durable_truth",
        source_type="pm_decision",
        source_ref=MemorySourceRef(task_id="t1", path="docs/decision.md"),
        content="Use Decimal for currency.",
    )
    data.update(overrides)
    return MemoryItem(**data)


def test_valid_memory_item_roundtrips(tmp_path: Path) -> None:
    store = _store(tmp_path)
    saved = store.put(_item())

    got = store.get(saved.memory_id)

    assert got is not None
    assert got.content == "Use Decimal for currency."
    assert got.authority == "durable_truth"


@pytest.mark.parametrize(
    "patch, message",
    [
        ({"project_id": ""}, "project_id"),
        ({"authority": "rumor"}, "unknown authority"),
        ({"source_type": ""}, "source_type"),
        ({"source_ref": MemorySourceRef()}, "source_ref"),
        ({"content": ""}, "content"),
        ({"source_ref": MemorySourceRef(path=".env")}, "sensitive"),
    ],
)
def test_invalid_memory_items_are_rejected(tmp_path: Path, patch, message: str) -> None:
    with pytest.raises(InvalidMemoryItem, match=message):
        _store(tmp_path).put(_item(**patch))


def test_derived_summary_requires_source_ids(tmp_path: Path) -> None:
    item = _item(source_type="derived_summary", summary="summary", source_ids=())

    with pytest.raises(InvalidMemoryItem, match="source_ids"):
        _store(tmp_path).put(item)


def test_external_memory_requires_explicit_scope(tmp_path: Path) -> None:
    item = _item(authority="external", source_type="external_doc")

    with pytest.raises(InvalidMemoryItem, match="external_scope"):
        _store(tmp_path).put(item)


def test_claims_can_be_stored_for_audit(tmp_path: Path) -> None:
    saved = _store(tmp_path).put(_item(authority="claim", source_type="raw_member_response"))

    assert saved.memory_id.startswith("mem_")
