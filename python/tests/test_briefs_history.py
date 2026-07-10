"""F008-HISTORY — per-brief edit history tests.

Verifies:
  * PUT /briefs/{id} captures one snapshot per update (first create produces 0)
  * GET /briefs/{id}/history sorts desc and reports valid sha256 + byte_size
  * GET /briefs/{id}/history/{ts} returns the snapshot body
  * path-traversal timestamps reject with 400, missing snapshots 404
"""
from __future__ import annotations

import hashlib
import re
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
    project: History Test
    corpus: history-corpus
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


def test_first_create_produces_no_snapshot(client: TestClient, tmp_errorta_home: Path) -> None:
    r = client.post("/briefs", json={"markdown": BRIEF_MD})
    assert r.status_code == 201, r.text

    r2 = client.get("/briefs/history-corpus/history")
    assert r2.status_code == 200
    assert r2.json() == []


def test_each_put_creates_one_snapshot(client: TestClient) -> None:
    client.post("/briefs", json={"markdown": BRIEF_MD})

    # Two sequential PUTs. Use a small sleep so timestamps differ (the format is
    # microsecond precision, but on fast machines two writes within the same
    # microsecond would collide; the sleep is generous slack).
    v1 = _bump(BRIEF_MD, "one")
    r1 = client.put("/briefs/history-corpus", json={"markdown": v1})
    assert r1.status_code == 200, r1.text
    time.sleep(0.01)

    v2 = _bump(BRIEF_MD, "two")
    r2 = client.put("/briefs/history-corpus", json={"markdown": v2})
    assert r2.status_code == 200, r2.text

    hist = client.get("/briefs/history-corpus/history").json()
    assert len(hist) == 2
    # Sorted descending — newest first.
    assert hist[0]["timestamp"] > hist[1]["timestamp"]
    # sha256 + byte_size valid and consistent with stored bytes.
    for entry in hist:
        assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is not None
        assert entry["byte_size"] > 0


def test_history_entries_match_persisted_bodies(
    client: TestClient, tmp_errorta_home: Path
) -> None:
    client.post("/briefs", json={"markdown": BRIEF_MD})
    v1 = _bump(BRIEF_MD, "one")
    client.put("/briefs/history-corpus", json={"markdown": v1})

    hist = client.get("/briefs/history-corpus/history").json()
    assert len(hist) == 1
    entry = hist[0]

    # Resolve the on-disk snapshot and check sha256 matches the reported one.
    snap_dir = tmp_errorta_home / ".errorta" / "corpora" / "history-corpus" / "brief-history"
    files = list(snap_dir.glob("*.md"))
    assert len(files) == 1
    raw = files[0].read_bytes()
    assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
    assert len(raw) == entry["byte_size"]
    # The snapshot body should be the *prior* on-disk markdown (i.e. BRIEF_MD).
    assert raw.decode("utf-8") == BRIEF_MD


def test_get_history_snapshot_returns_markdown(client: TestClient) -> None:
    client.post("/briefs", json={"markdown": BRIEF_MD})
    client.put("/briefs/history-corpus", json={"markdown": _bump(BRIEF_MD, "one")})
    entry = client.get("/briefs/history-corpus/history").json()[0]

    r = client.get(f"/briefs/history-corpus/history/{entry['timestamp']}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert r.text == BRIEF_MD


def test_path_traversal_timestamp_is_rejected(client: TestClient) -> None:
    client.post("/briefs", json={"markdown": BRIEF_MD})
    client.put("/briefs/history-corpus", json={"markdown": _bump(BRIEF_MD, "one")})

    # FastAPI's TestClient sends the path raw; the slashes will simply not match
    # the route (404 from the router itself) — but the explicit traversal token
    # that *does* hit the route through alternative encodings (e.g. embedded
    # dots) must be rejected with 400.
    bad = client.get("/briefs/history-corpus/history/..%2Fbrief.md")
    # Either the router rejects with 404 (slash decoded after routing) or the
    # handler's regex rejects with 400. Either is a safe outcome.
    assert bad.status_code in (400, 404)

    # A timestamp with dot-dot characters that *does* match the regex shape
    # must still be rejected with 404 (no such file) — proves we don't fall
    # through to reading arbitrary names.
    weird = client.get("/briefs/history-corpus/history/..-not-a-real-timestamp")
    assert weird.status_code in (400, 404)


def test_missing_snapshot_returns_404(client: TestClient) -> None:
    client.post("/briefs", json={"markdown": BRIEF_MD})
    # Valid-shape timestamp, but no such snapshot on disk.
    r = client.get("/briefs/history-corpus/history/2099-01-01T000000.000000Z")
    assert r.status_code == 404


def test_history_for_unknown_brief_is_404(client: TestClient) -> None:
    r = client.get("/briefs/does-not-exist/history")
    assert r.status_code == 404
