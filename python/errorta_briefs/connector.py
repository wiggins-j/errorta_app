"""SourceConnector ABC and shared exceptions for brief-driven collection (F008).

This module defines the contract every source-specific connector must implement
so a brief-driven collection pipeline can fan out across heterogeneous data
sources (NTRS, arXiv, FAA, ESA, NIST, NOAA, USPTO, ...) uniformly.

Public surface:
- ``RetryableError`` — transient failure; the runner should back off and retry.
- ``FatalError`` — non-recoverable; the runner should stop calling this connector.
- ``SourceDoc`` — minimal document descriptor passed between connector phases.
- ``SourceConnector`` — the ABC. Subclasses MUST implement all 6 abstract methods.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator


class RetryableError(Exception):
    """A transient connector failure (rate limit, 5xx, network blip).

    ``retry_after_s`` is the connector's hint to the runner about how long to
    back off before retrying. ``None`` means "use the runner's default policy".
    """

    def __init__(self, message: str = "", retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s: float | None = retry_after_s


class FatalError(Exception):
    """A non-recoverable connector failure (auth denied, bad config, 4xx).

    The runner should stop calling this connector for the remainder of the run.
    """


@dataclass
class SourceDoc:
    """Minimal cross-connector document descriptor.

    Required fields are the compliance-gate inputs: identity, provenance,
    sensitivity, and licensing. Connector-specific metadata (e.g. arXiv id,
    NTRS report number, USPTO patent number) lives in ``extra``.
    """

    canonical_id: str
    title: str
    source_url: str
    publication_date: str | None
    sensitivity_class: str
    redistribution_allowed: bool
    license: str | None
    extra: dict[str, Any] = field(default_factory=dict)


class SourceConnector(ABC):
    """Abstract base class for brief-driven source connectors.

    Subclasses MUST implement exactly the 6 abstract methods below. The runner
    discovers documents via ``search``, materializes payloads via ``fetch``,
    derives stable identifiers via ``canonical_id``, enriches metadata via
    ``metadata``, and observes connector health via ``status``.
    """

    @abstractmethod
    def __init__(self, config: dict) -> None:
        """Initialize the connector from a brief-derived config dict."""

    @abstractmethod
    def search(self, page: int) -> Iterator[SourceDoc]:
        """Yield candidate ``SourceDoc`` entries for the given result page."""

    @abstractmethod
    def fetch(self, doc: SourceDoc) -> bytes:
        """Fetch the raw payload (PDF, HTML, JSON, ...) for ``doc``."""

    @abstractmethod
    def canonical_id(self, doc: SourceDoc) -> str:
        """Return the stable canonical id used to dedupe across runs."""

    @abstractmethod
    def metadata(self, doc: SourceDoc) -> dict:
        """Return a metadata dict to attach to the ingested document."""

    @abstractmethod
    def status(self) -> dict:
        """Return a structured health snapshot (rate-limit budget, last error)."""
