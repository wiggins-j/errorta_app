"""errorta_briefs_connectors — Source connectors for brief-driven corpus collection.

F008c-arxiv track. Defines the SourceConnector ABC, SourceDoc dataclass, the
RetryableError / FatalError exception taxonomy, and the CONNECTOR_REGISTRY
mapping connector ids (matching brief `source.name` keys) to connector classes.

The interface mirrors the spec in `docs/specs/F008-brief-driven-collection.md`
section 2 ("Source Connector Interface"). Each connector validates its own
config dict, paginates a deterministic `search()`, and lazily fetches bytes
for one doc at a time. Compliance metadata (sensitivity_class,
redistribution_allowed) is the connector's responsibility — the downstream
compliance gate refuses anything missing required fields.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator, Optional

__all__ = [
    "ArxivConnector",
    "CONNECTOR_REGISTRY",
    "FatalError",
    "GenericHTMLConnector",
    "NtrsConnector",
    "RetryableError",
    "SourceConnector",
    "SourceDoc",
]


class RetryableError(Exception):
    """Transient failure (rate-limit, 5xx, network blip).

    Caller should retry with exponential backoff. If the upstream supplied a
    Retry-After header (HTTP 429/503), it is exposed as `retry_after_s` so the
    caller can honor it.
    """

    def __init__(self, message: str, *, retry_after_s: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class FatalError(Exception):
    """Permanent failure (auth denied, ToS block, schema invariant violated).

    Caller should stop this source, mark it failed in collect-state.json, and
    continue to the next source in the brief.
    """


@dataclass
class SourceDoc:
    """One discovered document, pre-fetch.

    Required compliance fields (sensitivity_class, redistribution_allowed) are
    populated by the connector at discovery time. `extra` is the
    connector-specific bag (e.g. arxiv version, doi, pdf_url for later fetch).
    """

    canonical_id: str
    title: str
    source_url: str
    publication_date: Optional[datetime]
    sensitivity_class: str
    redistribution_allowed: bool
    license: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


class SourceConnector(ABC):
    """Abstract base for all brief source connectors.

    Each concrete connector is registered in CONNECTOR_REGISTRY under the same
    `name` used in the brief's `sources[].name`. See F008 §2.
    """

    name: str = ""

    @abstractmethod
    def __init__(self, config: dict) -> None:
        """Validate connector-specific config from the brief."""

    @abstractmethod
    def search(self, *, page: int = 0) -> Iterator[SourceDoc]:
        """Yield SourceDoc results page-by-page. Must be deterministic per page."""

    @abstractmethod
    def fetch(self, doc: SourceDoc) -> bytes:
        """Download the document bytes (PDF/HTML/etc.)."""

    @abstractmethod
    def canonical_id(self, doc: SourceDoc) -> str:
        """Return the canonical id used for dedup. Usually equal to doc.canonical_id."""

    @abstractmethod
    def metadata(self, doc: SourceDoc) -> dict:
        """Return the full metadata dict to be persisted with the doc."""

    @abstractmethod
    def status(self) -> dict:
        """Return health/availability of this connector."""


# Concrete connectors are imported last so the base classes above are already
# defined in this module's namespace by the time `arxiv` imports them back.
from errorta_briefs_connectors.arxiv import ArxivConnector  # noqa: E402
from errorta_briefs_connectors.ntrs import NtrsConnector  # noqa: E402
from errorta_briefs_connectors.generic_html import GenericHTMLConnector  # noqa: E402

CONNECTOR_REGISTRY: dict[str, type[SourceConnector]] = {
    "arxiv": ArxivConnector,
    "ntrs": NtrsConnector,
    "generic_html": GenericHTMLConnector,
}
