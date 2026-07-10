"""F088-09 — dev context requests (typed, read-only mid-run retrieval)."""
from __future__ import annotations

import json
from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.schemas import (
    DeveloperContextRequestIntent,
    DeveloperToolPlanIntent,
    TurnParseError,
    parse_coding_turn,
)
from errorta_project_grounding import retrieval
from errorta_project_grounding.adapter import GroundingHit
from errorta_project_grounding.memory_store import (
    MemoryItem,
    MemoryQuery,
    MemorySourceRef,
    ProjectMemoryStore,
)


def _turn(intent: dict, *, task_id="t1") -> str:
    return json.dumps({"schema_version": "coding_turn.v1", "role": "dev",
                       "task_id": task_id, "intent": intent})


# --- schema dispatch --------------------------------------------------------


def test_context_request_parses_to_its_intent() -> None:
    out = parse_coding_turn("dev", "t1", _turn({
        "kind": "context_request", "reason": "missing_api_contract",
        "question": "What does divide do on zero?",
        "scope": {"corpus_query": "divide by zero", "sources": ["memory", "corpus"]},
        "needed_for": "implementation", "max_items": 4}))
    assert not isinstance(out, TurnParseError)
    assert isinstance(out.intent, DeveloperContextRequestIntent)
    assert out.intent.scope.sources == ["memory", "corpus"]


def test_tool_plan_still_parses_to_tool_intent() -> None:
    out = parse_coding_turn("dev", "t1", _turn({
        "kind": "tool_plan", "task_type": "implementation",
        "tool_calls": [{"tool": "code_write", "args": {"path": "a.py", "content": "x=1"}}]}))
    assert not isinstance(out, TurnParseError)
    assert isinstance(out.intent, DeveloperToolPlanIntent)


def test_context_request_without_question_fails_closed() -> None:
    out = parse_coding_turn("dev", "t1", _turn({"kind": "context_request", "question": "  "}))
    assert isinstance(out, TurnParseError)


# --- read-only answer -------------------------------------------------------


def _store(tmp: Path, pid: str) -> LedgerStore:
    s = LedgerStore(pid, root=tmp)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    return s


def _durable(mem, mid, content):
    mem.put(MemoryItem(project_id=mem.project_id, authority="durable_truth",
                       source_type="pm_decision", source_ref=MemorySourceRef(task_id="t"),
                       content=content, memory_id=mid, created_at="2026-01-01T00:00:00Z"))


def test_answer_is_read_only_and_grounded(tmp_path, monkeypatch) -> None:
    from errorta_council.coding.runner import _answer_dev_context_request
    s = _store(tmp_path, "cr1")
    mem = ProjectMemoryStore("cr1", root=tmp_path)
    _durable(mem, "d1", "divide raises ValueError on zero")
    before = len(mem.query(MemoryQuery(authorities=("durable_truth",), limit=200)))

    monkeypatch.setattr(retrieval, "retrieve_project_corpus",
                        lambda *a, **k: [GroundingHit(content="README: divide -> ValueError",
                                                      corpus_id="c", chunk_id="c9", score=0.8)])
    task = s.add_task(title="impl divide", role="dev")
    intent = DeveloperContextRequestIntent(
        kind="context_request", question="zero behavior?",
        scope={"sources": ["memory", "corpus"], "corpus_query": "divide by zero"})
    answer = _answer_dev_context_request(s, task, intent)

    assert answer["schema_version"] == "context_response.v1"
    assert answer["corpus_evidence"][0]["ref"] == "hit:c:c9"
    assert answer["memory"][0]["ref"] == "mem:d1"
    # recorded for audit (read-only ledger metadata)
    assert any(d["choice"] == "context_request" for d in s.list_decisions())
    # NO durable mutation — memory only queried
    after = len(mem.query(MemoryQuery(authorities=("durable_truth",), limit=200)))
    assert after == before


def test_answer_caps_results(tmp_path, monkeypatch) -> None:
    from errorta_council.coding.runner import _answer_dev_context_request
    s = _store(tmp_path, "cr2")
    mem = ProjectMemoryStore("cr2", root=tmp_path)
    for i in range(10):
        _durable(mem, f"d{i}", f"fact {i}")
    monkeypatch.setattr(retrieval, "retrieve_project_corpus",
                        lambda *a, **k: [GroundingHit(content=f"h{i}", corpus_id="c",
                                                      chunk_id=f"c{i}") for i in range(10)])
    task = s.add_task(title="t", role="dev")
    intent = DeveloperContextRequestIntent(kind="context_request", question="q",
                                           scope={"sources": ["memory", "corpus"]}, max_items=3)
    answer = _answer_dev_context_request(s, task, intent)
    assert len(answer["corpus_evidence"]) == 3 and len(answer["memory"]) == 3


# --- P1 fix: the answer is reliably delivered back to the dev ----------------


def test_dev_prompt_delivers_prior_context_response(tmp_path, monkeypatch) -> None:
    from errorta_council.coding.runner import _answer_dev_context_request, _dev_prompt
    s = _store(tmp_path, "cr3")
    ProjectMemoryStore("cr3", root=tmp_path)  # create the memory db
    monkeypatch.setattr(retrieval, "retrieve_project_corpus",
                        lambda *a, **k: [GroundingHit(content="divide raises", corpus_id="c", chunk_id="c1")])
    task = s.add_task(title="impl divide", role="dev")
    _answer_dev_context_request(s, task, DeveloperContextRequestIntent(
        kind="context_request", question="zero?", scope={"sources": ["corpus"]}))
    prompt = _dev_prompt(task, s)
    assert "context_response.v1" in prompt
    assert "Context response to YOUR earlier request" in prompt


def test_context_request_writes_wip_memory_row(tmp_path, monkeypatch) -> None:
    from errorta_council.coding.runner import _answer_dev_context_request
    s = _store(tmp_path, "cr4")
    mem = ProjectMemoryStore("cr4", root=tmp_path)
    monkeypatch.setattr(retrieval, "retrieve_project_corpus", lambda *a, **k: [])
    task = s.add_task(title="t", role="dev")
    _answer_dev_context_request(s, task, DeveloperContextRequestIntent(
        kind="context_request", question="what API?", scope={"sources": ["memory"]}))
    wip = mem.query(MemoryQuery(authorities=("wip",), limit=200))
    assert any(i.source_type == "context_request" for i in wip)  # surfaces in PM briefing
