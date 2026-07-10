"""ArxivConnector — reference SourceConnector implementation against the arXiv Atom API.

F008c-arxiv track. Talks to https://export.arxiv.org/api/query, paginates results,
strips arXiv version suffixes to a stable canonical_id, and tags Creative
Commons-licensed entries as redistributable. Default arXiv submissions retain
a non-exclusive license to arXiv only — `redistribution_allowed` defaults to
False unless the entry advertises an explicit CC license.

Politeness: arXiv asks API consumers to keep to ~1 request per 3 seconds.
This connector enforces that gate with a monotonic clock so tests can mock it
deterministically (no real network, no real sleep).
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any, Iterator, Optional
from xml.etree import ElementTree as ET

import httpx

# Atom feed namespaces used by the arXiv API.
NS: dict[str, str] = {
    "atom": "http://www.w3.org/2005/Atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
    "arxiv": "http://arxiv.org/schemas/atom",
}

# arXiv id formats:
#   - new style:    NNNN.NNNNN(vV)   e.g. 2403.12345v2  → canonical 2403.12345
#   - old style:    archive/NNNNNNN(vV)  e.g. hep-th/9901001v1 → hep-th/9901001
_ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5}|[a-z-]+/\d{7})v(\d+)$")

# Whitespace-collapse for atom:title / atom:summary (arXiv inserts hard wraps).
_WS_RE = re.compile(r"\s+")


# Resolve base classes at import time. The package `__init__.py` defines them
# before it imports this module, so the names are already live when this
# `from ... import ...` runs (Python re-enters the partially-loaded package).
from errorta_briefs_connectors import (  # noqa: E402
    FatalError,
    RetryableError,
    SourceConnector,
    SourceDoc,
)


class ArxivConnector(SourceConnector):
    """Reference connector for the arXiv Atom API.

    Config keys (validated in __init__):
        categories   list[str]    Required. e.g. ["cs.AI", "stat.ML"].
        date_from    str | None   Optional ISO date (YYYY-MM-DD) lower bound.
        max_results  int | None   Optional cap on total docs yielded across pages.

    Test hook:
        http_client  httpx.Client Optional pre-configured client (e.g. with a
                                 MockTransport) — when provided, the connector
                                 does not construct its own.
    """

    name = "arxiv"

    BASE_URL = "https://export.arxiv.org/api/query"
    PAGE_SIZE = 30
    POLITENESS_SLEEP_S = 3.0

    def __init__(self, config: dict) -> None:
        categories = config.get("categories")
        if not categories or not isinstance(categories, list):
            raise FatalError("arxiv config requires non-empty 'categories' list")
        self.categories: list[str] = list(categories)
        self.date_from: Optional[str] = config.get("date_from")
        self.max_results: Optional[int] = config.get("max_results")

        http_client = config.get("http_client")
        if http_client is not None:
            self._client = http_client
            self._owns_client = False
        else:
            self._client = httpx.Client(timeout=30.0)
            self._owns_client = True

        # Politeness gate state. `_last_request_at` is in monotonic seconds.
        self._last_request_at: Optional[float] = None

    # ------------------------------------------------------------------ search

    def search(self, *, page: int = 0) -> Iterator[SourceDoc]:
        """Yield SourceDoc results for one page of the search.

        Pages are zero-indexed. start = page * PAGE_SIZE. arXiv search_query
        OR-joins the requested categories.
        """
        search_query = "+OR+".join(f"cat:{c}" for c in self.categories)
        start = page * self.PAGE_SIZE
        max_results = self.PAGE_SIZE
        if self.max_results is not None:
            # Don't ask for more than the caller-imposed cap on this page.
            remaining = self.max_results - start
            if remaining <= 0:
                return
            max_results = min(self.PAGE_SIZE, remaining)

        root = self._fetch_feed(search_query, start=start, max_results=max_results)
        for entry in root.findall("atom:entry", NS):
            yield self._parse_entry(entry)

    # ------------------------------------------------------------------- fetch

    def fetch(self, doc: SourceDoc) -> bytes:
        """Download the PDF bytes for `doc` via its stored pdf_url."""
        pdf_url = doc.extra.get("pdf_url")
        if not pdf_url:
            raise FatalError(f"doc {doc.canonical_id} has no pdf_url in extra")
        self._sleep_for_politeness()
        resp = self._client.get(pdf_url)
        self._classify_response(resp)
        return resp.content

    # ---------------------------------------------------------------- identity

    def canonical_id(self, doc: SourceDoc) -> str:
        return doc.canonical_id

    def metadata(self, doc: SourceDoc) -> dict:
        return doc.extra

    def status(self) -> dict:
        # No live ping — connectors are constructed before any network is
        # known-good. Caller can probe via search(page=0).
        return {"source": "arxiv", "reachable": True, "rate_limit": "1 req per 3s"}

    # ------------------------------------------------------------- internals

    def _fetch_feed(
        self, search_query: str, *, start: int, max_results: int
    ) -> ET.Element:
        params = {
            "search_query": search_query,
            "start": str(start),
            "max_results": str(max_results),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        self._sleep_for_politeness()
        resp = self._client.get(self.BASE_URL, params=params)
        self._classify_response(resp)
        # ET.fromstring accepts bytes directly; arXiv returns UTF-8 Atom XML.
        return ET.fromstring(resp.content)

    def _classify_response(self, resp: httpx.Response) -> None:
        """Map HTTP status into the connector exception taxonomy.

        200 → ok. 503 / 5xx → RetryableError (with Retry-After if present).
        400 / other non-2xx → FatalError.
        """
        status = resp.status_code
        if 200 <= status < 300:
            return
        if status == 503:
            retry_after = self._parse_retry_after(resp.headers.get("Retry-After"))
            raise RetryableError(
                f"arxiv returned 503 Service Unavailable",
                retry_after_s=retry_after,
            )
        if status == 400:
            raise FatalError(f"arxiv returned 400 Bad Request: {resp.text[:200]}")
        if 500 <= status < 600:
            retry_after = self._parse_retry_after(resp.headers.get("Retry-After"))
            raise RetryableError(
                f"arxiv returned {status}",
                retry_after_s=retry_after,
            )
        raise FatalError(f"arxiv returned unexpected status {status}")

    @staticmethod
    def _parse_retry_after(value: Optional[str]) -> Optional[float]:
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            # HTTP-date form is allowed but rare for arXiv — punt and let the
            # caller apply a default backoff.
            return None

    def _sleep_for_politeness(self) -> None:
        """Block until at least POLITENESS_SLEEP_S has passed since last request.

        Uses time.monotonic so wall-clock drift can't shorten the gate. The
        attribute access is split out so tests can monkeypatch `time.monotonic`
        and `time.sleep` independently.
        """
        now = time.monotonic()
        if self._last_request_at is not None:
            elapsed = now - self._last_request_at
            remaining = self.POLITENESS_SLEEP_S - elapsed
            if remaining > 0:
                time.sleep(remaining)
                now = time.monotonic()
        self._last_request_at = now

    # ------------------------------------------------------------ entry parse

    def _parse_entry(self, entry: ET.Element) -> SourceDoc:
        atom_id = self._text(entry.find("atom:id", NS)) or ""
        canonical, version = self._split_id(atom_id)

        title = _WS_RE.sub(" ", self._text(entry.find("atom:title", NS)) or "").strip()
        summary = _WS_RE.sub(
            " ", self._text(entry.find("atom:summary", NS)) or ""
        ).strip()

        authors: list[str] = []
        for author in entry.findall("atom:author", NS):
            name = self._text(author.find("atom:name", NS))
            if name:
                authors.append(name.strip())

        primary_cat_el = entry.find("arxiv:primary_category", NS)
        primary_category = (
            primary_cat_el.get("term") if primary_cat_el is not None else None
        )
        categories = [
            c.get("term") for c in entry.findall("atom:category", NS) if c.get("term")
        ]

        doi = self._text(entry.find("arxiv:doi", NS))
        journal_ref = self._text(entry.find("arxiv:journal_ref", NS))

        pdf_url: Optional[str] = None
        abs_url: Optional[str] = None
        license_str: Optional[str] = None
        for link in entry.findall("atom:link", NS):
            href = link.get("href") or ""
            if link.get("title") == "pdf":
                pdf_url = href
            if link.get("rel") == "alternate":
                abs_url = href
            # License heuristic: any link to creativecommons.org/licenses/...
            license_str = self._classify_license(href, current=license_str)

        published = self._parse_dt(self._text(entry.find("atom:published", NS)))
        updated = self._parse_dt(self._text(entry.find("atom:updated", NS)))

        if license_str is not None:
            redistribution_allowed = True
        else:
            redistribution_allowed = False

        source_url = abs_url or atom_id
        extra: dict[str, Any] = {
            "arxiv_version": version,
            "title": title,
            "summary": summary,
            "authors": authors,
            "primary_category": primary_category,
            "categories": categories,
            "doi": doi,
            "journal_ref": journal_ref,
            "pdf_url": pdf_url,
            "abs_url": abs_url,
            "published": published.isoformat() if published else None,
            "updated": updated.isoformat() if updated else None,
            "source_url": source_url,
            "sensitivity_class": "Public",
            "redistribution_allowed": redistribution_allowed,
            "license": license_str,
            "file_ext": ".pdf",
        }

        return SourceDoc(
            canonical_id=canonical,
            title=title,
            source_url=source_url,
            publication_date=published,
            sensitivity_class="Public",
            redistribution_allowed=redistribution_allowed,
            license=license_str,
            extra=extra,
        )

    @staticmethod
    def _text(el: Optional[ET.Element]) -> Optional[str]:
        if el is None:
            return None
        return el.text

    @staticmethod
    def _split_id(atom_id: str) -> tuple[str, Optional[str]]:
        """Strip the version suffix off an arXiv atom:id URL.

        Returns (canonical_id, version_str_or_None). Unparseable ids fall back
        to the trailing path component, with version=None.
        """
        m = _ARXIV_ID_RE.search(atom_id)
        if m is None:
            # Best-effort fallback: last path component.
            tail = atom_id.rsplit("/", 1)[-1]
            return tail, None
        return m.group(1), m.group(2)

    @staticmethod
    def _classify_license(href: str, *, current: Optional[str]) -> Optional[str]:
        """Promote the entry license if `href` points to a known CC license.

        Order of specificity: CC-BY-SA > CC-BY (so we don't accidentally
        downgrade CC-BY-SA to CC-BY). CC0 is independent.
        """
        if current in ("CC-BY-SA", "CC0"):
            return current
        low = href.lower()
        if "creativecommons.org/publicdomain/zero" in low:
            return "CC0"
        if "creativecommons.org/licenses/by-sa" in low:
            return "CC-BY-SA"
        if "creativecommons.org/licenses/by" in low:
            return "CC-BY"
        return current

    @staticmethod
    def _parse_dt(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        # arXiv uses RFC 3339 with a trailing Z.
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
