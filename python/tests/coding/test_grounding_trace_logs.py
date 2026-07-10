"""F088 — lock the grounding-consumption trace logging + a full simulated run.

Drives the whole autonomy loop (PM -> dev -> reviewer -> tester -> merge) with a
grounding-aware scripted gateway and asserts that (a) every grounding pull emits
an ``errorta.grounding`` INFO trace line, (b) the PM and devs actually receive
grounding in their prompts, and (c) durable grounding is written after a merge.
This is the deterministic, no-network core behind
``scripts/simulate_coding_grounding.py``.
"""
from __future__ import annotations

import json
import logging
import re
import sys

from errorta_council.coding.autonomy import (
    CADENCE_OFF,
    DEFINITION_OF_DONE,
    CodingAutonomyPolicy,
)
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import CodingRunner
from errorta_project_grounding.corpus_binding import ProjectCorpusBinding, save_binding

MEMBERS = [
    {"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"}},
    {"id": "m-dev", "enabled": True, "metadata": {"coding_role": "dev"}},
    {"id": "m-rev", "enabled": True, "metadata": {"coding_role": "reviewer"}},
    {"id": "m-test", "enabled": True, "metadata": {"coding_role": "tester"}},
]

_ADD = "def add(a, b):\n    return a + b\n"
_DIVIDE = ("def divide(a, b):\n    if b == 0:\n"
           "        raise ValueError('division by zero')\n    return a / b\n")


def _tid(prompt: str, role: str) -> str:
    return re.search(rf"{role} for task id '([^']+)'", prompt).group(1)


def _head(prompt: str) -> str:
    return re.search(r"PR head you are reviewing is '([^']*)'", prompt).group(1)


def _pm(tasks=None, done=False, summary=""):
    intent = {"kind": "plan", "done": done}
    if tasks is not None:
        intent["tasks"] = tasks
    if summary:
        intent["completion_summary"] = summary
    return json.dumps({"schema_version": "coding_turn.v1", "role": "pm", "intent": intent})


def _dev_code(tid, files):
    return json.dumps({"schema_version": "coding_turn.v1", "role": "dev", "task_id": tid,
                       "intent": {"kind": "tool_plan", "task_type": "implementation",
                                  "tool_calls": [{"tool": "code_write",
                                                  "args": {"path": p, "content": c}}
                                                 for p, c in files]}})


def _dev_ctx(tid):
    return json.dumps({"schema_version": "coding_turn.v1", "role": "dev", "task_id": tid,
                       "intent": {"kind": "context_request", "reason": "missing_api_contract",
                                  "question": "What must divide do on zero?",
                                  "scope": {"corpus_query": "divide by zero",
                                            "sources": ["memory", "corpus"]},
                                  "needed_for": "implementation", "max_items": 4}})


def _rev(tid, head):
    return json.dumps({"schema_version": "coding_turn.v1", "role": "reviewer", "task_id": tid,
                       "intent": {"kind": "review_verdict", "reviewed_head": head,
                                  "approved": True, "findings": []}})


def _tester(tid):
    return json.dumps({"schema_version": "coding_turn.v1", "role": "tester", "task_id": tid,
                       "intent": {"kind": "test_plan", "command_ids": ["unit"],
                                  "scope": "full_project", "rationale": "go"}})


class _Gateway:
    def __init__(self):
        self.pm = 0
        self.asked: set[str] = set()

    def __call__(self, member, prompt):
        if "You are the PM" in prompt:
            self.pm += 1
            if self.pm == 1:
                return _pm(tasks=[{"title": "implement add", "role": "dev"}])
            if self.pm == 2:
                return _pm(tasks=[{"title": "implement divide with zero handling",
                                   "role": "dev"}])
            return _pm(done=True, summary="done")
        if "You are a developer" in prompt:
            tid = _tid(prompt, "developer")
            if "divide with zero handling" in prompt and tid not in self.asked:
                self.asked.add(tid)
                return _dev_ctx(tid)
            if "def add" in prompt:
                return _dev_code(tid, [("calc.py", _ADD + "\n" + _DIVIDE)])
            return _dev_code(tid, [("calc.py", _ADD)])
        if "DELIVERY reviewer" in prompt:
            # F146 Slice B: approve the integrated delivered head so the run
            # completes (delivery tests run deterministically, no fake needed).
            head = re.search(
                r"delivered head you are reviewing is '([^']*)'", prompt).group(1)
            return _rev("delivery-review", head)
        if "You are a reviewer" in prompt:
            return _rev(_tid(prompt, "reviewer"), _head(prompt))
        if "You are a tester" in prompt:
            return _tester(_tid(prompt, "tester"))
        return "{}"


