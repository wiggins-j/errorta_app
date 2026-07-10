"""F008-IMPORT-VAL — tests for POST /briefs/validate-markdown.

Hermetic: HOME redirected via ``tmp_errorta_home``, no network. A mock
connector is registered into CONNECTOR_REGISTRY for the "valid" case and
cleared on teardown.
"""
from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Iterator, List

import pytest
from fastapi.testclient import TestClient

from errorta_briefs.connector import SourceConnector, SourceDoc
from errorta_briefs.runner import CONNECTOR_REGISTRY, reset_active_run


VALID_BRIEF_MD = textwrap.dedent(
    """\
    ---
    project: Validate Markdown Test
    corpus: validate-md-corpus
    sensitivity: Public
    refresh: manual
    sources:
      - name: mocksrc
        config: {}
    ---

    Body prose.
    """
)


UNKNOWN_CONNECTOR_BRIEF_MD = textwrap.dedent(
    """\
    ---
    project: Unknown Connector Test
    corpus: unknown-conn-corpus
    sensitivity: Public
    refresh: manual
    sources:
      - name: not-a-real-connector
        config: {}
    ---

    Body prose.
    """
)


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


class _AlwaysOkConnector(SourceConnector):
    """Mock connector that reports ok=True with no sample docs."""

    def __init__(self, config: dict) -> None:
        self._config = config

    def search(self, page: int) -> Iterator[SourceDoc]:  # pragma: no cover
        return iter([])

    def fetch(self, doc: SourceDoc) -> bytes:  # pragma: no cover
        return b""

    def canonical_id(self, doc: SourceDoc) -> str:  # pragma: no cover
        return doc.canonical_id

    def metadata(self, doc: SourceDoc) -> dict:  # pragma: no cover
        return {}

    def status(self) -> dict:
        return {"ok": True}


def test_validate_markdown_missing_frontmatter(client: TestClient) -> None:
    """Markdown without YAML frontmatter returns ok=false with a populated
    errors array (NOT a 422 — the import path needs ok/errors contract)."""
    r = client.post(
        "/briefs/validate-markdown",
        json={"markdown": "# Just a heading, no frontmatter\n"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert isinstance(body["errors"], list)
    assert len(body["errors"]) >= 1
    # The error entries are dicts with at least one of msg/message — clean
    # contract for the frontend's structured error rendering.
    first = body["errors"][0]
    assert isinstance(first, dict)
    assert any(k in first for k in ("msg", "message"))


def test_validate_markdown_unknown_connector(client: TestClient) -> None:
    """A brief that parses but references an unknown connector returns
    ok=false with connectors[name].ok=false."""
    r = client.post(
        "/briefs/validate-markdown",
        json={"markdown": UNKNOWN_CONNECTOR_BRIEF_MD},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["errors"] == []
    assert "not-a-real-connector" in body["connectors"]
    assert body["connectors"]["not-a-real-connector"]["ok"] is False
    assert body["parsed"] is not None
    assert body["parsed"]["corpus"] == "unknown-conn-corpus"


def test_validate_markdown_valid_brief(client: TestClient) -> None:
    """A valid brief with a known connector returns ok=true and a populated
    connectors map. Also confirms /validate-markdown does NOT create a brief
    on disk (no entry visible to /briefs list)."""
    CONNECTOR_REGISTRY["mocksrc"] = _AlwaysOkConnector
    r = client.post(
        "/briefs/validate-markdown",
        json={"markdown": VALID_BRIEF_MD},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["errors"] == []
    assert "mocksrc" in body["connectors"]
    assert body["connectors"]["mocksrc"]["ok"] is True
    assert body["parsed"] is not None
    assert body["parsed"]["corpus"] == "validate-md-corpus"

    # Hermetic side-effect check: validate-markdown must not persist.
    listing = client.get("/briefs").json()
    assert all(b["brief_id"] != "validate-md-corpus" for b in listing)
