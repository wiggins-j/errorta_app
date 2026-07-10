"""F088 — end-to-end simulation of the coding console + AIAR grounding.

Drives a WHOLE autonomous coding project (PM plans -> devs implement -> reviewer
approves -> tester runs REAL tests -> PM merges) and proves the PM and the
developers actually consume grounding (the F088-08 PM boot briefing, the
F088-07 role context packets, and a F088-09 developer context request that
queries the corpus + project memory).

Everything is written to ONE log file at DEBUG so the full run is traceable:
every turn, every grounding pull (role packet / pm-boot / context-request /
corpus retrieve / memory sync), every PR/merge. The log path is printed at the
top and bottom of the run.

Two modes (member turns are scripted either way — deterministic, no live model
needed; the GROUNDING path underneath is the real code):

  local   (default)  no network. Project memory grounding is fully live; corpus
                     retrieval is live IFF a real AIAR is importable in this venv
                     (else it degrades honestly to "unavailable", which the run
                     reports — the plumbing is still exercised end to end).

  remote             corpus is owned by a remote AIAR (the watchdog). Requires
                     ERRORTA_AIAR_REMOTE_URL (+ token via ERRORTA_AIAR_REMOTE_TOKEN
                     and an SSH tunnel). The PM boot briefing + dev context
                     request pull REAL corpus evidence from AIAR.

Usage:
  python scripts/simulate_coding_grounding.py                 # local
  python scripts/simulate_coding_grounding.py --mode remote   # vs watchdog AIAR
  python scripts/simulate_coding_grounding.py --home /tmp/sim --keep

Run from the `python/` dir (or with it on PYTHONPATH).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

# --- isolate state BEFORE importing any errorta module (paths read the env) ---
_ARGS_HOME = None
for i, a in enumerate(sys.argv):
    if a == "--home" and i + 1 < len(sys.argv):
        _ARGS_HOME = sys.argv[i + 1]
HOME = Path(_ARGS_HOME).expanduser() if _ARGS_HOME else Path(
    tempfile.mkdtemp(prefix="errorta-sim-"))
HOME.mkdir(parents=True, exist_ok=True)
os.environ["ERRORTA_HOME"] = str(HOME)

LOG_DIR = HOME / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / "coding-sim.log"


def _setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    root.addHandler(ch)


_setup_logging()
LOG = logging.getLogger("sim")

from errorta_council.coding.autonomy import (  # noqa: E402
    CADENCE_OFF,
    DEFINITION_OF_DONE,
    CodingAutonomyPolicy,
)
from errorta_council.coding.ledger import LedgerStore  # noqa: E402
from errorta_council.coding.runner import CodingRunner  # noqa: E402
from errorta_project_grounding.bootstrap import start_project_bootstrap  # noqa: E402
from errorta_project_grounding.corpus_binding import load_binding  # noqa: E402

PROJECT_ID = "f088-sim"
CORPUS_ID = "errorta-grounding-sim"

# --- scripted member turns (grounding-aware) --------------------------------
import re  # noqa: E402

_ADD = "def add(a, b):\n    return a + b\n"
_DIVIDE = (
    "def divide(a, b):\n"
    "    if b == 0:\n"
    "        raise ValueError('division by zero')\n"
    "    return a / b\n"
)


def _task_id(prompt: str, role: str) -> str:
    return re.search(rf"{role} for task id '([^']+)'", prompt).group(1)


def _pr_head(prompt: str) -> str:
    return re.search(r"PR head you are reviewing is '([^']*)'", prompt).group(1)


def _pm(tasks=None, done=False, summary="") -> str:
    intent = {"kind": "plan", "done": done}
    if tasks is not None:
        intent["tasks"] = tasks
    if summary:
        intent["completion_summary"] = summary
    return json.dumps({"schema_version": "coding_turn.v1", "role": "pm", "intent": intent})


def _dev_code(task_id: str, files) -> str:
    return json.dumps({
        "schema_version": "coding_turn.v1", "role": "dev", "task_id": task_id,
        "intent": {"kind": "tool_plan", "task_type": "implementation",
                   "tool_calls": [{"tool": "code_write", "args": {"path": p, "content": c}}
                                  for p, c in files]}})


def _dev_context_request(task_id: str, question: str, corpus_query: str) -> str:
    return json.dumps({
        "schema_version": "coding_turn.v1", "role": "dev", "task_id": task_id,
        "intent": {"kind": "context_request", "reason": "missing_api_contract",
                   "question": question,
                   "scope": {"corpus_query": corpus_query, "sources": ["memory", "corpus"]},
                   "needed_for": "implementation", "max_items": 4}})


def _rev(task_id: str, head: str) -> str:
    return json.dumps({
        "schema_version": "coding_turn.v1", "role": "reviewer", "task_id": task_id,
        "intent": {"kind": "review_verdict", "reviewed_head": head, "approved": True,
                   "findings": []}})


def _tester(task_id: str, command_ids) -> str:
    return json.dumps({
        "schema_version": "coding_turn.v1", "role": "tester", "task_id": task_id,
        "intent": {"kind": "test_plan", "command_ids": command_ids,
                   "scope": "full_project", "rationale": "validate"}})


class GroundingAwareGateway:
    """PM plans add() then divide(); the dev building divide() FIRST issues a
    read-only context request (exercising grounding retrieval + memory), is
    requeued, then implements; reviewer approves; tester runs the real unit
    command. Tracks which dev tasks already asked so it asks exactly once."""

    def __init__(self) -> None:
        self.pm_calls = 0
        self.asked: set[str] = set()

    def __call__(self, member: dict, prompt: str) -> str:
        if "You are the PM" in prompt:
            self.pm_calls += 1
            if self.pm_calls == 1:
                return _pm(tasks=[{"title": "implement add", "role": "dev"}])
            if self.pm_calls == 2:
                return _pm(tasks=[{"title": "implement divide with zero handling",
                                   "role": "dev"}])
            return _pm(done=True, summary="add + divide implemented and tested")
        if "You are a developer" in prompt:
            tid = _task_id(prompt, "developer")
            # The divide task asks the corpus/memory what divide-by-zero should do
            # before writing code — demonstrates a developer USING grounding.
            # Scope to the divide task by its title (not the North Star text that
            # appears in every prompt) so exactly one demonstrative ask fires.
            if "divide with zero handling" in prompt and tid not in self.asked:
                self.asked.add(tid)
                return _dev_context_request(
                    tid, "What must divide do on division by zero?",
                    "divide by zero behavior")
            if "def add" in prompt:  # read-back shows add() already merged -> extend
                return _dev_code(tid, [("calc.py", _ADD + "\n" + _DIVIDE)])
            return _dev_code(tid, [("calc.py", _ADD)])
        if "You are a reviewer" in prompt:
            return _rev(_task_id(prompt, "reviewer"), _pr_head(prompt))
        if "You are a tester" in prompt:
            return _tester(_task_id(prompt, "tester"), ["unit"])
        return "{}"


# --- grounding liveness probe -----------------------------------------------


def _corpus_grounding_live(mode: str) -> tuple[bool, str]:
    if mode == "remote":
        try:
            from errorta_project_grounding.remote_adapter import active_remote_adapter
            a = active_remote_adapter()
            if a is None:
                return False, "remote: ERRORTA_AIAR_REMOTE_URL not set"
            caps = a.capabilities()
            return bool(caps.available), f"remote: available={caps.available}"
        except Exception as exc:
            return False, f"remote: probe error {exc}"
    try:
        from errorta_query.pipeline import default_pipeline
        name = type(default_pipeline()).__name__
        return name != "StubPipeline", f"local: pipeline={name}"
    except Exception as exc:
        return False, f"local: probe error {exc}"


def _wait_for_corpus(store: LedgerStore, *, live: bool, timeout: float = 60.0) -> None:
    """When corpus retrieval is live, give async local ingest time to chunk+embed
    so the first PM-boot retrieval sees evidence. No-op when not live (remote
    ingest is synchronous; stub local has nothing to wait for)."""
    if not live:
        return
    binding = load_binding(store)
    if binding.adapter_source == "remote":
        return  # remote bootstrap already published synchronously
    from errorta_corpus.manifest import load_manifest
    deadline = time.time() + timeout
    while time.time() < deadline:
        manifest = load_manifest(CORPUS_ID)
        if manifest and all(e.status in ("ready", "failed", "removed")
                            for e in manifest.values()):
            LOG.info("corpus ingest settled: %s",
                     {fid: e.status for fid, e in manifest.items()})
            return
        time.sleep(1.0)
    LOG.warning("corpus ingest did not settle within %.0fs (continuing)", timeout)


# --- the sample repo the corpus is built from -------------------------------


def _write_sample_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text(
        "# Calculator service\n\n"
        "A tiny calculator. The public API is `add(a, b)` and `divide(a, b)`.\n"
        "`add` returns the sum of two numbers.\n", encoding="utf-8")
    (root / "API.md").write_text(
        "# API contract\n\n"
        "## divide(a, b)\n"
        "Returns a / b. On division by zero the divide function MUST raise a "
        "ValueError with the message 'division by zero'. It must never return "
        "infinity or NaN.\n\n"
        "## add(a, b)\n"
        "Returns a + b.\n", encoding="utf-8")
    (root / "notes.txt").write_text(
        "Design note: all error conditions surface as Python exceptions, never "
        "sentinel return values.\n", encoding="utf-8")


# --- verification + trace ----------------------------------------------------


def _grounding_markers(prompt: str) -> list[str]:
    out = []
    if "PM boot briefing" in prompt:
        out.append("pm_boot")
    if "Project grounding context packet" in prompt:
        out.append("role_packet")
    if "Context response to YOUR earlier request" in prompt:
        out.append("context_response")
    return out


def _print_turn_trace(store: LedgerStore) -> dict:
    LOG.info("================ PER-TURN GROUNDING TRACE ================")
    seen = {"pm_boot": 0, "role_packet": 0, "context_response": 0}
    for t in store.list_turns():
        markers = _grounding_markers(t.get("prompt", ""))
        for m in markers:
            seen[m] += 1
        LOG.info("turn role=%-8s task=%-14s outcome=%-12s grounding=%s",
                 t.get("role"), t.get("task_id"), t.get("outcome"),
                 ",".join(markers) or "-")
    return seen


def _memory_summary(store: LedgerStore) -> dict:
    try:
        from errorta_project_grounding.memory_store import MemoryQuery, ProjectMemoryStore
    except Exception:
        return {}
    db = store.dir / "grounding" / "memory.sqlite3"
    if not db.exists():
        return {}
    mem = ProjectMemoryStore(store.project_id, root=store.dir.parent)
    durable = mem.query(MemoryQuery(authorities=("durable_truth",), limit=500))
    wip = mem.query(MemoryQuery(authorities=("wip",), limit=500))
    by_durable: dict[str, int] = {}
    for it in durable:
        by_durable[it.source_type] = by_durable.get(it.source_type, 0) + 1
    by_wip: dict[str, int] = {}
    for it in wip:
        by_wip[it.source_type] = by_wip.get(it.source_type, 0) + 1
    LOG.info("================ PROJECT MEMORY (grounding index) ================")
    LOG.info("durable_truth rows by source_type: %s", by_durable)
    LOG.info("wip rows by source_type: %s", by_wip)
    return {"durable": by_durable, "wip": by_wip,
            "durable_total": len(durable), "wip_total": len(wip)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["local", "remote"], default="local")
    ap.add_argument("--home", default=None)  # parsed pre-import; here for --help
    ap.add_argument("--keep", action="store_true", help="keep ERRORTA_HOME after run")
    args = ap.parse_args()

    LOG.info("########## F088 coding+grounding simulation (mode=%s) ##########", args.mode)
    LOG.info("ERRORTA_HOME = %s", HOME)
    LOG.info("FULL LOG     = %s   (tail -f to watch)", LOG_PATH)

    live, why = _corpus_grounding_live(args.mode)
    LOG.info("corpus grounding live = %s (%s)", live, why)
    if args.mode == "remote" and not live:
        LOG.error("remote mode requires a reachable AIAR — aborting. %s", why)
        return 2

    repo = HOME / "sample-repo"
    _write_sample_repo(repo)

    store = LedgerStore(PROJECT_ID)
    store.create_project(
        north_star="A calculator with add() and a safe divide() that raises on zero",
        definition_of_done=("add and divide implemented; divide raises ValueError "
                             "on zero; tests green"),
        target="new", repo_path=None)
    store.set_test_commands({"unit": {
        "argv": [sys.executable, "-c",
                 "import sys; sys.path.insert(0,'.'); from calc import add; assert add(1,2)==3"],
        "cwd": ".", "timeout_seconds": 30, "label": "unit"}})

    LOG.info("---- bootstrapping corpus '%s' from %s ----", CORPUS_ID, repo)
    job = start_project_bootstrap(store, corpus_id=CORPUS_ID, source_root=repo)
    LOG.info("bootstrap: status=%s adapter=%s docs=%s chunks=%s enqueued=%s errors=%s",
             job.status, job.adapter_source, job.documents_ingested, job.chunks_added,
             len(job.enqueued), list(job.errors)[:3])
    binding = load_binding(store)
    LOG.info("binding: corpus=%s mode=%s health=%s (%s) adapter=%s",
             binding.corpus_id, binding.mode, binding.health_state,
             binding.health_reason, binding.adapter_source)
    _wait_for_corpus(store, live=live)

    LOG.info("---- running the autonomous coding team ----")
    members = [
        {"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"}},
        {"id": "m-dev", "enabled": True, "metadata": {"coding_role": "dev"}},
        {"id": "m-rev", "enabled": True, "metadata": {"coding_role": "reviewer"}},
        {"id": "m-test", "enabled": True, "metadata": {"coding_role": "tester"}},
    ]
    runner = CodingRunner(PROJECT_ID, members, GroundingAwareGateway(), guardrail_enabled=True)
    result = runner.run(CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF, max_iterations=60))
    LOG.info("loop finished: stop_reason=%s iterations=%s",
             result.stop_reason, getattr(result, "iterations", "?"))

    # --- observe ---
    seen = _print_turn_trace(store)
    mem = _memory_summary(store)
    prs = store.list_prs()
    decisions = store.list_decisions()
    ctx_reqs = [d for d in decisions if d.get("choice") == "context_request"]
    LOG.info("================ LEDGER ================")
    LOG.info("PRs: %d (merged=%d)", len(prs),
             sum(1 for p in prs if p.get("status") == "merged"))
    LOG.info("context requests answered: %d", len(ctx_reqs))
    for d in ctx_reqs:
        cr = d.get("context_response") or {}
        LOG.info("  ctx-request: q=%r corpus_evidence=%d memory=%d",
                 (cr.get("question") or "")[:60], len(cr.get("corpus_evidence") or []),
                 len(cr.get("memory") or []))

    # final master state
    final = ""
    try:
        runner.workspace.checkout("master")
        final = runner.workspace._ws.read_file("calc.py")
    except Exception as exc:
        LOG.warning("could not read master calc.py: %s", exc)

    # --- assertions ---
    checks: list[tuple[str, bool]] = []
    checks.append(("loop reached definition_of_done", result.stop_reason == DEFINITION_OF_DONE))
    checks.append(("two PRs, all merged",
                   len(prs) == 2 and all(p.get("status") == "merged" for p in prs)))
    checks.append(("master accumulated add() and divide()",
                   "def add" in final and "def divide" in final))
    checks.append(("PM received a boot briefing (grounding)", seen["pm_boot"] >= 1))
    checks.append(("a role grounding packet was injected", seen["role_packet"] >= 1))
    checks.append(("dev issued a context request", len(ctx_reqs) >= 1))
    checks.append(("context response delivered back to dev", seen["context_response"] >= 1))
    checks.append(("durable_truth grounding written after merge",
                   mem.get("durable_total", 0) > 0))
    checks.append(("a tester verdict came from a real run",
                   any(r.get("passed") for r in store.list_test_runs())))
    if live:
        ctx_corpus = sum(len((d.get("context_response") or {}).get("corpus_evidence") or [])
                         for d in ctx_reqs)
        checks.append(("LIVE corpus evidence reached a grounding consumer",
                       ctx_corpus > 0))

    LOG.info("================ RESULT ================")
    ok = True
    for name, passed in checks:
        LOG.info("[%s] %s", "PASS" if passed else "FAIL", name)
        ok = ok and passed
    LOG.info("corpus grounding was %s this run (%s)",
             "LIVE" if live else "NOT live (memory grounding only)", why)
    LOG.info("FULL LOG: %s", LOG_PATH)
    LOG.info("OVERALL: %s", "PASS" if ok else "FAIL")

    if not args.keep and not _ARGS_HOME:
        LOG.info("(ephemeral ERRORTA_HOME left at %s — pass --keep to retain across runs)", HOME)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