def _run(store: LedgerStore):
    # A bound corpus (no real ingest needed) so the PM boot briefing path runs;
    # local-no-AIAR retrieval degrades to "unavailable", which is itself traced.
    save_binding(store, ProjectCorpusBinding(
        project_id=store.project_id, mode="build_from_repo", corpus_id="sim-corpus",
        source_root="/x", health_state="ready", health_reason="bound"))
    store.set_test_commands({"unit": {
        "argv": [sys.executable, "-c",
                 "import sys; sys.path.insert(0,'.'); from calc import add; assert add(1,2)==3"],
        "cwd": ".", "timeout_seconds": 30}})
    runner = CodingRunner(store.project_id, MEMBERS, _Gateway(), guardrail_enabled=True)
    res = runner.run(CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF, max_iterations=60))
    return runner, res


def test_full_run_traces_grounding_for_pm_and_devs(tmp_errorta_home, caplog):
    caplog.set_level(logging.INFO, logger="errorta.grounding")
    store = LedgerStore("gtrace")
    store.create_project(
        north_star="calculator with add and a safe divide that raises on zero",
        definition_of_done="add and divide work; divide raises on zero; tests green",
        target="new", repo_path=None)
    runner, res = _run(store)

    # the project actually completed through the PR/merge pipeline
    assert res.stop_reason == DEFINITION_OF_DONE
    prs = store.list_prs()
    assert len(prs) == 2 and all(p["status"] == "merged" for p in prs)

    msgs = [r.getMessage() for r in caplog.records if r.name == "errorta.grounding"]
    blob = "\n".join(msgs)
    # every grounding-consumption point is traced
    assert "grounding pm-boot:" in blob                  # F088-08 PM boot briefing
    assert "grounding packet:" in blob                   # F088-07 role packets
    assert "grounding context-request:" in blob          # F088-09 dev context request
    assert "grounding sync:" in blob                     # post-merge memory write
    # the PM boot trace names the corpus retrieval status (here: unavailable, no AIAR)
    assert any("pm-boot:" in m and "corpus_status=" in m for m in msgs)
    # role packets fired for more than one role across the run
    roles = {m.split("role=")[1].split()[0] for m in msgs if "grounding packet:" in m}
    assert {"pm", "dev"} <= roles


def test_grounding_reaches_the_prompts_and_memory(tmp_errorta_home):
    store = LedgerStore("gprompt")
    store.create_project(
        north_star="calculator with add and a safe divide that raises on zero",
        definition_of_done="add and divide work; divide raises on zero; tests green",
        target="new", repo_path=None)
    _run(store)

    prompts = [t.get("prompt", "") for t in store.list_turns()]
    joined = "\n".join(prompts)
    # the actual grounding artifacts are present in member prompts
    assert "PM boot briefing" in joined
    assert "Project grounding context packet" in joined
    assert "Context response to YOUR earlier request" in joined

    # a developer issued exactly one read-only context request, answered from memory
    ctx = [d for d in store.list_decisions() if d.get("choice") == "context_request"]
    assert len(ctx) == 1
    answer = ctx[0]["context_response"]
    assert answer["schema_version"] == "context_response.v1"
    assert len(answer["memory"]) > 0           # durable truth from PR1 fed the dev

    # durable grounding was written to the project memory index after merges
    from errorta_project_grounding.memory_store import MemoryQuery, ProjectMemoryStore
    mem = ProjectMemoryStore(store.project_id, root=store.dir.parent)
    durable = mem.query(MemoryQuery(authorities=("durable_truth",), limit=200))
    assert durable and {"code_chunk", "test_evidence", "merge_episode"} & {
        i.source_type for i in durable}
