"""F010 planner tests — pure planning, no copying."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from errorta_export import ExportPlan, planner


def _build_fake_corpus(
    errorta_home: Path,
    corpus_name: str,
    files: list[tuple[str, bytes]],
) -> dict[str, dict]:
    corpus_dir = errorta_home / "corpora" / corpus_name
    files_dir = corpus_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    entries: dict[str, dict] = {}
    for i, (filename, content) in enumerate(files):
        p = files_dir / filename
        p.write_bytes(content)
        fid = f"f{i:03d}"
        entries[fid] = {
            "file_id": fid,
            "original_path": str(p),
            "copied_path": str(p),
            "sha256": "deadbeef" * 8,
            "size_bytes": len(content),
            "mime_ext": filename.split(".")[-1],
            "status": "ready",
        }

    manifest = {"name": corpus_name, "files": entries}
    (corpus_dir / "manifest.json").write_text(json.dumps(manifest))
    return entries


def test_planner_basic_two_files(tmp_path: Path) -> None:
    home = tmp_path / ".errorta"
    target = tmp_path / "usb"
    entries = _build_fake_corpus(
        home,
        "test-corpus",
        [("a.pdf", b"A" * 100), ("b.txt", b"B" * 250)],
    )

    plan = planner(target_dir=target, corpora_list=["test-corpus"], errorta_home=home)

    assert isinstance(plan, ExportPlan)
    assert len(plan.files) == 2
    expected_total = sum(e["size_bytes"] for e in entries.values())
    assert plan.total_size_bytes == expected_total
    assert plan.corpora_included == ["test-corpus"]

    expected_root = target / "Errorta" / "corpora" / "test-corpus" / "files"
    for ef in plan.files:
        assert ef.dest_path.parent == expected_root

    # No files copied: target dir should not exist yet.
    assert not target.exists()


def test_planner_include_models_raises(tmp_path: Path) -> None:
    home = tmp_path / ".errorta"
    _build_fake_corpus(home, "test-corpus", [("a.pdf", b"x")])
    with pytest.raises(NotImplementedError):
        planner(
            target_dir=tmp_path / "usb",
            corpora_list=["test-corpus"],
            errorta_home=home,
            include_models=True,
        )


def test_planner_unknown_corpus_raises(tmp_path: Path) -> None:
    home = tmp_path / ".errorta"
    home.mkdir()
    with pytest.raises(FileNotFoundError):
        planner(
            target_dir=tmp_path / "usb",
            corpora_list=["does-not-exist"],
            errorta_home=home,
        )
