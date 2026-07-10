"""F008-HISTORY-RESTORE — snapshot restore endpoint tests.

Verifies:
  * Happy path: brief.md byte-equals snapshot; current draft saved to history;
    manifest state -> DRAFT; created_at / last_run_at / runs[] preserved.
  * 404 on unknown brief and unknown snapshot timestamp.
  * 400 on malformed timestamp.
  * 400 on corrupted snapshot (parse fails); brief.md untouched on disk.
  * Path traversal via timestamp is rejected.
"""
from __future__ import annotations

import json
import textwrap
import time
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from errorta_briefs.runner import CONNECTOR_REGISTRY, reset_active_run


BRIEF_MD = textwrap.dedent(
    """\
    ---
    project: Restore Test
    corpus: restore-corpus
    sensitivity: Public
    refresh: manual
    sources:
      - name: fake
        config: {}
    ---

    Initial body.
    """
)


def _bump(markdown: str, tag: str) -> str:
    return markdown + f"\n\n<!-- edit {tag} -->\n"


@pytest.fixture(autouse=True)
def _reset_runner_singletons() -> Iterator[None]:
    reset_active_run()
    CONNECTOR_REGISTRY.clear()
    yield
    reset_active_run()
    CONNECTOR_REGISTRY.clear()


@pytest.fixture
def client(tmp_errorta_home: Path) -> Iterator[TestClient]:
    from errorta_app.server import app

    with TestClient(app) as c:
        yield c


def _brief_dir(home: Path) -> Path:
    return home / ".errorta" / "corpora" / "restore-corpus"


def _seed_with_history(client: TestClient) -> str:
    """Create the brief, do one PUT to generate a snapshot, return the timestamp."""
    r = client.post("/briefs", json={"markdown": BRIEF_MD})
    assert r.status_code == 201, r.text
    time.sleep(0.01)
    r2 = client.put("/briefs/restore-corpus", json={"markdown": _bump(BRIEF_MD, "v2")})
    assert r2.status_code == 200, r2.text
    hist = client.get("/briefs/restore-corpus/history").json()
    assert len(hist) == 1
    return hist[0]["timestamp"]


def test_restore_happy_path(client: TestClient, tmp_errorta_home: Path) -> None:
    timestamp = _seed_with_history(client)
    brief_dir = _brief_dir(tmp_errorta_home)
    md_path = brief_dir / "brief.md"
    manifest_path = brief_dir / "brief-manifest.json"

    # Capture pre-restore manifest fields so we can confirm preservation.
    pre = json.loads(manifest_path.read_text(encoding="utf-8"))
    pre_created_at = pre["created_at"]
    pre_last_run_at = pre.get("last_run_at")
    pre_runs = pre.get("runs", [])

    # Capture the current on-disk markdown (the "v2" body) before restore.
    current_md = md_path.read_text(encoding="utf-8")
    assert current_md != BRIEF_MD  # sanity: we did edit it

    r = client.post(f"/briefs/restore-corpus/history/{timestamp}/restore")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["brief_id"] == "restore-corpus"
    assert body["state"] == "DRAFT"

    # brief.md now byte-equals the snapshot (which was BRIEF_MD).
    assert md_path.read_text(encoding="utf-8") == BRIEF_MD

    # The pre-restore markdown is now in history as a NEW snapshot entry.
    hist = client.get("/briefs/restore-corpus/history").json()
    assert len(hist) == 2  # original + the one captured during restore
    # The newest entry should be the just-captured pre-restore markdown.
    newest_ts = hist[0]["timestamp"]
    snap = client.get(f"/briefs/restore-corpus/history/{newest_ts}")
    assert snap.status_code == 200
    assert snap.text == current_md

    # Manifest preserved created_at / last_run_at / runs[]; state -> DRAFT.
    post = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert post["state"] == "DRAFT"
    assert post["created_at"] == pre_created_at
    assert post.get("last_run_at") == pre_last_run_at
    assert post.get("runs", []) == pre_runs


def test_restore_unknown_brief_returns_404(client: TestClient) -> None:
    r = client.post(
        "/briefs/does-not-exist/history/2099-01-01T000000.000000Z/restore",
    )
    assert r.status_code == 404


def test_restore_unknown_snapshot_returns_404(client: TestClient) -> None:
    client.post("/briefs", json={"markdown": BRIEF_MD})
    r = client.post(
        "/briefs/restore-corpus/history/2099-01-01T000000.000000Z/restore",
    )
    assert r.status_code == 404


def test_restore_malformed_timestamp_returns_400(client: TestClient) -> None:
    client.post("/briefs", json={"markdown": BRIEF_MD})
    # Contains characters outside [0-9T.Z-] -> regex fails.
    r = client.post("/briefs/restore-corpus/history/not_a_timestamp!/restore")
    assert r.status_code == 400


def test_restore_corrupted_snapshot_returns_400_and_leaves_brief_untouched(
    client: TestClient, tmp_errorta_home: Path
) -> None:
    timestamp = _seed_with_history(client)
    brief_dir = _brief_dir(tmp_errorta_home)
    md_path = brief_dir / "brief.md"
    snap_path = brief_dir / "brief-history" / f"{timestamp}.md"

    # Corrupt the snapshot on disk so parse_brief_markdown fails.
    snap_path.write_text("this is not a valid brief markdown\n", encoding="utf-8")

    # Capture current brief.md bytes before the failing restore attempt.
    before = md_path.read_bytes()

    r = client.post(f"/briefs/restore-corpus/history/{timestamp}/restore")
    assert r.status_code == 400
    detail = r.json().get("detail")
    # Either the structured {message, errors} dict or a string with parse info.
    assert detail is not None

    # brief.md MUST be unchanged on disk.
    assert md_path.read_bytes() == before


def test_restore_path_traversal_is_rejected(
    client: TestClient, tmp_errorta_home: Path
) -> None:
    client.post("/briefs", json={"markdown": BRIEF_MD})
    # A regex-shape-matching string that resolves outside the history root
    # would be caught by the resolved-path check. A timestamp with regex-illegal
    # characters (slashes) won't even match the route. Either outcome is safe.
    r = client.post("/briefs/restore-corpus/history/..%2Fbrief.md/restore")
    assert r.status_code in (400, 404)

    # A regex-passing but non-existent timestamp -> 404.
    r2 = client.post(
        "/briefs/restore-corpus/history/-..-not-real-/restore"
    )
    assert r2.status_code in (400, 404)
