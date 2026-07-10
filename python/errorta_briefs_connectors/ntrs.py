"""NtrsConnector — NASA NTRS SourceConnector (F008-NTRS track).

Talks to https://ntrs.nasa.gov/api/citations/search (and per-citation detail
endpoints) to discover NASA Technical Reports Server records and, when a PDF is
available, fetches its bytes. Records without a PDF are still emitted so the
downstream pipeline has the metadata; the compliance gate is the gatekeeper for
redistribution.

Compliance gate (load-bearing — see docs/specs/F008-brief-driven-collection.md
§Compliance): a record is admitted only when distribution is the literal string
``PUBLIC`` *and* one of:

* ``copyright.licenseType`` is an allowlisted public license, OR
* the first author affiliation is NASA *and* ``copyright.determinationType ==
  'GOVERNMENT_WORK'``.

Anything else is refused with a human-readable reason that names the field that
failed the gate.

Politeness: NTRS does not publish a hard rate limit, but we use the same
monotonic-clock gate pattern as the arXiv connector so production runs don't
hammer the API. Test hook ``http_client`` accepts a pre-built httpx.Client (with
MockTransport) so the suite can run with zero real network.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Iterator, Optional
from urllib.parse import urljoin

import httpx

# Late base-class import to mirror arxiv.py — the package __init__ has these
# names live by the time this module's `from ... import ...` executes.
from errorta_briefs_connectors import (  # noqa: E402
    FatalError,
    SourceConnector,
    SourceDoc,
)
from errorta_briefs_connectors._http import request_with_retry  # noqa: E402

# Licenses we treat as redistributable. Anything outside this set requires the
# NASA-author + GOVERNMENT_WORK fallback to pass the compliance gate.
_ALLOWED_LICENSES: frozenset[str] = frozenset(
    {"US-Gov-Work", "Public Domain", "CC0", "CC-BY", "CC-BY-SA"}
)

# Marker used to detect NASA-authored records when the licenseType isn't on
# the allowlist (e.g. legacy records with only a determinationType set).
_NASA_ORG_MARKER: str = "NASA"


class NtrsConnector(SourceConnector):
    """SourceConnector for NASA NTRS.

    Config keys (validated in __init__):
        q          str          Required. Search query string (NTRS Lucene syntax).
        size       int | None   Page size, default 100, max 100 per upstream cap.
        max_docs   int | None   Optional cap on total docs yielded across pages.

    Test hook:
        http_client  httpx.Client  Optional pre-built client (e.g. MockTransport)
                                   — when provided, the connector does not
                                   construct its own.
    """

    name = "ntrs"

    BASE_URL: str = "https://ntrs.nasa.gov/"
    SEARCH_PATH: str = "api/citations/search"
    CITATION_PATH_TMPL: str = "api/citations/{ntrs_id}"
    PAGE_SIZE_DEFAULT: int = 100
    POLITENESS_SLEEP_S: float = 1.0
    USER_AGENT: str = "Errorta/0.3 NtrsConnector (+https://github.com/wiggins-j/errorta_app)"

    def __init__(self, config: dict) -> None:
        q = config.get("q")
        if not q or not isinstance(q, str):
            raise FatalError("ntrs config requires non-empty 'q' string")
        self.q: str = q
        size = config.get("size", self.PAGE_SIZE_DEFAULT)
        if not isinstance(size, int) or size <= 0:
            raise FatalError("ntrs config 'size' must be a positive int")
        self.size: int = size
        self.max_docs: Optional[int] = config.get("max_docs")

        http_client = config.get("http_client")
        if http_client is not None:
            self._client = http_client
            self._owns_client = False
        else:
            self._client = httpx.Client(
                timeout=30.0,
                follow_redirects=True,
                headers={"User-Agent": self.USER_AGENT},
            )
            self._owns_client = True

        self._last_request_at: Optional[float] = None

    # ------------------------------------------------------------------ search

    def search(self, *, page: int = 0) -> Iterator[SourceDoc]:
        """Yield SourceDoc results, paginating until exhaustion or max_docs.

        NTRS uses zero-indexed `page`. Pagination exits when any of:

        * the current page is short (``len(results) < size``), or
        * ``(page + 1) * size >= stats.hits`` (we've crossed the total), or
        * the connector-level ``max_docs`` cap has been satisfied.
        """
        emitted = 0
        current_page = page
        while True:
            payload = self._fetch_search_page(current_page)
            results = payload.get("results") or []
            stats = payload.get("stats") or {}
            hits = int(stats.get("hits") or 0)

            for raw in results:
                doc = self._build_source_doc(raw)
                if doc is None:
                    continue
                yield doc
                emitted += 1
                if self.max_docs is not None and emitted >= self.max_docs:
                    return

            if len(results) < self.size:
                return
            if hits and (current_page + 1) * self.size >= hits:
                return
            current_page += 1

    # ------------------------------------------------------------------- fetch

    def fetch(self, doc: SourceDoc) -> bytes:
        """Download PDF bytes for `doc`, or raise FatalError if metadata-only.

        Records emitted with ``has_pdf=False`` have no resolvable PDF; callers
        should branch on the metadata rather than calling fetch().
        """
        if not doc.extra.get("has_pdf"):
            raise FatalError(f"doc {doc.canonical_id} has no PDF (metadata-only)")
        pdf_url = doc.extra.get("pdf_url")
        if not pdf_url:
            raise FatalError(f"doc {doc.canonical_id} has has_pdf=True but no pdf_url")
        self._sleep_for_politeness()
        resp = request_with_retry(lambda: self._client.get(pdf_url))
        return resp.content

    # ---------------------------------------------------------------- identity

    def canonical_id(self, doc: SourceDoc) -> str:
        return doc.canonical_id

    def metadata(self, doc: SourceDoc) -> dict:
        return doc.extra

    def status(self) -> dict:
        return {"source": "ntrs", "reachable": True, "rate_limit": "~1 req per 1s"}

    # ------------------------------------------------------------- internals

    def _fetch_search_page(self, page: int) -> dict:
        params = {
            "q": self.q,
            "page": str(page),
            "size": str(self.size),
            "sort": "published:desc",
        }
        url = urljoin(self.BASE_URL, self.SEARCH_PATH)
        self._sleep_for_politeness()
        resp = request_with_retry(lambda: self._client.get(url, params=params))
        try:
            return resp.json()
        except ValueError as exc:
            raise FatalError(f"ntrs returned invalid JSON: {exc}") from exc

    def _fetch_citation(self, ntrs_id: str) -> dict:
        path = self.CITATION_PATH_TMPL.format(ntrs_id=ntrs_id)
        url = urljoin(self.BASE_URL, path)
        self._sleep_for_politeness()
        resp = request_with_retry(lambda: self._client.get(url))
        try:
            return resp.json()
        except ValueError as exc:
            raise FatalError(f"ntrs citation returned invalid JSON: {exc}") from exc

    def _sleep_for_politeness(self) -> None:
        now = time.monotonic()
        if self._last_request_at is not None:
            elapsed = now - self._last_request_at
            remaining = self.POLITENESS_SLEEP_S - elapsed
            if remaining > 0:
                time.sleep(remaining)
                now = time.monotonic()
        self._last_request_at = now

    # ----------------------------------------------------------- record build

    def _build_source_doc(self, raw: dict) -> Optional[SourceDoc]:
        """Map an NTRS citation dict to a SourceDoc.

        Returns None when the compliance gate refuses the record — refusals are
        annotated on the dict so the runner can log them, but the connector
        itself just skips. (Tests exercise `_check_compliance` directly.)
        """
        ntrs_id = self._normalize_ntrs_id(raw.get("id"))
        if ntrs_id is None:
            return None
        allowed, reason = self._check_compliance(raw)
        if not allowed:
            return None

        title = (raw.get("title") or "").strip()
        published = self._parse_publication_date(raw.get("publicationDate"))
        downloads = raw.get("downloads") or []
        pdf_url = self._resolve_pdf_url(downloads)
        has_pdf = pdf_url is not None
        authors = self._extract_authors(raw)
        license_type = ((raw.get("copyright") or {}).get("licenseType")) or None
        source_url = urljoin(self.BASE_URL, f"citations/{ntrs_id}")

        extra: dict[str, Any] = {
            "ntrs_id": ntrs_id,
            "title": title,
            "authors": authors,
            "publication_date": published.isoformat() if published else None,
            "publication_date_raw": raw.get("publicationDate"),
            "distribution": raw.get("distribution"),
            "license_type": license_type,
            "determination_type": (raw.get("copyright") or {}).get("determinationType"),
            "downloads": downloads,
            "pdf_url": pdf_url,
            "has_pdf": has_pdf,
            "source_url": source_url,
            "sensitivity_class": "Public",
            "redistribution_allowed": True,
            "compliance_reason": reason,
            # Only populated by an explicit fetch; the search step never reads
            # bytes so that metadata-only records cost zero PDF traffic.
            "full_text": None,
            "file_ext": ".pdf",
        }
        return SourceDoc(
            canonical_id=f"ntrs:{ntrs_id}",
            title=title,
            source_url=source_url,
            publication_date=published,
            sensitivity_class="Public",
            redistribution_allowed=True,
            license=license_type,
            extra=extra,
        )

    @staticmethod
    def _normalize_ntrs_id(raw_id: Any) -> Optional[str]:
        if raw_id is None:
            return None
        s = str(raw_id).strip()
        if not s:
            return None
        # Zero-pad to 20 digits when the id is numeric (NTRS canonical form).
        if s.isdigit():
            return s.zfill(20)
        return s

    @staticmethod
    def _extract_authors(raw: dict) -> list[str]:
        out: list[str] = []
        for aff in raw.get("authorAffiliations") or []:
            meta = aff.get("meta") or {}
            author = meta.get("author") or {}
            name = author.get("name")
            if isinstance(name, str) and name.strip():
                out.append(name.strip())
        return out

    @classmethod
    def _check_compliance(cls, raw: dict) -> tuple[bool, str]:
        """Apply the load-bearing compliance gate. Returns (allowed, reason).

        ``reason`` is always a short human-readable string; on refusal it names
        the specific field that failed so logs are actionable.
        """
        distribution = raw.get("distribution")
        if distribution != "PUBLIC":
            return False, f"distribution={distribution!r} (must be 'PUBLIC')"

        copyright_obj = raw.get("copyright") or {}
        license_type = copyright_obj.get("licenseType")
        determination_type = copyright_obj.get("determinationType")

        if license_type in _ALLOWED_LICENSES:
            return True, f"licenseType={license_type}"

        # NASA-authored fallback: first affiliation's org name contains NASA
        # AND the determination is GOVERNMENT_WORK.
        affiliations = raw.get("authorAffiliations") or []
        first_org = ""
        if affiliations:
            meta = (affiliations[0].get("meta") or {})
            org = meta.get("organization") or {}
            first_org = org.get("name") or ""
        if _NASA_ORG_MARKER in first_org and determination_type == "GOVERNMENT_WORK":
            return True, f"NASA GOVERNMENT_WORK (org={first_org!r})"

        return (
            False,
            (
                f"licenseType={license_type!r} not in allowlist and "
                f"no NASA GOVERNMENT_WORK fallback (org={first_org!r}, "
                f"determinationType={determination_type!r})"
            ),
        )

    def _resolve_pdf_url(self, downloads: list[dict]) -> Optional[str]:
        for entry in downloads:
            mimetype = (entry.get("mimetype") or "").lower()
            if mimetype != "application/pdf":
                continue
            links = entry.get("links") or {}
            original = links.get("original")
            if not original:
                continue
            return urljoin(self.BASE_URL, original)
        return None

    @staticmethod
    def _parse_publication_date(raw: Any) -> Optional[datetime]:
        """Tolerate 'YYYY', 'YYYY-MM', 'YYYY-MM-DD'. Anything else → None."""
        if not isinstance(raw, str) or not raw.strip():
            return None
        s = raw.strip()
        # Strip trailing time component if present (e.g. '2023-04-05T00:00:00Z').
        if "T" in s:
            s = s.split("T", 1)[0]
        for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None
