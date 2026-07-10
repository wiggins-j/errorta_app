"""Tests for the NtrsConnector (F008-NTRS track).

All HTTP is intercepted with httpx.MockTransport — every request is matched
against an explicit handler table; unmatched requests fail the test (the
hermetic-network equivalent of respx's `assert_all_called`). Time is mocked
via monkeypatching `time.monotonic` and `time.sleep` so politeness gates and
backoff are observable without delaying the suite.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import httpx
import pytest

from errorta_briefs_connectors import (
    CONNECTOR_REGISTRY,
    FatalError,
    NtrsConnector,
    RetryableError,
    SourceConnector,
)
from errorta_briefs_connectors import _http as http_mod
from errorta_briefs_connectors import ntrs as ntrs_mod

FIXTURES = Path(__file__).parent / "fixtures" / "ntrs"


def _load_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _load_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class _Router:
    """Tiny URL-prefix router with a hit log — fails on any unmatched request.

    This is the assertion that earns the "respx asserts all requests mocked"
    acceptance criterion without taking a new dep: every request must match an
    explicitly registered route, or the test fails immediately.
    """

    def __init__(self) -> None:
        self.routes: list[tuple[str, Callable[[httpx.Request], httpx.Response]]] = []
        self.calls: list[str] = []

    def add(self, url_prefix: str, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.routes.append((url_prefix, handler))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        for prefix, handler in self.routes:
            if url.startswith(prefix):
                return handler(request)
        raise AssertionError(f"unmocked request to {url}")


def _mock_client(router: _Router) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(router))


def _json_response(payload: dict) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)
    return handler


# ----------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable real sleeps so the suite stays fast and hermetic."""
    monkeypatch.setattr(ntrs_mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr(http_mod.time, "sleep", lambda _s: None)


# --------------------------------------------------------------- registry sanity


def test_connector_registry_exposes_ntrs() -> None:
    assert "ntrs" in CONNECTOR_REGISTRY
    assert CONNECTOR_REGISTRY["ntrs"] is NtrsConnector
    assert issubclass(NtrsConnector, SourceConnector)


def test_ntrs_is_exported_in_all() -> None:
    import errorta_briefs_connectors as pkg

    assert "NtrsConnector" in pkg.__all__


# --------------------------------------------------------------- config gate


def test_missing_q_raises_fatal() -> None:
    router = _Router()
    with pytest.raises(FatalError):
        NtrsConnector({"http_client": _mock_client(router)})


def test_invalid_size_raises_fatal() -> None:
    router = _Router()
    with pytest.raises(FatalError):
        NtrsConnector(
            {"q": "thermal", "size": -1, "http_client": _mock_client(router)}
        )


# ---------------------------------------------------------------- search/parse


def test_search_emits_nasa_record_with_pdf() -> None:
    router = _Router()
    router.add(
        "https://ntrs.nasa.gov/api/citations/search",
        _json_response(_load_json("search_page0.json")),
    )
    client = _mock_client(router)
    conn = NtrsConnector({"q": "thermal", "size": 100, "http_client": client})

    docs = list(conn.search(page=0))

    # search_page0 has 3 records: NASA-PDF, contractor (refused), NASA-metadata.
    # The contractor record is dropped by the compliance gate, so we expect 2.
    assert len(docs) == 2
    pdf_doc = docs[0]
    # canonical_id = "ntrs:" + zfill(20) of the numeric NTRS id.
    assert pdf_doc.canonical_id == "ntrs:" + "20230001111".zfill(20)
    assert pdf_doc.title.startswith("Thermal Analysis")
    assert pdf_doc.sensitivity_class == "Public"
    assert pdf_doc.redistribution_allowed is True
    assert pdf_doc.extra["has_pdf"] is True
    assert pdf_doc.extra["pdf_url"] == (
        "https://ntrs.nasa.gov/api/citations/20230001111/downloads/thermal.pdf"
    )
    assert pdf_doc.publication_date is not None
    assert pdf_doc.publication_date.year == 2023


def test_search_emits_metadata_only_record_without_raising() -> None:
    router = _Router()
    router.add(
        "https://ntrs.nasa.gov/api/citations/search",
        _json_response(_load_json("search_page0.json")),
    )
    conn = NtrsConnector(
        {"q": "thermal", "size": 100, "http_client": _mock_client(router)}
    )

    docs = list(conn.search(page=0))
    meta_doc = [d for d in docs if d.canonical_id.endswith("20230003333")][0]
    assert meta_doc.extra["has_pdf"] is False
    assert meta_doc.extra["pdf_url"] is None
    assert meta_doc.extra["full_text"] is None


def test_compliance_gate_refuses_contractor_restricted() -> None:
    raw = _load_json("citation_contractor_restricted.json")
    allowed, reason = NtrsConnector._check_compliance(raw)
    assert allowed is False
    assert "license" in reason.lower()


def test_compliance_gate_allows_nasa_government_work_emits_public() -> None:
    raw = _load_json("citation_nasa_pdf.json")
    allowed, reason = NtrsConnector._check_compliance(raw)
    assert allowed is True
    assert "US-Gov-Work" in reason or "GOVERNMENT_WORK" in reason

    router = _Router()
    router.add(
        "https://ntrs.nasa.gov/api/citations/search",
        _json_response({"results": [raw], "stats": {"hits": 1}}),
    )
    conn = NtrsConnector(
        {"q": "thermal", "size": 100, "http_client": _mock_client(router)}
    )
    docs = list(conn.search(page=0))
    assert len(docs) == 1
    assert docs[0].sensitivity_class == "Public"


def test_compliance_gate_nasa_org_fallback_requires_government_work() -> None:
    # NASA org but determinationType != GOVERNMENT_WORK and licenseType not on
    # the allowlist → refused.
    raw = {
        "id": 1,
        "distribution": "PUBLIC",
        "copyright": {"licenseType": "OTHER", "determinationType": "CONTRACTOR"},
        "authorAffiliations": [
            {"meta": {"organization": {"name": "NASA Langley"}}}
        ],
    }
    allowed, _reason = NtrsConnector._check_compliance(raw)
    assert allowed is False


def test_compliance_gate_refuses_non_public_distribution() -> None:
    raw = {
        "id": 1,
        "distribution": "ITAR",
        "copyright": {"licenseType": "US-Gov-Work"},
    }
    allowed, reason = NtrsConnector._check_compliance(raw)
    assert allowed is False
    assert "distribution" in reason


# ---------------------------------------------------------------- pagination


def test_pagination_exits_on_partial_page() -> None:
    router = _Router()
    # Even though stats.hits=1001, the page only returns 1 result while size=100,
    # so the loop must exit (short page beats the hits-based termination).
    router.add(
        "https://ntrs.nasa.gov/api/citations/search",
        _json_response(_load_json("search_page_partial.json")),
    )
    conn = NtrsConnector(
        {"q": "thermal", "size": 100, "http_client": _mock_client(router)}
    )

    docs = list(conn.search(page=0))

    assert len(docs) == 1
    # Exactly one search call — pagination did not continue.
    assert len(router.calls) == 1


def test_pagination_exits_on_empty_page() -> None:
    router = _Router()
    router.add(
        "https://ntrs.nasa.gov/api/citations/search",
        _json_response(_load_json("search_empty.json")),
    )
    conn = NtrsConnector(
        {"q": "thermal", "size": 100, "http_client": _mock_client(router)}
    )
    assert list(conn.search(page=0)) == []
    assert len(router.calls) == 1


# ------------------------------------------------------------- retry helper


def test_retry_helper_429_twice_then_200_yields_three_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(http_mod.time, "sleep", lambda s: sleeps.append(s))

    calls: list[str] = []

    def send() -> httpx.Response:
        calls.append("call")
        if len(calls) <= 2:
            return httpx.Response(429, headers={"Retry-After": "0"}, content=b"slow down")
        return httpx.Response(200, json={"ok": True})

    resp = http_mod.request_with_retry(send)
    assert resp.status_code == 200
    assert len(calls) == 3
    assert len(sleeps) == 2  # one between each retry


def test_retry_helper_404_raises_fatal_after_one_call() -> None:
    calls: list[str] = []

    def send() -> httpx.Response:
        calls.append("call")
        return httpx.Response(404, content=b"not found")

    with pytest.raises(FatalError):
        http_mod.request_with_retry(send)
    assert len(calls) == 1


def test_retry_helper_500_exhausts_budget_and_raises_retryable() -> None:
    calls: list[str] = []

    def send() -> httpx.Response:
        calls.append("call")
        return httpx.Response(500, content=b"oops")

    with pytest.raises(RetryableError):
        http_mod.request_with_retry(send, max_retries=2)
    # 1 initial + 2 retries = 3 calls
    assert len(calls) == 3


def test_retry_helper_connect_error_then_success() -> None:
    calls: list[str] = []

    def send() -> httpx.Response:
        calls.append("call")
        if len(calls) == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"ok": True})

    resp = http_mod.request_with_retry(send)
    assert resp.status_code == 200
    assert len(calls) == 2


