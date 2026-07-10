"""Tests for the ArxivConnector (F008c-arxiv track).

All HTTP is intercepted with httpx.MockTransport — no real network. Time is
mocked via monkeypatching `time.monotonic` and `time.sleep` so the politeness
gate is observable without delaying the test run.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import httpx
import pytest

from errorta_briefs_connectors import (
    CONNECTOR_REGISTRY,
    ArxivConnector,
    FatalError,
    RetryableError,
    SourceConnector,
)
from errorta_briefs_connectors import arxiv as arxiv_mod

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _feed_handler(payload: bytes) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, headers={"content-type": "application/atom+xml"})
    return handler


# --------------------------------------------------------------- registry sanity


def test_connector_registry_exposes_arxiv() -> None:
    assert "arxiv" in CONNECTOR_REGISTRY
    assert CONNECTOR_REGISTRY["arxiv"] is ArxivConnector
    assert issubclass(ArxivConnector, SourceConnector)


# --------------------------------------------------------------- search/parse


def test_search_yields_five_source_docs_with_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    # No politeness sleep — first request, but also bypass it for hermeticity.
    monkeypatch.setattr(arxiv_mod.time, "sleep", lambda _s: None)

    client = _mock_client(_feed_handler(_load("arxiv_feed_5entries.xml")))
    conn = ArxivConnector({"categories": ["cs.AI", "cs.CL"], "http_client": client})

    docs = list(conn.search(page=0))

    assert len(docs) >= 5
    first = docs[0]
    assert first.canonical_id == "2403.12345"
    assert first.extra["arxiv_version"] == "2"
    assert first.title.startswith("Efficient Retrieval-Augmented Generation")
    # Whitespace collapsed (no embedded newlines).
    assert "\n" not in first.title
    assert first.extra["authors"] == ["Alice Researcher", "Bob Coauthor"]
    assert first.extra["primary_category"] == "cs.IR"
    assert "cs.IR" in first.extra["categories"]
    assert "cs.CL" in first.extra["categories"]
    assert first.extra["pdf_url"] == "http://arxiv.org/pdf/2403.12345v2"
    assert first.extra["abs_url"] == "http://arxiv.org/abs/2403.12345v2"
    assert first.extra["doi"] == "10.1234/arxiv.2403.12345"
    assert first.publication_date is not None
    assert first.publication_date.year == 2024
    assert first.extra["published"].startswith("2024-03-15")


def test_default_entries_have_no_redistribution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(arxiv_mod.time, "sleep", lambda _s: None)
    client = _mock_client(_feed_handler(_load("arxiv_feed_5entries.xml")))
    conn = ArxivConnector({"categories": ["cs.AI"], "http_client": client})

    docs = list(conn.search(page=0))

    for d in docs:
        assert d.sensitivity_class == "Public"
        assert d.redistribution_allowed is False
        assert d.license is None


def test_cc_by_entry_flips_redistribution_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(arxiv_mod.time, "sleep", lambda _s: None)
    client = _mock_client(_feed_handler(_load("arxiv_feed_cc_by.xml")))
    conn = ArxivConnector({"categories": ["cs.AI"], "http_client": client})

    docs = list(conn.search(page=0))

    assert len(docs) == 1
    doc = docs[0]
    assert doc.canonical_id == "2405.11111"
    assert doc.license == "CC-BY"
    assert doc.redistribution_allowed is True
    assert doc.sensitivity_class == "Public"


def test_canonical_id_and_metadata_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(arxiv_mod.time, "sleep", lambda _s: None)
    client = _mock_client(_feed_handler(_load("arxiv_feed_5entries.xml")))
    conn = ArxivConnector({"categories": ["cs.AI"], "http_client": client})

    doc = next(iter(conn.search(page=0)))
    assert conn.canonical_id(doc) == doc.canonical_id
    assert conn.metadata(doc) is doc.extra


def test_status_does_not_ping_network() -> None:
    # Explicitly pass a client that would raise if used — status() must not call it.
    def fail(_r: httpx.Request) -> httpx.Response:
        raise AssertionError("status() must not perform any HTTP")

    client = _mock_client(fail)
    conn = ArxivConnector({"categories": ["cs.AI"], "http_client": client})
    s = conn.status()
    assert s == {"source": "arxiv", "reachable": True, "rate_limit": "1 req per 3s"}


# ------------------------------------------------------------ id parser edge cases


@pytest.mark.parametrize(
    "atom_id, expected_canonical, expected_version",
    [
        ("http://arxiv.org/abs/2403.12345v2", "2403.12345", "2"),
        ("http://arxiv.org/abs/2403.12345v10", "2403.12345", "10"),
        ("http://arxiv.org/abs/hep-th/9901001v1", "hep-th/9901001", "1"),
        ("http://arxiv.org/abs/2407.0001v1", "2407.0001", "1"),
    ],
)
def test_split_id_strips_version(
    atom_id: str, expected_canonical: str, expected_version: str
) -> None:
    canonical, version = ArxivConnector._split_id(atom_id)
    assert canonical == expected_canonical
    assert version == expected_version


# ---------------------------------------------------------- error classification


def test_503_with_retry_after_raises_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(arxiv_mod.time, "sleep", lambda _s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"busy", headers={"Retry-After": "7"})

    client = _mock_client(handler)
    conn = ArxivConnector({"categories": ["cs.AI"], "http_client": client})

    with pytest.raises(RetryableError) as excinfo:
        list(conn.search(page=0))
    assert excinfo.value.retry_after_s == 7.0


def test_400_raises_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(arxiv_mod.time, "sleep", lambda _s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=b"bad query")

    client = _mock_client(handler)
    conn = ArxivConnector({"categories": ["cs.AI"], "http_client": client})

    with pytest.raises(FatalError):
        list(conn.search(page=0))


def test_500_without_retry_after_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(arxiv_mod.time, "sleep", lambda _s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"oops")

    client = _mock_client(handler)
    conn = ArxivConnector({"categories": ["cs.AI"], "http_client": client})

    with pytest.raises(RetryableError) as excinfo:
        list(conn.search(page=0))
    assert excinfo.value.retry_after_s is None


# ------------------------------------------------------------- politeness gate


def test_politeness_gate_sleeps_when_called_twice_within_3s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two requests close together: the second one must sleep ~3s."""
    sleeps: list[float] = []
    # Monotonic clock advances only when we say so.
    clock = {"t": 1000.0}

    def fake_monotonic() -> float:
        return clock["t"]

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr(arxiv_mod.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(arxiv_mod.time, "sleep", fake_sleep)

    client = _mock_client(_feed_handler(_load("arxiv_feed_5entries.xml")))
    conn = ArxivConnector({"categories": ["cs.AI"], "http_client": client})

    # First page: no prior request, no sleep needed.
    list(conn.search(page=0))
    assert sleeps == []  # gate didn't fire on the first call

    # Advance the clock by only 0.5s — gate should now sleep ~2.5s.
    clock["t"] += 0.5
    list(conn.search(page=1))

    assert len(sleeps) == 1
    # Remaining = 3.0 - 0.5 = 2.5
    assert sleeps[0] == pytest.approx(2.5, abs=1e-6)


def test_politeness_gate_skips_sleep_when_3s_already_elapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    clock = {"t": 5000.0}

    monkeypatch.setattr(arxiv_mod.time, "monotonic", lambda: clock["t"])

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr(arxiv_mod.time, "sleep", fake_sleep)

    client = _mock_client(_feed_handler(_load("arxiv_feed_5entries.xml")))
    conn = ArxivConnector({"categories": ["cs.AI"], "http_client": client})

    list(conn.search(page=0))
    # Skip well past the 3s window before issuing the next request.
    clock["t"] += 10.0
    list(conn.search(page=1))

    assert sleeps == []  # gate did not need to fire


# ---------------------------------------------------------------------- fetch


def test_fetch_returns_pdf_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(arxiv_mod.time, "sleep", lambda _s: None)
    pdf_payload = b"%PDF-1.4 fake bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        if "pdf" in str(request.url):
            return httpx.Response(200, content=pdf_payload)
        return httpx.Response(200, content=_load("arxiv_feed_5entries.xml"))

    client = _mock_client(handler)
    conn = ArxivConnector({"categories": ["cs.AI"], "http_client": client})

    doc = next(iter(conn.search(page=0)))
    body = conn.fetch(doc)
    assert body == pdf_payload


def test_fetch_without_pdf_url_raises_fatal() -> None:
    conn = ArxivConnector(
        {"categories": ["cs.AI"], "http_client": _mock_client(lambda r: httpx.Response(200))}
    )
    # Build a SourceDoc manually with no pdf_url.
    from errorta_briefs_connectors import SourceDoc

    doc = SourceDoc(
        canonical_id="x",
        title="t",
        source_url="http://example.com",
        publication_date=None,
        sensitivity_class="Public",
        redistribution_allowed=False,
        license=None,
        extra={},
    )
    with pytest.raises(FatalError):
        conn.fetch(doc)


# ----------------------------------------------------------------- config gate


def test_missing_categories_raises_fatal() -> None:
    with pytest.raises(FatalError):
        ArxivConnector({"http_client": _mock_client(lambda r: httpx.Response(200))})
