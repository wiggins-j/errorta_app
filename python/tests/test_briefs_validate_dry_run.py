"""F008 VALIDATE-UI track — tests for /briefs/{id}/validate?dry_run=true.

Hermetic: HOME redirected via ``tmp_errorta_home``. No network — connectors
are mock subclasses of SourceConnector registered into CONNECTOR_REGISTRY
for the test, then cleared.
"""
from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Iterator, List

import pytest
from fastapi.testclient import TestClient

from errorta_briefs.connector import SourceConnector, SourceDoc
from errorta_briefs.runner import CONNECTOR_REGISTRY, reset_active_run


BRIEF_MD = textwrap.dedent(
    """\
    ---
    project: Test Project
    corpus: dryrun-corpus
    sensitivity: Public
    refresh: manual
    sources:
      - name: mocksrc
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


def _make_doc(idx: int, license_value: str | None) -> SourceDoc:
    return SourceDoc(
        canonical_id=f"doc-{idx}",
        title=f"Doc {idx}",
        source_url=f"https://example.invalid/{idx}",
        publication_date="2026-01-01",
        sensitivity_class="Public",
        redistribution_allowed=True,
        license=license_value,
    )


class _MixedLicenseConnector(SourceConnector):
    """Yields 3 docs with CC-BY (valid) and 2 docs with 'Unknown' (refused)."""

    def __init__(self, config: dict) -> None:
        self._config = config

    def search(self, page: int) -> Iterator[SourceDoc]:
        docs: List[SourceDoc] = [
            _make_doc(1, "CC-BY"),
            _make_doc(2, "CC-BY"),
            _make_doc(3, "CC-BY"),
            _make_doc(4, "Unknown"),
            _make_doc(5, "Unknown"),
        ]
        return iter(docs)

    def fetch(self, doc: SourceDoc) -> bytes:  # pragma: no cover
        return b""

    def canonical_id(self, doc: SourceDoc) -> str:  # pragma: no cover
        return doc.canonical_id

    def metadata(self, doc: SourceDoc) -> dict:  # pragma: no cover
        return {}

    def status(self) -> dict:
        return {"ok": True}


class _NoLicenseOverrideConnector(SourceConnector):
    """Mimic GenericHTMLConnector with no license override — every doc has
    license='Unknown' so every candidate gets refused on the same rule."""

    def __init__(self, config: dict) -> None:
        self._config = config

    def search(self, page: int) -> Iterator[SourceDoc]:
        docs: List[SourceDoc] = [_make_doc(i, "Unknown") for i in range(1, 6)]
        return iter(docs)

    def fetch(self, doc: SourceDoc) -> bytes:  # pragma: no cover
        return b""

    def canonical_id(self, doc: SourceDoc) -> str:  # pragma: no cover
        return doc.canonical_id

    def metadata(self, doc: SourceDoc) -> dict:  # pragma: no cover
        return {}

    def status(self) -> dict:
        return {"ok": True}


def test_validate_without_dry_run_unchanged(client: TestClient) -> None:
    CONNECTOR_REGISTRY["mocksrc"] = _MixedLicenseConnector
    client.post("/briefs", json={"markdown": BRIEF_MD})
    r = client.post("/briefs/dryrun-corpus/validate")
    assert r.status_code == 200, r.text
    body = r.json()
    # backward-compat: dry_run_projection absent or None.
    assert body.get("dry_run_projection") in (None, {})
    assert "connectors" in body and "mocksrc" in body["connectors"]


def test_validate_dry_run_mixed_licenses(client: TestClient) -> None:
    CONNECTOR_REGISTRY["mocksrc"] = _MixedLicenseConnector
    client.post("/briefs", json={"markdown": BRIEF_MD})
    r = client.post("/briefs/dryrun-corpus/validate?dry_run=true")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("dry_run_projection") is not None
    proj = body["dry_run_projection"]["mocksrc"]
    assert proj["candidates_seen"] == 5
    assert proj["compliance_pass"] == 3
    assert proj["compliance_refused"] == 2
    assert proj["connector_name"] == "_MixedLicenseConnector"
    assert any(
        "license not in allowlist: Unknown" in reason
        for reason in proj["sample_refusal_reasons"]
    )


def test_validate_dry_run_no_license_override_all_refused(client: TestClient) -> None:
    CONNECTOR_REGISTRY["mocksrc"] = _NoLicenseOverrideConnector
    client.post("/briefs", json={"markdown": BRIEF_MD})
    r = client.post("/briefs/dryrun-corpus/validate?dry_run=true")
    assert r.status_code == 200, r.text
    body = r.json()
    proj = body["dry_run_projection"]["mocksrc"]
    assert proj["candidates_seen"] == 5
    assert proj["compliance_pass"] == 0
    assert proj["compliance_refused"] == 5
    # 100% refused, every reason is the license-allowlist message.
    assert len(proj["sample_refusal_reasons"]) == 5
    assert all(
        r == "license not in allowlist: Unknown"
        for r in proj["sample_refusal_reasons"]
    )
