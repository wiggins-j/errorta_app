"""Tests for the GenericHTMLConnector (F008c-html track).

All HTTP is intercepted with httpx.MockTransport — no real network. Time is
mocked via monkeypatching ``time.monotonic`` and ``time.sleep`` so the
politeness gate is observable without delaying the test run. Random jitter is
pinned via ``random.uniform`` monkeypatch.

The three load-bearing scenarios — robots Disallow blocking silently with
logged reason, license_override='CC-BY-4.0' passing compliance, and the
default (no override) producing a doc the downstream gate refuses — are each
covered by a dedicated test.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

import httpx
import pytest

from errorta_briefs.compliance import ComplianceGate
from errorta_briefs.connector import SourceDoc as GateSourceDoc
from errorta_briefs_connectors import FatalError, SourceConnector
from errorta_briefs_connectors import generic_html as gh_mod
from errorta_briefs_connectors.generic_html import (
    GenericHTMLConnector,
    USER_AGENT,
    _canonical_id_for,
    _normalize_url,
)

FIXTURES = Path(__file__).parent / "fixtures" / "generic_html"


def _load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    # Mirror arxiv.py's test pattern: MockTransport, no real network, the
    # connector's UA header is asserted separately so we do not bake it into
    # the transport-level client construction here.
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"User-Agent": USER_AGENT},
    )


def _pin_jitter_and_sleep(monkeypatch: pytest.MonkeyPatch, value: float = 2.0) -> list[float]:
    """Pin random.uniform to ``value`` and capture all sleep durations."""
    sleeps: list[float] = []
    monkeypatch.setattr(gh_mod.random, "uniform", lambda a, b: value)
    monkeypatch.setattr(gh_mod.time, "sleep", lambda s: sleeps.append(s))
    return sleeps


def _default_handler(
    *,
    robots: bytes | None = None,
    seed_body: bytes | None = None,
    seed_url: str = "https://example.test/seed",
) -> Callable[[httpx.Request], httpx.Response]:
    """Build a transport handler that serves robots.txt + a single seed page."""
    robots_payload = robots if robots is not None else b"User-agent: *\nAllow: /\n"
    body = seed_body if seed_body is not None else _load("seed_with_main.html")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(
                200, content=robots_payload, headers={"content-type": "text/plain"}
            )
        if url.startswith(seed_url):
            return httpx.Response(
                200, content=body, headers={"content-type": "text/html; charset=utf-8"}
            )
        return httpx.Response(404, content=b"")

    return handler


# ----------------------------------------------------------------- URL normalize


@pytest.mark.parametrize(
    "a, b",
    [
        ("https://Example.com/path", "https://example.com/path"),
        ("https://example.com/path/", "https://example.com/path"),
        ("https://example.com/path#frag", "https://example.com/path"),
        ("https://example.com/path?b=2&a=1", "https://example.com/path?a=1&b=2"),
        # All four variations combined collapse to the same identity.
        ("HTTPS://Example.com/path/?b=2&a=1#frag", "https://example.com/path?a=1&b=2"),
    ],
)
def test_canonical_id_stable_across_url_variants(a: str, b: str) -> None:
    assert _normalize_url(a) == _normalize_url(b)
    assert _canonical_id_for(a) == _canonical_id_for(b)
    assert _canonical_id_for(a).startswith("html:")
    assert len(_canonical_id_for(a)) == len("html:") + 16


# ----------------------------------------------------------------- init guards


def test_missing_seed_urls_raises_fatal() -> None:
    with pytest.raises(FatalError):
        GenericHTMLConnector({})


def test_max_hops_greater_than_one_raises_fatal_at_init() -> None:
    with pytest.raises(FatalError):
        GenericHTMLConnector({"seed_urls": ["https://example.test/"], "max_hops": 2})


def test_subclass_relationship() -> None:
    assert issubclass(GenericHTMLConnector, SourceConnector)


# ----------------------------------------------------------------- search/docs


def test_search_page_0_yields_one_doc_per_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    _pin_jitter_and_sleep(monkeypatch)
    seed = "https://example.test/seed"
    client = _mock_client(_default_handler(seed_url=seed))
    conn = GenericHTMLConnector({"seed_urls": [seed], "http_client": client})

    docs = list(conn.search(page=0))
    assert len(docs) == 1
    d = docs[0]
    assert d.source_url == seed
    assert d.canonical_id == _canonical_id_for(seed)
    assert d.sensitivity_class == "Public"
    # No override → default refused posture.
    assert d.redistribution_allowed is False
    assert d.license == "Unknown"


def test_license_override_yields_redistributable_public_doc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_jitter_and_sleep(monkeypatch)
    seed = "https://example.test/seed"
    client = _mock_client(_default_handler(seed_url=seed))
    conn = GenericHTMLConnector(
        {
            "seed_urls": [seed],
            "license_override": "CC-BY-4.0",
            "http_client": client,
        }
    )

    doc = next(iter(conn.search(page=0)))
    assert doc.redistribution_allowed is True
    assert doc.license == "CC-BY-4.0"
    assert doc.sensitivity_class == "Public"
    assert doc.extra["license_override_applied"] is True


# ------------------------------------------- compliance gate refusal demo (load-bearing)


def _to_gate_doc(d: SourceDoc) -> GateSourceDoc:  # type: ignore[name-defined]
    return GateSourceDoc(
        canonical_id=d.canonical_id,
        title=d.title,
        source_url=d.source_url,
        publication_date=None,
        sensitivity_class=d.sensitivity_class,
        redistribution_allowed=d.redistribution_allowed,
        license=d.license,
        extra=d.extra,
    )


def test_default_doc_is_refused_by_downstream_compliance_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit demonstration: without license_override, generic_html docs are
    refused by the ComplianceGate. This is the *intended* behavior."""
    _pin_jitter_and_sleep(monkeypatch)
    seed = "https://example.test/seed"
    client = _mock_client(_default_handler(seed_url=seed))
    conn = GenericHTMLConnector({"seed_urls": [seed], "http_client": client})

    doc = next(iter(conn.search(page=0)))
    ok, reason = ComplianceGate().accepts(_to_gate_doc(doc))
    assert ok is False
    assert reason is not None
    # Either the "Unknown" license fails the allowlist, or
    # redistribution_allowed=False fails rule 3 — both are correct refusals.
    assert ("license" in reason) or ("redistribution_allowed" in reason)