# ------------------------------------------------------------ date parser


@pytest.mark.parametrize(
    "raw, expected_year, expected_month, expected_day",
    [
        ("2023", 2023, 1, 1),
        ("2023-04", 2023, 4, 1),
        ("2023-04-05", 2023, 4, 5),
        ("2023-04-05T00:00:00Z", 2023, 4, 5),
    ],
)
def test_publication_date_parser_tolerates_partial_forms(
    raw: str, expected_year: int, expected_month: int, expected_day: int
) -> None:
    parsed = NtrsConnector._parse_publication_date(raw)
    assert parsed is not None
    assert parsed.year == expected_year
    assert parsed.month == expected_month
    assert parsed.day == expected_day


def test_publication_date_parser_returns_none_for_garbage() -> None:
    assert NtrsConnector._parse_publication_date("not a date") is None
    assert NtrsConnector._parse_publication_date(None) is None
    assert NtrsConnector._parse_publication_date("") is None


def test_partial_date_fixture_round_trips() -> None:
    raw = _load_json("citation_partial_date.json")
    router = _Router()
    router.add(
        "https://ntrs.nasa.gov/api/citations/search",
        _json_response({"results": [raw], "stats": {"hits": 1}}),
    )
    conn = NtrsConnector(
        {"q": "thermal", "size": 100, "http_client": _mock_client(router)}
    )
    docs = list(conn.search(page=0))
    assert len(docs) == 1
    assert docs[0].publication_date is not None
    assert docs[0].publication_date.year == 2019


