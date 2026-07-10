from __future__ import annotations

from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_project_grounding.context_packets import (
    build_pm_boot_briefing,
    build_role_context_packet,
    ensure_pm_working_memory,
)
from errorta_project_grounding.pm_working_memory import SOURCE_TYPE


def _store(tmp_path: Path, project_id: str = "pmwm-prompt") -> LedgerStore:
    store = LedgerStore(project_id, root=tmp_path)
    store.create_project(
        north_star="Build the MVP",
        definition_of_done="All tests pass",
        target="new",
        repo_path=None,
    )
    return store


def test_ensure_creates_pm_memory_and_pm_packet_includes_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = ensure_pm_working_memory(store)

    packet = build_role_context_packet(store=store, role="pm")

    assert item is not None
    assert packet is not None
    pm_items = [i for i in packet["items"] if i["source_type"] == SOURCE_TYPE]
    assert len(pm_items) == 1
    assert pm_items[0]["why_included"] == "pm working memory"


def test_dev_packet_does_not_receive_pm_working_memory(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ensure_pm_working_memory(store)

    packet = build_role_context_packet(store=store, role="dev")

    assert packet is not None
    assert all(i["source_type"] != SOURCE_TYPE for i in packet["items"])
    assert packet["omitted"]["not_visible_to_role"] >= 1


def test_pm_boot_briefing_includes_pm_working_memory_on_first_turn(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ensure_pm_working_memory(store)

    briefing = build_pm_boot_briefing(store=store)

    assert briefing is not None
    assert any(i["source_type"] == SOURCE_TYPE for i in briefing["durable_truth"])


def test_pm_working_memory_survives_tiny_packet_budget(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ensure_pm_working_memory(store)
    for i in range(10):
        store.record_decision(
            title=f"Decision {i}",
            context="pm_decision",
            choice="pm_decision",
            rationale="r",
        )
    from errorta_project_grounding.update_pipeline import sync_from_ledger
    sync_from_ledger(store)

    packet = build_role_context_packet(store=store, role="pm", token_budget=50)

    assert packet is not None
    assert any(i["source_type"] == SOURCE_TYPE for i in packet["items"])