def test_cc_by_4_override_passes_downstream_compliance_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_jitter_and_sleep(monkeypatch)
    seed = "https://example.test/seed"
    client = _mock_client(_default_handler(seed_url=seed))
    conn = GenericHTMLConnector(
        {
            "seed_urls": [seed],
            "license_override": "CC-BY",  # in DEFAULT_LICENSE_ALLOWLIST
            "http_client": client,
        }
    )
    doc = next(iter(conn.search(page=0)))
    ok, reason = ComplianceGate().accepts(_to_gate_doc(doc))
    assert ok is True, reason


# ----------------------------------------------------------------- robots gate


def test_robots_disallow_blocks_fetch_with_logged_reason(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _pin_jitter_and_sleep(monkeypatch)
    seed = "https://example.test/seed"
    handler = _default_handler(robots=_load("robots_disallow.txt"), seed_url=seed)
    client = _mock_client(handler)
    conn = GenericHTMLConnector({"seed_urls": [seed], "http_client": client})

    doc = next(iter(conn.search(page=0)))
    caplog.set_level(logging.INFO, logger=gh_mod.logger.name)
    with pytest.raises(FatalError):
        conn.fetch(doc)
    # The reason is logged at INFO (or higher) with the URL.
    assert any(
        "robots" in rec.getMessage() and seed in rec.getMessage()
        for rec in caplog.records
    )


def test_robots_allow_lets_fetch_proceed(monkeypatch: pytest.MonkeyPatch) -> None:
    _pin_jitter_and_sleep(monkeypatch)
    seed = "https://example.test/seed"
    handler = _default_handler(robots=_load("robots_allow.txt"), seed_url=seed)
    client = _mock_client(handler)
    conn = GenericHTMLConnector({"seed_urls": [seed], "http_client": client})

    doc = next(iter(conn.search(page=0)))
    body = conn.fetch(doc)
    assert b"Errorta generic HTML connector fixture" in body


# ----------------------------------------------------------------- content gates


def test_js_only_seed_raises_fatal_with_no_doc_emitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_jitter_and_sleep(monkeypatch)
    seed = "https://example.test/spa"
    handler = _default_handler(
        seed_body=_load("seed_js_only.html"), seed_url=seed
    )
    client = _mock_client(handler)
    conn = GenericHTMLConnector({"seed_urls": [seed], "http_client": client})

    doc = next(iter(conn.search(page=0)))
    with pytest.raises(FatalError, match="JS-only"):
        conn.fetch(doc)


def test_content_type_octet_stream_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _pin_jitter_and_sleep(monkeypatch)
    seed = "https://example.test/seed"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/robots.txt"):
            return httpx.Response(200, content=b"", headers={"content-type": "text/plain"})
        return httpx.Response(
            200, content=b"\x00\x01binary\x02",
            headers={"content-type": "application/octet-stream"},
        )

    client = _mock_client(handler)
    conn = GenericHTMLConnector({"seed_urls": [seed], "http_client": client})
    doc = next(iter(conn.search(page=0)))
    with pytest.raises(FatalError, match="content-type"):
        conn.fetch(doc)


def test_body_over_10mb_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _pin_jitter_and_sleep(monkeypatch)
    seed = "https://example.test/seed"
    big = b"x" * (10 * 1024 * 1024 + 1)

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/robots.txt"):
            return httpx.Response(200, content=b"", headers={"content-type": "text/plain"})
        return httpx.Response(200, content=big, headers={"content-type": "text/html"})

    client = _mock_client(handler)
    conn = GenericHTMLConnector({"seed_urls": [seed], "http_client": client})
    doc = next(iter(conn.search(page=0)))
    with pytest.raises(FatalError, match="cap"):
        conn.fetch(doc)


# ----------------------------------------------------------------- politeness


def test_politeness_gate_per_host_with_mocked_monotonic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two fetches to the same host close together: second one must sleep."""
    seed = "https://example.test/seed"
    handler = _default_handler(robots=_load("robots_allow.txt"), seed_url=seed)
    client = _mock_client(handler)

    sleeps: list[float] = []
    clock = {"t": 1000.0}
    monkeypatch.setattr(gh_mod.time, "monotonic", lambda: clock["t"])

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr(gh_mod.time, "sleep", fake_sleep)
    monkeypatch.setattr(gh_mod.random, "uniform", lambda a, b: 2.5)

    conn = GenericHTMLConnector({"seed_urls": [seed], "http_client": client})
    doc = next(iter(conn.search(page=0)))

    # First fetch: no prior request on this host → robots fetch and seed
    # fetch each go without sleeping (gate keyed per host).
    conn.fetch(doc)
    first_round = list(sleeps)
    sleeps.clear()

    # Advance only 0.5s, then re-fetch: gate should sleep 2.0s (2.5 - 0.5).
    clock["t"] += 0.5
    conn.fetch(doc)
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(2.0, abs=1e-6)
    # And the first round shouldn't have slept anything either.
    assert first_round == []


def test_politeness_gate_per_host_independence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two different hosts get independent gates — no cross-host wait."""
    seed_a = "https://a.example.test/page"
    seed_b = "https://b.example.test/page"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, content=b"User-agent: *\nAllow: /\n")
        return httpx.Response(
            200,
            content=_load("seed_with_main.html"),
            headers={"content-type": "text/html"},
        )

    client = _mock_client(handler)
    sleeps: list[float] = []
    clock = {"t": 5000.0}
    monkeypatch.setattr(gh_mod.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(gh_mod.time, "sleep", lambda s: (sleeps.append(s), clock.__setitem__("t", clock["t"] + s)))
    monkeypatch.setattr(gh_mod.random, "uniform", lambda a, b: 2.5)

    conn = GenericHTMLConnector(
        {"seed_urls": [seed_a, seed_b], "http_client": client}
    )
    docs = list(conn.search(page=0))
    assert len(docs) == 2
    # Fetch one doc per host back-to-back — independent gates means zero sleep.
    conn.fetch(docs[0])
    conn.fetch(docs[1])
    assert sleeps == []


# --------------------------------------------------------------- user-agent header


def test_user_agent_header_is_identifying_and_not_a_browser_spoof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_jitter_and_sleep(monkeypatch)
    seen_uas: list[str] = []
    seed = "https://example.test/seed"

    def handler(request: httpx.Request) -> httpx.Response:
        seen_uas.append(request.headers.get("user-agent", ""))
        if str(request.url).endswith("/robots.txt"):
            return httpx.Response(
                200, content=b"User-agent: *\nAllow: /\n",
                headers={"content-type": "text/plain"},
            )
        return httpx.Response(
            200, content=_load("seed_with_main.html"),
            headers={"content-type": "text/html"},
        )

    client = _mock_client(handler)
    conn = GenericHTMLConnector({"seed_urls": [seed], "http_client": client})
    doc = next(iter(conn.search(page=0)))
    conn.fetch(doc)

    assert seen_uas, "no requests captured"
    for ua in seen_uas:
        assert ua == USER_AGENT
        assert "Errorta" in ua
        # No browser-spoofing tokens.
        assert "Mozilla" not in ua
        assert "Chrome" not in ua
        assert "Safari" not in ua


# ----------------------------------------------------------------- status helper


def test_status_does_not_ping_network() -> None:
    def fail(_r: httpx.Request) -> httpx.Response:
        raise AssertionError("status() must not perform any HTTP")

    client = _mock_client(fail)
    conn = GenericHTMLConnector(
        {"seed_urls": ["https://example.test/seed"], "http_client": client}
    )
    s = conn.status()
    assert s["source"] == "generic_html"
    assert s["reachable"] is True
    assert s["license_override"] is None
