"""F088-07 — role-scoped context packets."""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_project_grounding.context_packets import (
    build_role_context_packet,
    format_packet,
)
from errorta_project_grounding.memory_store import (
    MemoryItem,
    MemorySourceRef,
    MemoryVisibility,
    ProjectMemoryStore,
)


def _store(tmp: Path, pid: str) -> LedgerStore:
    s = LedgerStore(pid, root=tmp)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    return s


def _mem(tmp: Path, pid: str) -> ProjectMemoryStore:
    return ProjectMemoryStore(pid, root=tmp)


def _durable(mem, mid, content, *, created_at, **md):
    mem.put(MemoryItem(project_id=mem.project_id, authority="durable_truth",
                       source_type="pm_decision", source_ref=MemorySourceRef(task_id="t"),
                       content=content, memory_id=mid, created_at=created_at, metadata=md))


def _wip(mem, mid, content, *, created_at, vis=None, **md):
    mem.put(MemoryItem(project_id=mem.project_id, authority="wip",
                       source_type="open_pr", source_ref=MemorySourceRef(pr_id="pr1"),
                       content=content, memory_id=mid, created_at=created_at,
                       visibility=vis or MemoryVisibility(), metadata=md))


def _claim(mem, mid, content):
    mem.put(MemoryItem(project_id=mem.project_id, authority="claim",
                       source_type="dev_turn", source_ref=MemorySourceRef(task_id="t"),
                       content=content, memory_id=mid, created_at="2026-01-01T00:00:00Z"))


# --- ordering, exclusion, visibility ----------------------------------------


def test_durable_before_wip_even_when_wip_newer(tmp_path) -> None:
    s = _store(tmp_path, "p1"); mem = _mem(tmp_path, "p1")
    _durable(mem, "d1", "older durable fact", created_at="2026-01-01T00:00:00Z")
    _wip(mem, "w1", "newer wip", created_at="2026-09-09T00:00:00Z")
    pkt = build_role_context_packet(store=s, role="pm")
    auths = [i["authority"] for i in pkt["items"]]
    assert auths[0] == "durable_truth" and "wip" in auths
    assert auths.index("durable_truth") < auths.index("wip")


def test_claims_excluded_for_every_role(tmp_path) -> None:
    s = _store(tmp_path, "p2"); mem = _mem(tmp_path, "p2")
    _durable(mem, "d1", "x", created_at="2026-01-01T00:00:00Z")
    _claim(mem, "c1", "the db is definitely postgres")
    for role in ("pm", "dev", "reviewer", "tester"):
        pkt = build_role_context_packet(store=s, role=role)
        assert all(i["authority"] != "claim" for i in pkt["items"])
        assert pkt["omitted"]["claims_excluded"] >= 1


def test_visibility_hides_role_invisible_rows(tmp_path) -> None:
    s = _store(tmp_path, "p3"); mem = _mem(tmp_path, "p3")
    _wip(mem, "w1", "pm-only note", created_at="2026-01-01T00:00:00Z",
         vis=MemoryVisibility(default_dev=False))
    dev = build_role_context_packet(store=s, role="dev")
    pm = build_role_context_packet(store=s, role="pm")
    assert all(i["ref"] != "mem:w1" for i in dev["items"])
    assert any(i["ref"] == "mem:w1" for i in pm["items"])
    assert dev["omitted"]["not_visible_to_role"] >= 1


def test_wip_overlapping_durable_marked_open_overlay(tmp_path) -> None:
    s = _store(tmp_path, "p4"); mem = _mem(tmp_path, "p4")
    _durable(mem, "d1", "divide raises ValueError", created_at="2026-01-01T00:00:00Z",
             conflict_group="divide-behavior")
    _wip(mem, "w1", "wip changing divide", created_at="2026-09-09T00:00:00Z",
         conflict_group="divide-behavior")
    pkt = build_role_context_packet(store=s, role="dev")
    overlay = next(i for i in pkt["items"] if i["ref"] == "mem:w1")
    durable = next(i for i in pkt["items"] if i["ref"] == "mem:d1")
    assert overlay.get("open_overlay") is True
    assert pkt["items"].index(durable) < pkt["items"].index(overlay)


# --- budget trim ------------------------------------------------------------


def test_trim_drops_wip_before_durable(tmp_path) -> None:
    s = _store(tmp_path, "p5"); mem = _mem(tmp_path, "p5")
    _durable(mem, "d1", "keep this durable fact", created_at="2026-01-01T00:00:00Z")
    for i in range(6):
        _wip(mem, f"w{i}", f"wip number {i} with some text", created_at=f"2026-02-0{i+1}T00:00:00Z")
    pkt = build_role_context_packet(store=s, role="dev", token_budget=120)
    assert any(i["authority"] == "durable_truth" for i in pkt["items"])  # durable kept
    assert all(i["authority"] != "wip" for i in pkt["items"])  # wip trimmed first
    assert pkt["omitted"]["over_budget"] >= 1 and pkt["budget"]["truncated"] is True


# --- determinism + no-store regression --------------------------------------


def test_deterministic_output(tmp_path) -> None:
    s = _store(tmp_path, "p6"); mem = _mem(tmp_path, "p6")
    _durable(mem, "d1", "a", created_at="2026-01-01T00:00:00Z")
    _wip(mem, "w1", "b", created_at="2026-02-01T00:00:00Z")
    assert build_role_context_packet(store=s, role="pm") == build_role_context_packet(store=s, role="pm")


def test_no_memory_store_returns_none_and_empty_text(tmp_path) -> None:
    s = _store(tmp_path, "p7")  # never synced -> no memory.sqlite3
    assert build_role_context_packet(store=s, role="dev") is None
    assert format_packet(None) == ""


def test_format_packet_renders_refs(tmp_path) -> None:
    s = _store(tmp_path, "p8"); mem = _mem(tmp_path, "p8")
    _durable(mem, "d1", "divide raises ValueError", created_at="2026-01-01T00:00:00Z")
    text = format_packet(build_role_context_packet(store=s, role="dev"))
    assert "project_context_packet.v1" in text and "mem:d1" in text


# --- runner integration: byte-equivalent when no grounding ------------------


def test_runner_helper_empty_without_store(tmp_path) -> None:
    from errorta_council.coding.runner import _grounding_packet_text
    s = _store(tmp_path, "p9")
    assert _grounding_packet_text("dev", s) == ""  # no memory db -> no packet


def test_runner_helper_injects_when_memory_present(tmp_path) -> None:
    from errorta_council.coding.runner import _grounding_packet_text
    s = _store(tmp_path, "p10"); mem = _mem(tmp_path, "p10")
    _durable(mem, "d1", "use sqlite", created_at="2026-01-01T00:00:00Z")
    out = _grounding_packet_text("pm", s)
    assert "mem:d1" in out and "context packet" in out.lower()
