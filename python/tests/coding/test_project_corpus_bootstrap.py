from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_project_grounding.bootstrap import (
    CODE_EXTENSIONS,
    load_job,
    plan_project_bootstrap,
    start_project_bootstrap,
)
from errorta_project_grounding.corpus_binding import load_binding


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".gitignore").write_text("ignored.md\nbuild/\n", encoding="utf-8")
    (root / "README.md").write_text("# ok\n", encoding="utf-8")
    (root / "ignored.md").write_text("no\n", encoding="utf-8")
    (root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (root / "build").mkdir()
    (root / "build" / "out.md").write_text("generated\n", encoding="utf-8")
    (root / "image.bin").write_bytes(b"\x00\x01")
    return root


def test_bootstrap_plan_excludes_sensitive_generated_and_ignored_files(tmp_path: Path) -> None:
    plan = plan_project_bootstrap(_repo(tmp_path))

    assert plan.included == ("README.md",)
    assert plan.skipped["ignored.md"] == "gitignored"
    # consolidated safe-index policy reports a single "denied_path" reason
    assert plan.skipped[".env"] == "denied_path"
    assert plan.skipped["build/out.md"] == "denied_path"
    assert plan.skipped["image.bin"] == "unsupported_extension"


def test_bootstrap_plan_can_opt_into_source_files(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    default_plan = plan_project_bootstrap(repo)
    source_plan = plan_project_bootstrap(repo, extra_extensions=CODE_EXTENSIONS)

    assert default_plan.skipped["src/app.py"] == "unsupported_extension"
    assert "src/app.py" in source_plan.included


def test_start_bootstrap_persists_job_and_binding(
    tmp_path: Path,
    tmp_errorta_home: Path,
    isolated_manifest_locks,
) -> None:
    repo = _repo(tmp_path)
    store = LedgerStore("p", root=tmp_path / "ledgers")
    store.create_project(
        north_star="n",
        definition_of_done="d",
        target="existing",
        repo_path=str(repo),
    )

    job = start_project_bootstrap(store, corpus_id="project-p", source_root=repo)

    assert job.status == "done"
    assert len(job.enqueued) == 1
    assert load_job(store, job.job_id) is not None
    binding = load_binding(store)
    assert binding.mode == "build_from_repo"
    assert binding.corpus_id == "project-p"
    assert binding.health_state == "indexing"
