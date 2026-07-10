"""F010 manifest writer tests."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from errorta_export import ExportFile, ExportPlan, write_export_manifest


def test_write_export_manifest_schema(tmp_path: Path) -> None:
    target = tmp_path / "usb"
    target.mkdir()
    files_root = target / "Errorta" / "corpora" / "test-corpus" / "files"

    plan = ExportPlan(
        files=[
            ExportFile(
                src_path=Path("/src/a.pdf"),
                dest_path=files_root / "a.pdf",
                size_bytes=100,
                sha256_hex="a" * 64,
            ),
            ExportFile(
                src_path=Path("/src/b.txt"),
                dest_path=files_root / "b.txt",
                size_bytes=250,
                sha256_hex="b" * 64,
            ),
        ],
        total_size_bytes=350,
        dest_paths={"test-corpus": files_root},
        corpora_included=["test-corpus"],
    )

    out = write_export_manifest(target, plan)
    assert out == target / "export-manifest.json"
    assert out.exists()

    payload = json.loads(out.read_text())
    assert payload["version"] == "1"
    assert payload["total_size_bytes"] == 350
    assert payload["file_count"] == 2
    assert payload["corpora"] == ["test-corpus"]

    # exported_at parses as ISO8601
    parsed = datetime.fromisoformat(payload["exported_at"])
    assert parsed.tzinfo is not None

    files = payload["files"]
    assert "Errorta/corpora/test-corpus/files/a.pdf" in files
    assert "Errorta/corpora/test-corpus/files/b.txt" in files
    a = files["Errorta/corpora/test-corpus/files/a.pdf"]
    assert a["sha256"] == "a" * 64
    assert a["size_bytes"] == 100
    assert a["original_path"] == "/src/a.pdf"