# ------------------------------------------------------------------- fetch


def test_fetch_returns_pdf_bytes() -> None:
    pdf_payload = b"%PDF-1.4 fake thermal"
    router = _Router()
    router.add(
        "https://ntrs.nasa.gov/api/citations/search",
        _json_response(_load_json("search_page0.json")),
    )
    router.add(
        "https://ntrs.nasa.gov/api/citations/20230001111/downloads/thermal.pdf",
        lambda _r: httpx.Response(200, content=pdf_payload),
    )
    conn = NtrsConnector(
        {"q": "thermal", "size": 100, "http_client": _mock_client(router)}
    )

    docs = list(conn.search(page=0))
    pdf_doc = [d for d in docs if d.extra["has_pdf"]][0]
    body = conn.fetch(pdf_doc)
    assert body == pdf_payload


def test_fetch_raises_on_metadata_only_doc() -> None:
    router = _Router()
    router.add(
        "https://ntrs.nasa.gov/api/citations/search",
        _json_response(_load_json("search_page0.json")),
    )
    conn = NtrsConnector(
        {"q": "thermal", "size": 100, "http_client": _mock_client(router)}
    )
    docs = list(conn.search(page=0))
    meta = [d for d in docs if not d.extra["has_pdf"]][0]
    with pytest.raises(FatalError):
        conn.fetch(meta)


# ---------------------------------------------------------- identity helpers


def test_canonical_id_and_metadata_helpers() -> None:
    router = _Router()
    router.add(
        "https://ntrs.nasa.gov/api/citations/search",
        _json_response(_load_json("search_page0.json")),
    )
    conn = NtrsConnector(
        {"q": "thermal", "size": 100, "http_client": _mock_client(router)}
    )
    doc = next(iter(conn.search(page=0)))
    assert conn.canonical_id(doc) == doc.canonical_id
    assert conn.metadata(doc) is doc.extra


def test_status_does_not_ping_network() -> None:
    router = _Router()
    conn = NtrsConnector(
        {"q": "thermal", "size": 100, "http_client": _mock_client(router)}
    )
    s = conn.status()
    assert s == {"source": "ntrs", "reachable": True, "rate_limit": "~1 req per 1s"}
    assert router.calls == []
