"""F088-06 — rebuild/repair: reconstruct the index from the ledger or the repo."""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.workspace import CodingWorkspace
from errorta_project_grounding.memory_store import MemoryQuery, ProjectMemoryStore
from errorta_project_grounding.update_pipeline import (
    rebuild_from_ledger,
    rebuild_from_repo,
    sync_from_ledger,
)


class _Pass:
    command_ids = ["unit"]
    results: list = []
    unknown_ids: list = []
    passed = True
    sandbox = "seatbelt"


def _project(tmp: Path, pid: str):
    s = LedgerStore(pid, root=tmp)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    ws = CodingWorkspace(pid, s)
    ws.setup(target="new", repo_path=None)
    return s, ws


def _merge_pr(s, ws, *, title: str, files: dict[str, str]):
    t = s.add_task(title=title, role="dev")
    branch = ws.start_task_branch(t.task_id)
    for path, content in files.items():
        ws.write_file(path, content, task_id=t.task_id)
    head = ws.head()
    pr = s.record_pr(task_id=t.task_id, branch=branch, head=head, dev_member="m-dev")
    s.record_decision(title="review", context="c", choice="review_approved",
                      rationale="ok", extra={"reviewed_head": head, "pr_id": pr["pr_id"]})
    s.record_test_run(_Pass(), task_id=t.task_id, head=head)
    res = ws.merge_pr(branch)
    s.update_pr(pr["pr_id"], status="merged", head=res.get("head", head),
                reviewer_approved=True, reviewed_head=head,
                tests_passed=True, tested_head=head)
    s.record_episode(title=f"merged {branch}", summary=f"merged {title}",
                     head=res.get("head", head), related_task_ids=[t.task_id])


def _durable_count(tmp: Path, pid: str) -> int:
    return len(ProjectMemoryStore(pid, root=tmp).query(
        MemoryQuery(authorities=("durable_truth",), limit=500)))


def test_rebuild_from_ledger_is_idempotent_reconstruction(tmp_path: Path) -> None:
    s, ws = _project(tmp_path, "r1")
    s.record_decision(title="d", context="pm_decision", choice="pm_decision", rationale="r")
    _merge_pr(s, ws, title="impl", files={"calc.py": "x = 1\n"})
    sync_from_ledger(s, workspace=ws)
    baseline = _durable_count(tmp_path, "r1")
    assert baseline >= 3  # pm decision + chunk + test evidence + episode

    out = rebuild_from_ledger(s, workspace=ws)
    assert isinstance(out, dict)
    assert _durable_count(tmp_path, "r1") == baseline  # idempotent


def test_rebuild_repairs_a_wiped_store(tmp_path: Path) -> None:
    s, ws = _project(tmp_path, "r2")
    s.record_decision(title="d", context="pm_decision", choice="pm_decision", rationale="r")
    _merge_pr(s, ws, title="impl", files={"calc.py": "x = 1\n"})
    sync_from_ledger(s, workspace=ws)
    db = s.dir / "grounding" / "memory.sqlite3"
    assert db.exists()
    db.unlink()  # simulate a lost/corrupt index

    rebuild_from_ledger(s, workspace=ws)
    assert _durable_count(tmp_path, "r2") >= 3  # reconstructed from the ledger alone


def test_rebuild_from_repo_anchors_master_files(tmp_path: Path) -> None:
    s, ws = _project(tmp_path, "r3")
    _merge_pr(s, ws, title="impl", files={"calc.py": "def add(a, b):\n    return a + b\n"})
    out = rebuild_from_repo(s, ws)
    assert out["status"] == "ok"
    assert out["anchored"] >= 1  # master files anchored even without AIAR
    paths = {i.source_ref.path for i in ProjectMemoryStore("r3", root=tmp_path).query(
        MemoryQuery(authorities=("durable_truth",), source_type="code_chunk", limit=500))}
    assert "calc.py" in paths
    assert ".gitignore" not in paths  # hidden/denied files are never anchored


def test_rebuild_from_repo_skips_secret_content(tmp_path: Path) -> None:
    s, ws = _project(tmp_path, "r4")
    _merge_pr(
        s,
        ws,
        title="impl",
        files={
            "safe.py": "def ok():\n    return True\n",
            "config.py": "API_SECRET='sk-abcdefghijklmnopqrstuvwxyz1234567890'\n",
        },
    )

    out = rebuild_from_repo(s, ws)

    assert out["status"] == "ok"
    paths = {
        i.source_ref.path
        for i in ProjectMemoryStore("r4", root=tmp_path).query(
            MemoryQuery(authorities=("durable_truth",), source_type="code_chunk", limit=500)
        )
    }
    assert "safe.py" in paths
    assert "config.py" not in paths


def test_rebuild_from_repo_sends_relative_source_metadata(tmp_path: Path) -> None:
    s, ws = _project(tmp_path, "r5")
    _merge_pr(
        s,
        ws,
        title="impl",
        files={
            "docs/readme.md": "# docs\n",
            "src/readme.md": "# src\n",
        },
    )
    from errorta_project_grounding.corpus_binding import ProjectCorpusBinding, save_binding

    save_binding(
        s,
        ProjectCorpusBinding(
            project_id="r5",
            mode="build_from_project",
            corpus_id="project-r5",
            adapter_source="remote",
            health_state="ready",
        ),
    )

    seen: list[dict] = []

    class _Adapter:
        def ingest_file(self, *, corpus_id, path, metadata):
            seen.append({"corpus_id": corpus_id, "path": path.name, "metadata": metadata})

    out = rebuild_from_repo(s, ws, adapter=_Adapter())

    assert out["status"] == "ok"
    sources = {item["metadata"].get("path") for item in seen}
    assert {"docs/readme.md", "src/readme.md"} <= sources
    assert all(not str(item["metadata"].get("path")).startswith("/") for item in seen)
