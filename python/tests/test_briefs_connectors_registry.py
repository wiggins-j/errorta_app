"""HTML-REGISTER track — registry resolution for GenericHTMLConnector.

Verifies the connector is reachable via CONNECTOR_REGISTRY['generic_html']
(so the BriefRunner can instantiate it from a SourceSpec) and that
constructing it with a minimal license-overridden config produces a healthy
.status() report.
"""
from __future__ import annotations

import httpx

from errorta_briefs_connectors import CONNECTOR_REGISTRY, GenericHTMLConnector


def test_registry_resolves_generic_html_to_class() -> None:
    assert CONNECTOR_REGISTRY["generic_html"] is GenericHTMLConnector


def test_generic_html_instantiates_with_license_override_and_reports_ok() -> None:
    # Inject a no-op httpx.Client so __init__ does not try to open a real
    # network-bound client. The connector only uses the client during search/
    # fetch — neither of which is exercised here — so the transport is a stub.
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=""))
    client = httpx.Client(transport=transport)

    cls = CONNECTOR_REGISTRY["generic_html"]
    connector = cls(
        {
            "seed_urls": ["https://example.com/whitepaper"],
            "license_override": "CC-BY",
            "http_client": client,
        }
    )

    assert isinstance(connector, GenericHTMLConnector)

    status = connector.status()
    assert status["source"] == "generic_html"
    assert status["reachable"] is True
    assert status["license_override"] == "CC-BY"
