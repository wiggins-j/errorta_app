"""F087-16 — verbose per-turn run transcript + plaintext run-log export."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import CodingRunner, members_by_coding_role

MEMBERS = [
    {"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"}},
    {"id": "m-dev", "enabled": True, "metadata": {"coding_role": "dev"}},
    {"id": "m-rev", "enabled": True, "metadata": {"coding_role": "reviewer"}},
    {"id": "m-test", "enabled": True, "metadata": {"coding_role": "tester"}},
]


def test_record_turn_persists_verbatim(tmp_path: Path) -> None:
    s = LedgerStore("trn", root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    s.record_turn(role="pm", member_id="m-pm", task_id="plan",
                  prompt="PROMPT_SENTINEL", response="RESPONSE_SENTINEL",
                  outcome="planned", duration_ms=12)
    turns = s.list_turns()
    assert turns[0]["prompt"] == "PROMPT_SENTINEL"
    assert turns[0]["response"] == "RESPONSE_SENTINEL"
    assert turns[0]["outcome"] == "planned" and turns[0]["role"] == "pm"


def test_record_turn_caps_huge_fields(tmp_path: Path) -> None:
    s = LedgerStore("trncap", root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    big = "x" * 50_000
    rec = s.record_turn(role="dev", member_id="m", task_id="t", prompt=big,
                        response=big, outcome="task_done")
    assert len(rec["prompt"]) <= 20_000 and len(rec["response"]) <= 20_000


def _full_run(pid: str, *, root: Path | None = None):
    s = LedgerStore(pid, root=root)
    s.create_project(north_star="add()", definition_of_done="tested",
                     target="new", repo_path=None)
    s.set_test_commands({"unit": {
        "argv": [sys.executable, "-c", "import sys; sys.path.insert(0,'.'); "
                 "from add import add; assert add(1,2)==3"],
        "cwd": ".", "timeout_seconds": 30}})

    class Fake:
        def __init__(self): self.pm = 0
        def __call__(self, member, prompt):
            import re
            if "You are the PM" in prompt:
                self.pm += 1
                if self.pm == 1:
                    return json.dumps({"schema_version": "coding_turn.v1", "role": "pm",
                        "intent": {"kind": "plan", "done": False,
                                   "tasks": [{"title": "impl add", "role": "dev"}]}})
                return json.dumps({"schema_version": "coding_turn.v1", "role": "pm",
                    "intent": {"kind": "plan", "done": True, "completion_summary": "done"}})
            if "You are a developer" in prompt:
                tid = re.search(r"developer for task id '([^']+)'", prompt).group(1)
                return json.dumps({"schema_version": "coding_turn.v1", "role": "dev",
                    "task_id": tid, "intent": {"kind": "tool_plan",
                    "task_type": "implementation", "tool_calls": [
                        {"tool": "code_write", "args": {"path": "add.py",
                         "content": "def add(a,b):\n return a+b\n"}},
                        {"tool": "code_write", "args": {"path": "test_add.py",
                         "content": "from add import add\n\ndef test_add():\n assert add(1,2)==3\n"}}]}})
            if "You are a reviewer" in prompt:
                tid = re.search(r"reviewer for task id '([^']+)'", prompt).group(1)
                head = re.search(r"PR head you are reviewing is '([^']*)'", prompt).group(1)
                return json.dumps({"schema_version": "coding_turn.v1", "role": "reviewer",
                    "task_id": tid, "intent": {"kind": "review_verdict",
                    "reviewed_head": head, "approved": True, "findings": []}})
            if "You are a tester" in prompt:
                tid = re.search(r"tester for task id '([^']+)'", prompt).group(1)
                return json.dumps({"schema_version": "coding_turn.v1", "role": "tester",
                    "task_id": tid, "intent": {"kind": "test_plan",
                    "command_ids": ["unit"], "scope": "full_project", "rationale": "x"}})
            return "{}"

    from errorta_council.coding.autonomy import CodingAutonomyPolicy, CADENCE_OFF
    runner = CodingRunner(pid, MEMBERS, Fake(), root=root, guardrail_enabled=True)
    runner.run(CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF, max_iterations=50))
    return s


def test_full_run_records_every_member_turn(tmp_errorta_home: Path) -> None:
    s = _full_run("trnrun")
    turns = s.list_turns()
    roles = {t["role"] for t in turns}
    assert {"pm", "dev", "reviewer", "tester"} <= roles
    # every recorded turn carries a verbatim prompt + a raw response
    assert all(t["prompt"] and t["response"] for t in turns)
    # the dev turn's raw response is the actual JSON the model emitted
    dev = [t for t in turns if t["role"] == "dev"][0]
    assert "code_write" in dev["response"]


def test_run_log_txt_export(tmp_errorta_home: Path) -> None:
    s = _full_run("trnlog")
    app = FastAPI()
    from errorta_app.routes import coding as coding_routes
    app.include_router(coding_routes.router)
    c = TestClient(app, headers={"x-errorta-origin": "tauri-ui"})
    r = c.get("/coding/projects/trnlog/run-log.txt")
    assert r.status_code == 200
    body = r.text
    assert "CODING TEAM RUN LOG" in body
    assert "TURN-BY-TURN TRANSCRIPT" in body
    assert "PROMPT:" in body and "RESPONSE:" in body
    assert "TEST RUNS" in body and "FILES TOUCHED" in body
    assert "add.py" in body
