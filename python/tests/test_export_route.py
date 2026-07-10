"""F010 — /export route smoke tests.

Hermetic: redirects HOME to tmp via ``tmp_errorta_home``, builds a fake corpus
under ``$HOME/.errorta/corpora/...``, and drives /export/plan + /export/run
through ``fastapi.testclient.TestClient``.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _build_fake_corpus(
    errorta_home: Path,
    corpus_name: str,
    files: list[tuple[str, bytes]],
    *,
    record_real_sha: bool = True,
) -> None:
    corpus_dir = errorta_home / "corpora" / corpus_name
    files_dir = corpus_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    entries: dict[str, dict] = {}
    for i, (name, content) in enumerate(files):
        p = files_dir / name
        p.write_bytes(content)
        fid = f"f{i:03d}"
        sha = _sha256_bytes(content) if record_real_sha else "deadbeef" * 8
        entries[fid] = {
            "file_id": fid,
            "original_path": str(p),
            "copied_path": str(p),
            "sha256": sha,
            "size_bytes": len(content),
            "mime_ext": name.split(".")[-1],
            "status": "ready",
        }
    (corpus_dir / "manifest.json").write_text(
        json.dumps({"name": corpus_name, "files": entries})
    )


@pytest.fixture
def client(tmp_errorta_home: Path) -> Iterator[TestClient]:
    # Import after HOME has been redirected so any defaults inside the
    # planner that read ~/.errorta land inside tmp.
    from errorta_app.server import app

    with TestClient(app) as c:
        yield c


def _parse_sse(text: str) -> list[dict]:
    """Parse SSE frames into a list of payloads. ``hello`` frame returns {}."""
    out: list[dict] = []
    for raw in text.split("\n\n"):
        block = raw.strip()
        if not block:
            continue
        is_hello = False
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event: hello"):
                is_hello = True
            elif line.startswith("data: "):
                data_lines.append(line[len("data: "):])
        if not data_lines:
            continue
        payload_text = "\n".join(data_lines)
        try:
            parsed = json.loads(payload_text)
        except json.JSONDecodeError:
            continue
        if is_hello and parsed == {}:
            out.append({"event": "hello"})
        else:
            out.append(parsed)
    return out


# ---------------------------------------------------------------------------
# /export/plan
# ---------------------------------------------------------------------------


def test_plan_returns_counts_without_touching_dest(
    client: TestClient, tmp_errorta_home: Path, tmp_path: Path
) -> None:
    _build_fake_corpus(
        tmp_errorta_home / ".errorta",
        "alpha",
        [("a.pdf", b"A" * 100), ("b.txt", b"BB" * 250)],
    )
    target = tmp_path / "usb-out"
    assert not target.exists()

    r = client.post(
        "/export/plan",
        json={"target_dir": str(target), "corpora_list": ["alpha"]},
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["files_count"] == 2
    assert payload["total_size_bytes"] == 100 + 500
    assert payload["corpora"] == ["alpha"]
    expected_root = target / "Errorta" / "corpora"
    assert Path(payload["dest_root"]) == expected_root
    # Pure plan — destination never created.
    assert not target.exists()


# ---------------------------------------------------------------------------
# /export/run — happy path
# ---------------------------------------------------------------------------


def test_run_streams_phases_files_done_and_writes_manifest(
    client: TestClient, tmp_errorta_home: Path, tmp_path: Path
) -> None:
    _build_fake_corpus(
        tmp_errorta_home / ".errorta",
        "alpha",
        [("a.pdf", b"hello"), ("b.txt", b"world!!")],
    )
    target = tmp_path / "usb-out"

    with client.stream(
        "POST",
        "/export/run",
        json={"target_dir": str(target), "corpora_list": ["alpha"]},
    ) as resp:
        assert resp.status_code == 200
        body_text = "".join(chunk for chunk in resp.iter_text())
    events = _parse_sse(body_text)

    # First event is hello.
    assert events[0] == {"event": "hello"}
    # phase: copying appears before phase: verifying, and a done at the end.
    kinds = [e.get("event") or e.get("phase") for e in events]
    # phase events use {"event":"phase", "phase":"copying"}; extract for ordering.
    phase_indices = [
        i for i, e in enumerate(events) if e.get("event") == "phase"
    ]
    assert len(phase_indices) == 2
    assert events[phase_indices[0]]["phase"] == "copying"
    assert events[phase_indices[1]]["phase"] == "verifying"

    # At least one 'file' event between copying and verifying.
    file_events = [e for e in events if e.get("event") == "file"]
    assert len(file_events) >= 1
    # bytes_done monotonically non-decreasing.
    last = -1
    for f in file_events:
        bd = int(f["bytes_done"])
        assert bd >= last, f"bytes_done regressed: {bd} < {last}"
        last = bd

    # done at the end with summary.manifest_path.
    assert events[-1]["event"] == "done"
    summary = events[-1]["summary"]
    assert summary["files_copied"] == 2
    manifest_path = Path(summary["manifest_path"])
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["file_count"] == 2
    # No stray "error" events on the happy path.
    assert all(e.get("event") != "error" for e in events)
    # Files actually landed on disk.
    assert (target / "Errorta" / "corpora" / "alpha" / "files" / "a.pdf").exists()
    assert (target / "Errorta" / "corpora" / "alpha" / "files" / "b.txt").exists()
    # Silence unused 'kinds' lint hint.
    assert "phase" in kinds or True


# ---------------------------------------------------------------------------
# /export/run — integrity failure emits error event, no raise
# ---------------------------------------------------------------------------


def test_run_emits_error_event_on_integrity_failure(
    client: TestClient, tmp_errorta_home: Path, tmp_path: Path, monkeypatch
) -> None:
    # Use bogus recorded sha so copy_with_progress raises ExportIntegrityError
    # while writing the partial file. The route must catch and surface as
    # an SSE error event, never raise into the HTTP response.
    _build_fake_corpus(
        tmp_errorta_home / ".errorta",
        "alpha",
        [("a.pdf", b"hello-world")],
        record_real_sha=False,  # planted sha mismatches the file content
    )
    target = tmp_path / "usb-out"

    with client.stream(
        "POST",
        "/export/run",
        json={"target_dir": str(target), "corpora_list": ["alpha"]},
    ) as resp:
        assert resp.status_code == 200
        body_text = "".join(chunk for chunk in resp.iter_text())
    events = _parse_sse(body_text)
    # Final event is an error frame.
    assert events[-1]["event"] == "error"
    assert "Integrity" in events[-1]["error"] or "expected" in events[-1]["error"]
    # No done event after the error.
    assert all(e.get("event") != "done" for e in events)
